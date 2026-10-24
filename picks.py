"""
Highlights Picker — spoiler-free NBA/NFL highlight recommendations.

Reads the last N days of games from ESPN's public site API, scores each game on
  * interest  — your teams (config.toml), and for NBA whether their stars played
  * strength  — how good both teams are (win %, shrunk toward .500 early season)
  * drama     — closeness, lead changes, late-game win-probability tension, OT
and prints the best ones with a link to the official highlight video.

SPOILER RULE: nothing this script prints may reveal a score, winner, margin,
record, overtime, or stat line. Only pre-game-safe facts are shown.

Usage:
  python picks.py --nba 1 --nfl 1
  python picks.py --minutes 60
  python picks.py --nba 3 --days 3 --html
Python 3.11+, standard library only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import sys
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOCAL_TZ = None  # set from config.toml "timezone"; None = this computer's zone
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
LEAGUES = {
    "nba": {"path": "basketball/nba", "periods": 4, "channel": "NBA", "suffix": "full game highlights"},
    "nfl": {"path": "football/nfl", "periods": 4, "channel": "NFL", "suffix": "game highlights"},
}


# ---------------------------------------------------------------- fetching
def get(url: str, as_json: bool = True, timeout: int = 20):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": "CONSENT=YES+1; SOCS=CAI",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "replace")
    return json.loads(body) if as_json else body


def espn(league: str, endpoint: str, **params) -> dict:
    q = urllib.parse.urlencode(params)
    return get(f"https://site.api.espn.com/apis/site/v2/sports/{LEAGUES[league]['path']}/{endpoint}?{q}")


def finished_games(league: str, days: int) -> list[dict]:
    today = dt.date.today()
    seen, games = set(), []
    for i in range(days + 1):
        d = (today - dt.timedelta(days=i)).strftime("%Y%m%d")
        try:
            data = espn(league, "scoreboard", dates=d, limit=100)
        except Exception as e:  # network hiccup on one day shouldn't kill the run
            print(f"  (couldn't load {league.upper()} {d}: {e})", file=sys.stderr)
            continue
        for ev in data.get("events", []):
            if ev["id"] in seen:
                continue
            comp = ev["competitions"][0]
            if not comp.get("status", ev.get("status", {})).get("type", {}).get("completed"):
                continue
            seen.add(ev["id"])
            games.append(ev)
    return games


# ---------------------------------------------------------------- helpers
def team_info(comp: dict) -> dict:
    out = {}
    for c in comp["competitors"]:
        t = c["team"]
        rec = next((r.get("summary") for r in c.get("records", []) if r.get("type") in ("total", None)), None)
        if rec is None and c.get("records"):
            rec = c["records"][0].get("summary")
        out[c["homeAway"]] = {
            "name": t.get("name") or t.get("shortDisplayName") or t.get("displayName"),
            "display": t.get("displayName"),
            "abbr": t.get("abbreviation"),
            "score": float(c.get("score") or 0),
            "record": rec,
        }
    return out


def win_pct(record: str | None, pseudo: int) -> float:
    """Win % shrunk toward .500 by `pseudo` fake wins and losses (early-season sanity)."""
    if not record:
        return 0.5
    parts = [int(x) for x in re.findall(r"\d+", record)[:3]]
    w, l = (parts + [0, 0])[:2]
    ties = parts[2] if len(parts) > 2 else 0
    return (w + 0.5 * ties + pseudo) / (w + l + ties + 2 * pseudo) if (w + l + ties) else 0.5


def match_team(name: str, display: str, prefs: list[dict]) -> dict | None:
    for p in prefs:
        n = p["name"].lower()
        if n == (name or "").lower() or n in (display or "").lower():
            return p
    return None


def norm(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", s.lower())


# ---------------------------------------------------------------- scoring
def drama_score(league: str, summary: dict, teams: dict, final_period: int) -> float:
    """0..1. Uses (in order of preference) win probability, the score timeline, final margin."""
    parts: list[tuple[float, float]] = []  # (value, weight)

    margin = abs(teams["home"]["score"] - teams["away"]["score"])
    if league == "nba":
        steps = [(3, 1.0), (6, .8), (10, .55), (15, .3)]
    else:
        steps = [(3, 1.0), (7, .8), (10, .55), (14, .3)]
    close = next((v for m, v in steps if margin <= m), .1)
    if final_period > LEAGUES[league]["periods"]:
        close = 1.0
    parts.append((close, 0.40))

    # Win-probability tension in the last quarter of the game + total swing
    wp = [e.get("homeWinPercentage") for e in summary.get("winprobability", []) if e.get("homeWinPercentage") is not None]
    if len(wp) >= 10:
        tail = wp[int(len(wp) * 0.75):]
        tension = sum(1 - 2 * abs(p - 0.5) for p in tail) / len(tail)
        swing = sum(abs(b - a) for a, b in zip(wp, wp[1:]))
        parts.append((tension, 0.30))
        parts.append((min(swing / (6.0 if league == "nba" else 4.0), 1.0), 0.15))

    # Lead changes from the score timeline
    timeline = summary.get("plays") if league == "nba" else summary.get("scoringPlays")
    diffs = []
    for p in timeline or []:
        h, a = p.get("homeScore"), p.get("awayScore")
        if h is None or a is None:
            continue
        d = float(h) - float(a)
        if d != 0:
            diffs.append(d > 0)
    if diffs:
        changes = sum(1 for x, y in zip(diffs, diffs[1:]) if x != y)
        parts.append((min(changes / (12 if league == "nba" else 4), 1.0), 0.15))

    tot = sum(w for _, w in parts)
    return sum(v * w for v, w in parts) / tot


def nba_star_status(summary: dict, stars: list[str]) -> tuple[list[str], list[str]]:
    """Returns (played, missed) among the listed stars that appear on either roster in the box score."""
    want = {norm(s): s for s in stars}
    played, missed = [], []
    for side in summary.get("boxscore", {}).get("players", []):
        for block in side.get("statistics", []):
            labels = block.get("labels") or block.get("names") or []
            mi = labels.index("MIN") if "MIN" in labels else None
            for a in block.get("athletes", []):
                key = norm(a.get("athlete", {}).get("displayName", ""))
                if key not in want:
                    continue
                stats = a.get("stats") or []
                mins = 0.0
                if mi is not None and mi < len(stats):
                    try:
                        mins = float(str(stats[mi]).split(":")[0])
                    except ValueError:
                        mins = 0.0
                (missed if a.get("didNotPlay") or mins <= 0 else played).append(want[key])
    return played, missed


def big_performance(league: str, summary: dict) -> bool:
    if league != "nba":
        return False
    for side in summary.get("boxscore", {}).get("players", []):
        for block in side.get("statistics", []):
            labels = block.get("labels") or []
            idx = {k: labels.index(k) for k in ("PTS", "REB", "AST", "STL", "BLK") if k in labels}
            for a in block.get("athletes", []):
                s = a.get("stats") or []
                try:
                    vals = {k: int(s[i]) for k, i in idx.items() if i < len(s)}
                except ValueError:
                    continue
                if vals.get("PTS", 0) >= 40 or sum(1 for v in vals.values() if v >= 10) >= 3:
                    return True
    return False


def score_game(league: str, ev: dict, cfg: dict) -> dict | None:
    lc, sc = cfg[league], cfg["scoring"]
    comp = ev["competitions"][0]
    teams = team_info(comp)
    prefs = lc.get("teams", [])
    final_period = (comp.get("status") or ev.get("status") or {}).get("period") or 0

    try:
        summary = espn(league, "summary", event=ev["id"])
    except Exception:
        summary = {}

    interest, reasons, my_teams = 0.0, [], []
    star_lines = []
    for side in ("away", "home"):
        t = teams[side]
        p = match_team(t["name"], t["display"], prefs)
        w = p["weight"] if p else sc.get("other_team_weight", 0.15)
        if p:
            my_teams.append(t["name"])
            stars = p.get("stars") or []
            if league == "nba" and stars and summary:
                played, missed = nba_star_status(summary, stars)
                if played:
                    star_lines.append(" & ".join(played))
                elif missed:
                    w *= 0.25  # their guy sat — game is mostly a throwaway for you
                    star_lines.append(f"no {' / '.join(missed)}")
        interest += w
    interest = min(interest / 2.0, 1.0)
    if len(my_teams) == 2:
        interest = min(interest + 0.15, 1.0)

    pseudo = 6 if league == "nba" else 2
    ov = sc.get("strength_override", {}) or {}
    strengths = [ov.get(teams[s]["name"], win_pct(teams[s]["record"], pseudo)) for s in ("away", "home")]
    # reward both teams being good, not one great team beating a bad one
    strength = (min(strengths) * 0.6 + max(strengths) * 0.4)
    strength = max(0.0, min((strength - 0.35) / 0.4, 1.0))

    drama = drama_score(league, summary, teams, final_period)

    total = sc["w_interest"] * interest + sc["w_strength"] * strength + sc["w_drama"] * drama
    if sc.get("boost_big_performances", True) and summary and big_performance(league, summary):
        total += 0.05

    # spoiler-safe reasons only
    if my_teams:
        reasons.append("your team" + ("s" if len(my_teams) > 1 else "") + ": " + ", ".join(my_teams))
    for s in star_lines:
        reasons.append(s + (" played" if not s.startswith("no ") else ""))
    if strength >= 0.7:
        reasons.append("two strong teams")

    date = dt.datetime.fromisoformat(ev["date"].replace("Z", "+00:00")).astimezone(LOCAL_TZ)
    return {
        "league": league,
        "id": ev["id"],
        "away": teams["away"], "home": teams["home"],
        "date": date,
        "week": (ev.get("week") or {}).get("number"),
        "score": total,
        "reasons": reasons,
        "_debug": {"interest": round(interest, 2), "strength": round(strength, 2), "drama": round(drama, 2)},
    }


# ---------------------------------------------------------------- video links
def find_video(league: str, g: dict) -> tuple[str, bool]:
    """Returns (url, is_direct). Looks for the official league channel's highlight upload."""
    a, h = g["away"]["name"], g["home"]["name"]
    date_q = f"{a} vs {h} {LEAGUES[league]['suffix']} {g['date']:%B} {g['date'].day} {g['date'].year}"
    if league == "nfl":
        wk = f" week {g['week']}" if g.get("week") else ""
        queries = [f"{a} vs {h} highlights NFL {g['date'].year}{wk}", f"{a} {h} game highlights NFL", date_q]
    else:
        queries = [date_q, f"{a} at {h} full game highlights NBA"]
    fallback = "https://www.youtube.com/results?" + urllib.parse.urlencode({"search_query": queries[0]})
    for q in queries:
        url = _search_official(league, a, h, q)
        if url:
            return url, True
    return fallback, False


def _search_official(league: str, a: str, h: str, q: str) -> str | None:
    search_url = "https://www.youtube.com/results?" + urllib.parse.urlencode({"search_query": q})
    try:
        page = get(search_url, as_json=False)
        m = re.search(r"var ytInitialData\s*=\s*(\{.*?\});\s*</script>", page, re.S)
        data = json.loads(m.group(1)) if m else {}
    except Exception:
        return None

    def text(o):
        if not isinstance(o, dict):
            return ""
        return o.get("simpleText") or "".join(r.get("text", "") for r in o.get("runs", []))

    def walk(o):
        if isinstance(o, dict):
            if "videoRenderer" in o:
                yield o["videoRenderer"]
            for v in o.values():
                yield from walk(v)
        elif isinstance(o, list):
            for v in o:
                yield from walk(v)

    want_channel = LEAGUES[league]["channel"].lower()
    for v in walk(data):
        owners = {text(v.get(k)).strip().lower() for k in ("ownerText", "longBylineText", "shortBylineText")}
        title = text(v.get("title")).lower()
        age = text(v.get("publishedTimeText")).lower()
        if want_channel not in owners:
            continue
        if "year" in age or "month" in age:  # an older meeting between the same teams
            continue
        if a.lower() in title and h.lower() in title and "highlight" in title:
            return f"https://www.youtube.com/watch?v={v['videoId']}"
    return None


# ---------------------------------------------------------------- output
def stars(score: float) -> str:
    n = max(1, min(5, round(score * 5 + 0.25)))
    return "★" * n + "☆" * (5 - n)


def pick(cfg: dict, want: dict[str, int], minutes: int | None, days: int, debug: bool) -> list[dict]:
    pool: dict[str, list[dict]] = {}
    for lg in want:
        evs = finished_games(lg, days)
        print(f"  checked {len(evs)} finished {lg.upper()} games", file=sys.stderr)
        scored = [g for g in (score_game(lg, e, cfg) for e in evs) if g]
        pool[lg] = sorted(scored, key=lambda g: g["score"], reverse=True)

    chosen: list[dict] = []
    if minutes:
        budget = minutes
        merged = sorted((g for lst in pool.values() for g in lst), key=lambda g: g["score"], reverse=True)
        for g in merged:
            length = cfg[g["league"]].get("video_minutes", 12)
            if length <= budget:
                chosen.append(g)
                budget -= length
            if budget < min(cfg[l].get("video_minutes", 12) for l in want):
                break
    else:
        for lg, n in want.items():
            chosen += pool[lg][:n]
    for g in chosen:
        g["url"], g["direct"] = find_video(g["league"], g)
    return chosen


def render_text(picks: list[dict], debug: bool) -> str:
    if not picks:
        return "Nothing worth your time found in that window."
    lines = []
    for g in picks:
        when = f"{g['date']:%a %b} {g['date'].day}"
        lines.append(f"{g['league'].upper()}  {g['away']['display']} @ {g['home']['display']}  ({when})  {stars(g['score'])}")
        if g["reasons"]:
            lines.append("      " + " · ".join(g["reasons"]))
        lines.append("      " + g["url"] + ("" if g["direct"] else "   (search — couldn't pin the exact video)"))
        if debug:
            lines.append("      debug: " + json.dumps(g["_debug"]))
    return "\n".join(lines)


def render_html(picks: list[dict]) -> str:
    cards = []
    for g in picks:
        cards.append(f"""<a class=card href="{html.escape(g['url'])}">
<div class=lg>{g['league'].upper()} · {g['date']:%a %b} {g['date'].day}</div>
<div class=m>{html.escape(g['away']['display'])} <span>@</span> {html.escape(g['home']['display'])}</div>
<div class=s>{stars(g['score'])}</div>
<div class=r>{html.escape(' · '.join(g['reasons']))}</div></a>""")
    return f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Highlight Picks</title><style>
body{{font-family:system-ui,sans-serif;background:#111;color:#eee;margin:0;padding:24px;max-width:640px;margin:auto}}
.card{{display:block;background:#1d1d1f;border-radius:14px;padding:18px;margin:14px 0;color:inherit;text-decoration:none}}
.lg{{font-size:13px;color:#999}} .m{{font-size:21px;font-weight:600;margin:6px 0}} .m span{{color:#777}}
.s{{color:#f5b400;letter-spacing:2px}} .r{{font-size:14px;color:#bbb;margin-top:6px}}</style>
<h2>Highlight picks · {dt.date.today():%b} {dt.date.today().day}</h2>{''.join(cards) or '<p>Nothing worth your time.</p>'}"""


def main():
    ap = argparse.ArgumentParser(description="Spoiler-free highlight picks")
    ap.add_argument("--nba", type=int, default=None, help="how many NBA games")
    ap.add_argument("--nfl", type=int, default=None, help="how many NFL games")
    ap.add_argument("--minutes", type=int, default=None, help="fill this much time instead of fixed counts")
    ap.add_argument("--days", type=int, default=None, help="look back this many days (default from config)")
    ap.add_argument("--html", action="store_true", help="also write picks.html")
    ap.add_argument("--json", action="store_true", help="print spoiler-safe JSON (for Claude)")
    ap.add_argument("--debug", action="store_true", help="show component scores (no game scores)")
    ap.add_argument("--request", help="read nba/nfl/minutes/days from this JSON file (GitHub runs)")
    ap.add_argument("--out", help="write spoiler-safe JSON results to this file")
    args = ap.parse_args()

    request = {}
    if args.request and Path(args.request).exists():
        request = json.loads(Path(args.request).read_text(encoding="utf-8"))
        for k in ("nba", "nfl", "minutes", "days"):
            if request.get(k) not in (None, "", 0) and getattr(args, k) is None:
                setattr(args, k, int(request[k]))

    cfg = tomllib.loads((HERE / "config.toml").read_text(encoding="utf-8"))
    global LOCAL_TZ
    try:
        from zoneinfo import ZoneInfo
        LOCAL_TZ = ZoneInfo(cfg.get("timezone", "America/Denver"))
    except Exception:
        LOCAL_TZ = None
    days = args.days or cfg["scoring"].get("days_back", 7)
    want = {k: v for k, v in (("nba", args.nba), ("nfl", args.nfl)) if v}
    if args.minutes and not want:
        want = {"nba": 1, "nfl": 1}
    if not want:
        want = {"nba": 1, "nfl": 1}

    picks = pick(cfg, want, args.minutes, days, args.debug)

    safe = [{
        "league": g["league"].upper(),
        "matchup": f"{g['away']['display']} @ {g['home']['display']}",
        "date": f"{g['date']:%a %b} {g['date'].day}",
        "rating": stars(g["score"]),
        "why": g["reasons"],
        "url": g["url"],
        "direct_link": g["direct"],
    } for g in picks]
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps({
            "request_id": request.get("id"),
            "generated_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "asked": {"nba": args.nba, "nfl": args.nfl, "minutes": args.minutes, "days": days},
            "picks": safe,
        }, indent=2, ensure_ascii=False), encoding="utf-8")
    if args.json:
        print(json.dumps(safe, indent=2, ensure_ascii=False))
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(render_text(picks, args.debug))
    if args.html:
        out = HERE / "picks.html"
        out.write_text(render_html(picks), encoding="utf-8")
        print(f"\nSaved {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
