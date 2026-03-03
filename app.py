"""Flask app for LoL Tournament Scout."""

import threading
import time
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, url_for

import analysis
import scraper
import storage

# Track background refresh jobs: {team_id: {status, results, total, done}}
_refresh_jobs = {}

app = Flask(__name__)


@app.errorhandler(Exception)
def handle_exception(e):
    """Return JSON for API errors instead of HTML error pages."""
    if request.path.startswith("/api/"):
        return jsonify({"error": str(e)}), 500
    raise e


@app.route("/favicon.ico")
def favicon():
    return "", 204


# --- Page routes ---


@app.route("/")
def index():
    data = storage.load()
    return render_template("index.html", data=data)


@app.route("/team/<team_id>")
def team_detail(team_id):
    data = storage.load()
    team = storage.get_team(data, team_id)
    if not team:
        return redirect(url_for("index"))
    is_my_team = data["meta"]["my_team_id"] == team_id
    return render_template("team.html", data=data, team=team, is_my_team=is_my_team)


@app.route("/analysis/<opp_id>")
def analysis_page(opp_id):
    data = storage.load()
    my_team_id = data["meta"]["my_team_id"]
    if not my_team_id:
        return redirect(url_for("index"))
    my_team = storage.get_team(data, my_team_id)
    opp_team = storage.get_team(data, opp_id)
    if not my_team or not opp_team:
        return redirect(url_for("index"))

    ban_recs = analysis.get_ban_recommendations(opp_team)
    pick_recs = analysis.get_pick_recommendations(my_team, opp_team)
    one_tricks = analysis.identify_one_tricks(opp_team)

    return render_template(
        "analysis.html",
        data=data,
        my_team=my_team,
        opp_team=opp_team,
        ban_recs=ban_recs,
        pick_recs=pick_recs,
        one_tricks=one_tricks,
    )


@app.route("/manage")
def manage():
    data = storage.load()
    return render_template("manage.html", data=data)


@app.route("/export")
def export_page():
    from datetime import datetime, timezone
    data = storage.load()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html = render_template("export.html", data=data, now=now)
    filename = f"lol_ims_info_{data['meta']['season_name'].replace(' ', '_')}.html"
    return html, 200, {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type": "text/html; charset=utf-8",
    }


@app.route("/export/analysis/<opp_id>")
def export_analysis(opp_id):
    from datetime import datetime, timezone
    data = storage.load()
    my_team_id = data["meta"]["my_team_id"]
    if not my_team_id:
        return redirect(url_for("index"))
    my_team = storage.get_team(data, my_team_id)
    opp_team = storage.get_team(data, opp_id)
    if not my_team or not opp_team:
        return redirect(url_for("index"))

    ban_recs = analysis.get_ban_recommendations(opp_team)
    pick_recs = analysis.get_pick_recommendations(my_team, opp_team)
    one_tricks = analysis.identify_one_tricks(opp_team)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    html = render_template(
        "export_analysis.html",
        my_team=my_team,
        opp_team=opp_team,
        ban_recs=ban_recs,
        pick_recs=pick_recs,
        one_tricks=one_tricks,
        now=now,
    )
    filename = f"analysis_{my_team['name']}_vs_{opp_team['name']}.html".replace(" ", "_")
    return html, 200, {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type": "text/html; charset=utf-8",
    }


# --- API routes ---


@app.route("/api/season", methods=["PUT"])
def api_update_season():
    data = storage.load()
    body = request.json
    if "season_name" in body:
        data["meta"]["season_name"] = body["season_name"].strip()
    storage.save(data)
    return jsonify({"success": True})


@app.route("/api/teams", methods=["POST"])
def api_create_team():
    data = storage.load()
    name = request.json.get("name", "New Team").strip()
    if not name:
        return jsonify({"error": "Team name required"}), 400
    team = storage.add_team(data, name)
    return jsonify({"success": True, "team": team})


@app.route("/api/teams/<team_id>/move", methods=["POST"])
def api_move_team(team_id):
    data = storage.load()
    direction = request.json.get("direction", 0)
    teams = data["teams"]
    idx = next((i for i, t in enumerate(teams) if t["id"] == team_id), None)
    if idx is None:
        return jsonify({"error": "Team not found"}), 404
    new_idx = idx + direction
    if new_idx < 0 or new_idx >= len(teams):
        return jsonify({"success": True})  # already at edge
    teams[idx], teams[new_idx] = teams[new_idx], teams[idx]
    storage.save(data)
    return jsonify({"success": True})


@app.route("/api/teams/<team_id>", methods=["PUT"])
def api_update_team(team_id):
    data = storage.load()
    team = storage.get_team(data, team_id)
    if not team:
        return jsonify({"error": "Team not found"}), 404

    body = request.json
    if "name" in body:
        team["name"] = body["name"].strip()
    if body.get("set_my_team"):
        data["meta"]["my_team_id"] = team_id
    if "season_name" in body:
        data["meta"]["season_name"] = body["season_name"].strip()

    storage.save(data)
    return jsonify({"success": True})


@app.route("/api/teams/<team_id>", methods=["DELETE"])
def api_delete_team(team_id):
    data = storage.load()
    if storage.delete_team(data, team_id):
        return jsonify({"success": True})
    return jsonify({"error": "Team not found"}), 404


@app.route("/api/players", methods=["POST"])
def api_create_player():
    data = storage.load()
    body = request.json
    team_id = body.get("team_id")
    if not team_id:
        return jsonify({"error": "team_id required"}), 400

    # Support op.gg link or individual name
    player_input = body.get("player_input", "").strip()
    role = body.get("role", "fill")
    is_sub = body.get("is_substitute", False)

    if not player_input:
        return jsonify({"error": "Player input required"}), 400

    parsed = scraper.parse_player_input(player_input)

    # Overwrite mode: clear existing players before adding new ones
    if body.get("overwrite"):
        team = storage.get_team(data, team_id)
        if team:
            team["players"] = []
            storage.save(data)

    # Auto-assign roles in order when pasting a multi-player link
    role_sequence = ["top", "jungle", "mid", "bot", "support"]
    added = []
    team = storage.get_team(data, team_id)
    for i, (game_name, tag_line) in enumerate(parsed):
        if len(parsed) >= 5 and i < 5:
            player_role = role_sequence[i]
        else:
            player_role = role

        # Single player add: replace existing starter in same role (keep position)
        insert_idx = None
        if len(parsed) == 1 and not is_sub and player_role != "fill" and team:
            for j, existing in enumerate(team["players"]):
                if existing.get("role") == player_role and not existing.get("is_substitute"):
                    insert_idx = j
                    team["players"].pop(j)
                    storage.save(data)
                    break

        player = storage.add_player(data, team_id, game_name, tag_line, player_role, is_sub)
        if player and insert_idx is not None and team:
            # Move from end to original position
            team["players"].remove(player)
            team["players"].insert(insert_idx, player)
            storage.save(data)
        if player:
            added.append(player)

    if not added:
        return jsonify({"error": "Could not add players"}), 400
    return jsonify({"success": True, "players": added})


@app.route("/api/players/<player_id>", methods=["PUT"])
def api_update_player(player_id):
    data = storage.load()
    body = request.json
    updates = {}
    for key in ("game_name", "tag_line", "role", "is_substitute"):
        if key in body:
            updates[key] = body[key]

    # Manual stats override
    if "manual_stats" in body:
        ms = body["manual_stats"]
        stats = {
            "last_updated": __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ).isoformat(),
            "tier": ms.get("tier", "UNRANKED"),
            "division": ms.get("division"),
            "lp": ms.get("lp", 0),
            "season_games": ms.get("season_games", 0),
            "season_wins": ms.get("season_wins", 0),
            "season_losses": ms.get("season_losses", 0),
            "season_winrate": 0,
            "champions": ms.get("champions", []),
            "scrape_error": None,
            "manual_override": True,
        }
        if stats["season_games"] > 0:
            stats["season_winrate"] = round(
                stats["season_wins"] / stats["season_games"] * 100, 1
            )
        updates["stats"] = stats

    # Extra fields (previous_season_tier, peak_tier) — merge into existing stats
    if "extra" in body:
        _, player_obj = storage.get_player(data, player_id)
        if player_obj:
            if not player_obj.get("stats"):
                player_obj["stats"] = {
                    "last_updated": None, "tier": "UNRANKED", "division": None,
                    "lp": 0, "season_games": 0, "season_wins": 0,
                    "season_losses": 0, "season_winrate": 0, "champions": [],
                    "scrape_error": None, "opgg_url": None,
                    "previous_season_tier": None, "peak_tier": None,
                }
            for k, v in body["extra"].items():
                player_obj["stats"][k] = v if v else None
            storage.save(data)

    player = storage.update_player(data, player_id, **updates)
    if not player:
        return jsonify({"error": "Player not found"}), 404
    return jsonify({"success": True, "player": player})


@app.route("/api/players/<player_id>", methods=["DELETE"])
def api_delete_player(player_id):
    data = storage.load()
    if storage.delete_player(data, player_id):
        return jsonify({"success": True})
    return jsonify({"error": "Player not found"}), 404


@app.route("/api/players/<player_id>/replace", methods=["POST"])
def api_replace_player(player_id):
    data = storage.load()
    _, old_player = storage.get_player(data, player_id)
    if not old_player:
        return jsonify({"error": "Player not found"}), 404

    body = request.json
    player_input = body.get("player_input", "").strip()
    if not player_input:
        return jsonify({"error": "Player input required"}), 400

    parsed = scraper.parse_player_input(player_input)
    if not parsed:
        return jsonify({"error": "Could not parse player input"}), 400

    game_name, tag_line = parsed[0]

    # Keep the old player's role, swap name/tag, clear stats
    old_player["game_name"] = game_name
    old_player["tag_line"] = tag_line
    old_player["stats"] = None
    storage.save(data)

    return jsonify({"success": True, "player": old_player})


@app.route("/api/players/<player_id>/refresh", methods=["POST"])
def api_refresh_player(player_id):
    data = storage.load()
    _, player = storage.get_player(data, player_id)
    if not player:
        return jsonify({"error": "Player not found"}), 404

    try:
        stats = scraper.scrape_player(player["game_name"], player["tag_line"])
        storage.update_player(data, player_id, stats=stats)
        return jsonify({"success": True, "stats": stats})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _refresh_one_player(player):
    """Refresh a single player. Returns (player_dict, stats_or_none, error_or_none)."""
    try:
        stats = scraper.scrape_player(player["game_name"], player["tag_line"])
        return player, stats, None
    except Exception as e:
        return player, None, str(e)


def _refresh_team_worker(team_id, players):
    """Background worker that refreshes all players on a team in parallel."""
    from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError

    job = _refresh_jobs[team_id]
    try:
        # Run up to 3 players at once
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {
                pool.submit(_refresh_one_player, p): p for p in players
            }
            for future in as_completed(futures, timeout=300):  # 5 min max total
                try:
                    player, stats, error = future.result(timeout=90)  # 90s per player
                    if stats:
                        data = storage.load()
                        storage.update_player(data, player["id"], stats=stats)
                        job["results"].append({"player": player["game_name"], "success": True})
                    else:
                        job["results"].append({"player": player["game_name"], "success": False, "error": error or "Unknown"})
                except TimeoutError:
                    p = futures.get(future, {})
                    name = p.get("game_name", "?") if isinstance(p, dict) else "?"
                    job["results"].append({"player": name, "success": False, "error": "Timed out"})
                except Exception as e:
                    job["results"].append({"player": "?", "success": False, "error": str(e)})
                job["done"] += 1
    except TimeoutError:
        # Overall timeout — mark remaining as failed
        remaining = job["total"] - job["done"]
        if remaining > 0:
            job["results"].append({"player": f"{remaining} players", "success": False, "error": "Overall timeout"})
            job["done"] = job["total"]
    except Exception as e:
        job["results"].append({"player": "worker", "success": False, "error": str(e)})
    finally:
        job["status"] = "complete"
        job["current"] = None


@app.route("/api/teams/<team_id>/refresh", methods=["POST"])
def api_refresh_team(team_id):
    data = storage.load()
    team = storage.get_team(data, team_id)
    if not team:
        return jsonify({"error": "Team not found"}), 404

    # Don't start a new job if one is already running
    if team_id in _refresh_jobs and _refresh_jobs[team_id]["status"] == "running":
        return jsonify({"success": True, "status": "already_running"})

    _refresh_jobs[team_id] = {
        "status": "running",
        "results": [],
        "total": len(team["players"]),
        "done": 0,
        "current": None,
    }

    thread = threading.Thread(
        target=_refresh_team_worker,
        args=(team_id, list(team["players"])),
        daemon=True,
    )
    thread.start()

    return jsonify({"success": True, "status": "started", "total": len(team["players"])})


@app.route("/api/teams/<team_id>/refresh/status", methods=["GET"])
def api_refresh_status(team_id):
    job = _refresh_jobs.get(team_id)
    if not job:
        return jsonify({"status": "none"})
    return jsonify(job)


@app.route("/api/refresh-all", methods=["POST"])
def api_refresh_all():
    data = storage.load()
    all_players = []
    for team in data["teams"]:
        all_players.extend(team["players"])

    if not all_players:
        return jsonify({"error": "No players to refresh"}), 400

    job_id = "__all__"
    if job_id in _refresh_jobs and _refresh_jobs[job_id]["status"] == "running":
        return jsonify({"success": True, "status": "already_running"})

    _refresh_jobs[job_id] = {
        "status": "running",
        "results": [],
        "total": len(all_players),
        "done": 0,
        "current": None,
    }

    thread = threading.Thread(
        target=_refresh_team_worker,
        args=(job_id, list(all_players)),
        daemon=True,
    )
    thread.start()

    return jsonify({"success": True, "status": "started", "total": len(all_players)})


@app.route("/api/import/multi-link", methods=["POST"])
def api_import_multi():
    url = request.json.get("url", "")
    try:
        players = scraper.parse_opgg_multi_link(url)
        return jsonify({
            "success": True,
            "players": [{"game_name": g, "tag_line": t} for g, t in players],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.template_filter("champion_icon")
def champion_icon_filter(champion_key):
    return scraper.champion_icon_url(champion_key)


@app.template_filter("tier_color")
def tier_color_filter(tier):
    colors = {
        "CHALLENGER": "#f4c874",
        "GRANDMASTER": "#ef4444",
        "MASTER": "#a855f7",
        "DIAMOND": "#60a5fa",
        "EMERALD": "#34d399",
        "PLATINUM": "#5eead4",
        "GOLD": "#fbbf24",
        "SILVER": "#94a3b8",
        "BRONZE": "#d97706",
        "IRON": "#78716c",
        "UNRANKED": "#6b7280",
    }
    return colors.get(tier, "#6b7280")


@app.template_filter("peak_color")
def peak_color_filter(peak_str):
    """Get tier color from a peak rank string like 'Platinum I'."""
    if not peak_str:
        return "#6b7280"
    base = peak_str.split()[0].upper()
    return tier_color_filter(base)


@app.template_filter("tier_display")
def tier_display_filter(player):
    stats = player.get("stats")
    if not stats or stats.get("tier") == "UNRANKED":
        return "Unranked"
    tier = stats["tier"].capitalize()
    div = stats.get("division", "")
    lp = stats.get("lp", 0)
    roman = {1: "I", 2: "II", 3: "III", 4: "IV"}.get(div, "")
    if stats["tier"] in ("CHALLENGER", "GRANDMASTER", "MASTER"):
        return f"{tier} {lp} LP"
    return f"{tier} {roman}" + (f" {lp} LP" if lp else "")


@app.template_filter("current_higher_than_peak")
def current_higher_than_peak_filter(player):
    """Returns True if current rank is higher than peak rank."""
    stats = player.get("stats")
    if not stats or stats.get("tier") == "UNRANKED" or not stats.get("peak_tier"):
        return False
    tier_order = {
        "challenger": 0, "grandmaster": 1, "master": 2, "diamond": 3,
        "emerald": 4, "platinum": 5, "gold": 6, "silver": 7, "bronze": 8, "iron": 9,
    }
    div_map = {"I": 1, "II": 2, "III": 3, "IV": 4}
    # Current rank
    cur_tier = stats["tier"].lower()
    cur_div = stats.get("division") or 1
    cur_key = (tier_order.get(cur_tier, 99), cur_div)
    # Peak rank (e.g. "Platinum I")
    peak_parts = stats["peak_tier"].split()
    peak_tier = peak_parts[0].lower() if peak_parts else ""
    peak_div = div_map.get(peak_parts[1], 1) if len(peak_parts) > 1 else 1
    peak_key = (tier_order.get(peak_tier, 99), peak_div)
    return cur_key < peak_key  # lower = better


if __name__ == "__main__":
    Path("data").mkdir(exist_ok=True)
    if not storage.DATA_FILE.exists():
        storage.save(storage.default_tournament())
    app.run(debug=True, host="127.0.0.1", port=5000)
