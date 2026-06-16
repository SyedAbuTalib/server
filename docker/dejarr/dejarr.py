#!/usr/bin/env python3
"""Dejarr - weekly AI media picks with a Trakt feedback loop."""
import html
import json
import os
import re
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SONARR_URL = os.environ.get("SONARR_URL", "http://sonarr:8989")
SONARR_KEY = os.environ["SONARR_API_KEY"]
RADARR_URL = os.environ.get("RADARR_URL", "http://radarr:7878")
RADARR_KEY = os.environ["RADARR_API_KEY"]
GEMINI_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
DISCORD_URL = os.environ["DISCORD_WEBHOOK_URL"]

TRAKT_CLIENT_ID = os.environ["TRAKT_CLIENT_ID"]
TRAKT_CLIENT_SECRET = os.environ["TRAKT_CLIENT_SECRET"]
TRAKT_INITIAL_REFRESH = os.environ["TRAKT_REFRESH_TOKEN"]

JELLYSEERR_URL = os.environ.get("JELLYSEERR_URL", "http://jellyseerr:5055")
JELLYSEERR_KEY = os.environ["JELLYSEERR_API_KEY"]
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w342"

STATE_DIR = Path(os.environ.get("STATE_DIR", "/state"))
TOKEN_FILE = STATE_DIR / "trakt.json"
RECS_DIR = STATE_DIR / "recs"
EXCLUSIONS_FILE = STATE_DIR / "exclusions.json"

HTTP_PORT = int(os.environ.get("HTTP_PORT", "8000"))
SCHEDULE_DAY = int(os.environ.get("SCHEDULE_DAY", "4"))  # 0=Mon, 4=Fri
SCHEDULE_HOUR = int(os.environ.get("SCHEDULE_HOUR", "18"))

_lock = threading.Lock()
_status = {"running": False, "last_run": None, "last_error": None}


def http(url, headers=None, data=None, method=None, timeout=60):
    h = {"User-Agent": "dejarr/1.0"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def trakt_refresh():
    refresh_token = TRAKT_INITIAL_REFRESH
    if TOKEN_FILE.exists():
        try:
            refresh_token = json.loads(TOKEN_FILE.read_text()).get("refresh_token", refresh_token)
        except Exception:
            pass
    body = json.dumps({
        "refresh_token": refresh_token,
        "client_id": TRAKT_CLIENT_ID,
        "client_secret": TRAKT_CLIENT_SECRET,
        "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
        "grant_type": "refresh_token",
    }).encode()
    tokens = json.loads(http(
        "https://api.trakt.tv/oauth/token",
        {"Content-Type": "application/json"},
        body, "POST"
    ))
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(tokens))
    return tokens["access_token"]


def trakt_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "trakt-api-version": "2",
        "trakt-api-key": TRAKT_CLIENT_ID,
        "Content-Type": "application/json",
    }


def trakt_get(path, token):
    return json.loads(http(f"https://api.trakt.tv{path}", trakt_headers(token)))


def _norm_title(s):
    """Lowercase, drop non-alphanumerics (keep spaces), collapse whitespace.
    'Parasyte: The Maxim' and 'Parasyte -the maxim-' both → 'parasyte the maxim'."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", s.lower())).strip()


def trakt_mark_watched(token, kind, title, year=None):
    """kind: 'show' or 'movie'. Searches title-only, picks the best match, POSTs to /sync/history."""
    # Sanitize query: Trakt search treats leading '-' as exclusion, so strip punctuation
    # before sending. Normalized comparison below handles the matching.
    q = urllib.parse.quote(_norm_title(title))
    search_type = "show" if kind == "show" else "movie"
    path = f"/search/{search_type}?query={q}&fields=title&limit=10"
    if year:
        path += f"&years={year}"
    results = trakt_get(path, token)
    if not results:
        return False, f"no trakt match for '{title}'"
    title_norm = _norm_title(title)
    try:
        year_int = int(year) if year else None
    except (ValueError, TypeError):
        year_int = None
    hit = None
    for r in results:
        item = r.get(search_type, {})
        if _norm_title(item.get("title", "")) == title_norm:
            if year_int and item.get("year") == year_int:
                hit = item
                break
            if hit is None:
                hit = item
    if hit is None:
        first = results[0].get(search_type, {})
        return False, f"no exact title match for '{title}' (top result: '{first.get('title')}' ({first.get('year')}))"
    payload_key = "shows" if kind == "show" else "movies"
    body = json.dumps({payload_key: [{"ids": hit["ids"], "watched_at": "released"}]}).encode()
    resp = json.loads(http("https://api.trakt.tv/sync/history", trakt_headers(token), body, "POST"))
    added = resp.get("added", {})
    not_found = resp.get("not_found", {}).get(payload_key, [])
    resolved = hit.get("title", title)
    if kind == "show":
        count = added.get("episodes", 0) + added.get("seasons", 0) + added.get("shows", 0)
    else:
        count = added.get("movies", 0)
    if count > 0:
        suffix = f" ({count} episodes)" if kind == "show" else ""
        return True, f"marked '{resolved}' watched on Trakt{suffix}"
    if not not_found:
        return True, f"'{resolved}' was already on Trakt"
    return False, f"could not mark '{resolved}': added={added}, not_found={not_found}"


def _jellyseerr_query(query_str):
    """Single search call. Returns the parsed response, or None on error."""
    try:
        return json.loads(http(
            f"{JELLYSEERR_URL}/api/v1/search?query={urllib.parse.quote(query_str)}",
            {"X-Api-Key": JELLYSEERR_KEY},
        ))
    except Exception as e:
        print(f"  jellyseerr search '{query_str}' failed: {e}", flush=True)
        return None


def jellyseerr_search(query, kind):
    """Return (tmdb_id, poster_path) for the best match, or (None, None)."""
    media_type = "tv" if kind == "show" else "movie"
    resp = _jellyseerr_query(query)
    # Jellyseerr rejects some punctuation (e.g. URL-encoded slash in "Fate/Zero")
    # with HTTP 400; retry with punctuation stripped.
    sanitized = normalize_title(query)
    if (resp is None or not resp.get("results")) and sanitized and sanitized != query.lower().strip():
        resp = _jellyseerr_query(sanitized)
    if resp is None:
        return None, None
    target = normalize_title(query)
    best = None
    for r in resp.get("results", []):
        if r.get("mediaType") != media_type:
            continue
        cand_title = r.get("title") or r.get("name") or r.get("originalTitle") or r.get("originalName") or ""
        if normalize_title(cand_title) == target:
            return r.get("id"), r.get("posterPath")
        if best is None:
            best = r
    if best:
        return best.get("id"), best.get("posterPath")
    return None, None


def jellyseerr_request(kind, tmdb_id):
    media_type = "tv" if kind == "show" else "movie"
    body = {"mediaType": media_type, "mediaId": int(tmdb_id)}
    if kind == "show":
        body["seasons"] = "all"
    data = json.dumps(body).encode()
    return json.loads(http(
        f"{JELLYSEERR_URL}/api/v1/request",
        {"X-Api-Key": JELLYSEERR_KEY, "Content-Type": "application/json"},
        data, "POST"
    ))


def enrich_picks(picks):
    """Resolve TMDB id + poster path for each pick using Jellyseerr search."""
    for s in picks.get("shows", []):
        tmdb_id, poster = jellyseerr_search(s.get("title", ""), "show")
        s["tmdb_id"] = tmdb_id
        s["poster_path"] = poster
    for m in picks.get("movies", []):
        tmdb_id, poster = jellyseerr_search(m.get("title", ""), "movie")
        m["tmdb_id"] = tmdb_id
        m["poster_path"] = poster
    return picks


def fetch_library():
    series = json.loads(http(f"{SONARR_URL}/api/v3/series", {"X-Api-Key": SONARR_KEY}))
    movies = json.loads(http(f"{RADARR_URL}/api/v3/movie", {"X-Api-Key": RADARR_KEY}))
    return (
        sorted({s["title"] for s in series}),
        sorted({m["title"] for m in movies}),
    )


def fetch_trakt_history(token):
    watched_shows = trakt_get("/sync/watched/shows?extended=noseasons", token)
    watched_movies = trakt_get("/sync/watched/movies", token)
    shows = sorted({w["show"]["title"] for w in watched_shows})
    movies = sorted({w["movie"]["title"] for w in watched_movies})
    history = trakt_get("/sync/history?limit=40", token)
    recent, seen = [], set()
    for h in history:
        if h.get("type") == "episode":
            t = h.get("show", {}).get("title", "")
        elif h.get("type") == "movie":
            t = h.get("movie", {}).get("title", "")
        else:
            t = ""
        if t and t not in seen:
            seen.add(t)
            recent.append(t)
    return shows, movies, recent


def load_exclusions():
    if EXCLUSIONS_FILE.exists():
        try:
            return json.loads(EXCLUSIONS_FILE.read_text()).get("not_interested", [])
        except Exception:
            return []
    return []


def save_exclusions(items):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    EXCLUSIONS_FILE.write_text(json.dumps({"not_interested": items}, indent=2))


def ask_gemini(owned_shows, owned_movies, watched_shows, watched_movies, recent, excluded):
    today = datetime.now().strftime("%Y-%m-%d")
    blocked_shows = sorted(set(watched_shows) | set(owned_shows) | set(excluded))
    blocked_movies = sorted(set(watched_movies) | set(owned_movies) | set(excluded))
    prompt = f"""You are a media recommendation assistant. Today's date is {today}.

User's recently watched (most recent first, from Trakt):
{', '.join(recent[:25]) if recent else '(no recent activity)'}

HARD BLOCKLIST - TV shows you MUST NOT recommend (already watched, owned, or rejected):
{', '.join(blocked_shows) if blocked_shows else '(none)'}

HARD BLOCKLIST - Movies you MUST NOT recommend (already watched, owned, or rejected):
{', '.join(blocked_movies) if blocked_movies else '(none)'}

TASK:
- Recommend exactly 10 TV shows and 10 movies the user will enjoy based on the recently watched list, ordered best-fit first.
- HARD RULE: every pick must NOT appear in the blocklists above. Re-check each title against the blocklists before responding.
- HARD RULE: every pick MUST already be released and publicly available to watch as of {today}. No upcoming, announced, or unreleased titles. If uncertain, skip it.
- Each pick gets one short sentence connecting it to the user's taste.
"""
    schema = {
        "type": "object",
        "properties": {
            "shows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "year": {"type": "integer"},
                        "reason": {"type": "string"},
                    },
                    "required": ["title", "year", "reason"],
                },
            },
            "movies": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "year": {"type": "integer"},
                        "reason": {"type": "string"},
                    },
                    "required": ["title", "year", "reason"],
                },
            },
        },
        "required": ["shows", "movies"],
    }
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.85,
            "maxOutputTokens": 8000,
            "responseMimeType": "application/json",
            "responseSchema": schema,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }).encode()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}"
    resp = json.loads(http(url, {"Content-Type": "application/json"}, body, "POST"))
    text = resp["candidates"][0]["content"]["parts"][0]["text"].strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print(f"  gemini raw (first 400): {text[:400]!r}", flush=True)
        raise


def normalize_title(t):
    """Lower, strip punctuation, collapse whitespace. For loose matching."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (t or "").lower())).strip()


def scrub_picks(picks, owned_shows, owned_movies, watched_shows, watched_movies, excluded):
    """Drop any pick whose normalized title appears in the blocklists."""
    blocked_shows = {normalize_title(t) for t in (owned_shows + watched_shows + excluded)}
    blocked_movies = {normalize_title(t) for t in (owned_movies + watched_movies + excluded)}
    dropped = []
    kept_shows = []
    for s in picks.get("shows", []):
        if normalize_title(s.get("title", "")) in blocked_shows:
            dropped.append(s.get("title"))
        else:
            kept_shows.append(s)
    kept_movies = []
    for m in picks.get("movies", []):
        if normalize_title(m.get("title", "")) in blocked_movies:
            dropped.append(m.get("title"))
        else:
            kept_movies.append(m)
    if dropped:
        print(f"  scrubbed {len(dropped)} blocked picks: {dropped}", flush=True)
    return {"shows": kept_shows, "movies": kept_movies}


def format_for_discord(picks):
    lines = ["**TV SHOWS**"]
    for i, s in enumerate(picks["shows"], 1):
        lines.append(f"{i}. {s['title']} ({s['year']}) - {s['reason']}")
    lines.append("")
    lines.append("**MOVIES**")
    for i, m in enumerate(picks["movies"], 1):
        lines.append(f"{i}. {m['title']} ({m['year']}) - {m['reason']}")
    return "\n".join(lines)


def post_to_discord(text):
    payload = json.dumps({
        "username": "Dejarr",
        "embeds": [{
            "title": "Weekly Picks",
            "description": text[:4000],
            "color": 0x5865F2,
            "footer": {"text": f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}"},
        }],
    }).encode()
    http(DISCORD_URL, {"Content-Type": "application/json"}, payload, "POST")


def run_cycle(notify=True):
    """One full recommendation cycle. Returns (timestamp, picks) on success."""
    with _lock:
        if _status["running"]:
            return None
        _status["running"] = True
    try:
        print(f"[{datetime.now().isoformat()}] cycle start", flush=True)
        token = trakt_refresh()
        owned_shows, owned_movies = fetch_library()
        watched_shows, watched_movies, recent = fetch_trakt_history(token)
        excluded = load_exclusions()
        print(f"  lib {len(owned_shows)}s/{len(owned_movies)}m, watched {len(watched_shows)}s/{len(watched_movies)}m, excl {len(excluded)}", flush=True)
        picks = ask_gemini(owned_shows, owned_movies, watched_shows, watched_movies, recent, excluded)
        picks = scrub_picks(picks, owned_shows, owned_movies, watched_shows, watched_movies, excluded)
        picks = enrich_picks(picks)
        ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        RECS_DIR.mkdir(parents=True, exist_ok=True)
        (RECS_DIR / f"{ts}.json").write_text(json.dumps(picks, indent=2))
        if notify:
            post_to_discord(format_for_discord(picks))
        _status["last_run"] = ts
        _status["last_error"] = None
        print(f"  cycle done: {ts}", flush=True)
        return ts, picks
    except Exception as e:
        _status["last_error"] = f"{type(e).__name__}: {e}"
        if isinstance(e, urllib.error.HTTPError):
            try:
                _status["last_error"] += " - " + e.read().decode("utf-8", "ignore")[:200]
            except Exception:
                pass
        traceback.print_exc()
        return None
    finally:
        _status["running"] = False


def latest_recs():
    if not RECS_DIR.exists():
        return None, None
    files = sorted(RECS_DIR.glob("*.json"), reverse=True)
    if not files:
        return None, None
    return files[0].stem, json.loads(files[0].read_text())


def all_recs():
    if not RECS_DIR.exists():
        return []
    return sorted([f.stem for f in RECS_DIR.glob("*.json")], reverse=True)


def get_recs(ts):
    f = RECS_DIR / f"{ts}.json"
    if not f.exists():
        return None
    return json.loads(f.read_text())


def dismissed_path(ts):
    return RECS_DIR / f"{ts}.dismissed.json"


def load_dismissed(ts):
    f = dismissed_path(ts)
    if not f.exists():
        return set()
    try:
        return set(json.loads(f.read_text()))
    except Exception:
        return set()


def add_dismissed(ts, title):
    items = load_dismissed(ts)
    items.add(title)
    dismissed_path(ts).write_text(json.dumps(sorted(items)))


def filter_picks(picks, ts):
    dismissed = load_dismissed(ts)
    return {
        "shows": [s for s in picks.get("shows", []) if s.get("title") not in dismissed],
        "movies": [m for m in picks.get("movies", []) if m.get("title") not in dismissed],
    }


PAGE_CSS = """
:root { --bg:#0f1115; --card:#181b22; --text:#e6e6e6; --muted:#9aa3b2;
        --accent:#5865F2; --good:#3ba55d; --bad:#ed4245; --warn:#faa61a; }
* { box-sizing: border-box; }
body { background: var(--bg); color: var(--text); font-family: system-ui, sans-serif;
       margin: 0; padding: 2rem; max-width: 980px; margin-inline: auto; }
h1 { margin: 0 0 .25rem; }
header { display:flex; align-items:baseline; gap:1rem; flex-wrap:wrap;
         border-bottom:1px solid #2a2f3a; padding-bottom:1rem; margin-bottom:1.5rem; }
nav a { color: var(--muted); text-decoration:none; margin-right:1rem; }
nav a:hover { color: var(--text); }
.muted { color: var(--muted); font-size: .9rem; }
.card { background: var(--card); border-radius: 10px; padding: 1rem 1.25rem;
        margin-bottom: .75rem; border: 1px solid #232733; }
.card h3 { margin: 0 0 .25rem; font-size: 1.05rem; }
.card p { margin: .25rem 0 .75rem; color: var(--muted); }
.row { display:flex; gap:.5rem; flex-wrap:wrap; }
button, .btn { background:#2a2f3a; color:var(--text); border:none;
               padding:.45rem .85rem; border-radius:6px; cursor:pointer;
               font-size:.85rem; font-family:inherit; }
button.seen { background: var(--good); }
button.skip { background: var(--bad); }
button.run  { background: var(--accent); }
button.add  { background: var(--warn); color:#111; }
.card.pick { display: flex; gap: 1rem; align-items: flex-start; }
.card.pick .poster { width: 110px; flex-shrink: 0; border-radius: 6px; aspect-ratio: 2/3; object-fit: cover; }
.card.pick .meta { flex: 1; min-width: 0; }
@media (max-width: 600px) { .card.pick { flex-direction: column; } .card.pick .poster { width: 100%; max-width: 220px; } }
button:hover { filter: brightness(1.15); }
form { display: inline; }
.section { font-size:.8rem; color:var(--muted); text-transform:uppercase;
           letter-spacing:.08em; margin: 1.5rem 0 .5rem; }
.flash { background:#1d2530; border-left:3px solid var(--accent);
         padding:.5rem .75rem; margin-bottom:1rem; border-radius:4px; }
.flash.err { border-left-color: var(--bad); }
ul.history { list-style:none; padding:0; }
ul.history li { padding:.4rem 0; border-bottom:1px solid #232733; }
ul.history a { color: var(--text); text-decoration:none; }
ul.history a:hover { color: var(--accent); }
ul.watched { list-style:none; padding:0; columns: 2; column-gap: 2rem; }
ul.watched li { padding:.25rem 0; break-inside: avoid; }
@media (max-width: 600px) { ul.watched { columns: 1; } }
"""


def render(title, body, flash=None, flash_kind="ok"):
    flash_html = ""
    if flash:
        cls = "flash" + (" err" if flash_kind == "err" else "")
        flash_html = f'<div class="{cls}">{html.escape(flash)}</div>'
    last = _status.get("last_run") or "never"
    err = _status.get("last_error")
    err_html = f'<span class="muted"> · last error: {html.escape(err)}</span>' if err else ""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{html.escape(title)} - Dejarr</title>
<style>{PAGE_CSS}</style></head><body>
<header>
  <h1><a href="/" style="color:inherit;text-decoration:none">Dejarr</a></h1>
  <nav><a href="/">Picks</a><a href="/history">Archive</a><a href="/watched">Watched</a></nav>
  <span class="muted">last run: {html.escape(last)}{err_html}</span>
</header>
{flash_html}
{body}
</body></html>"""


def render_card(item, kind, ts):
    title = html.escape(item.get("title", ""))
    year = item.get("year", "")
    reason = html.escape(item.get("reason", ""))
    tmdb_id = item.get("tmdb_id")
    poster = item.get("poster_path")
    poster_html = ""
    if poster:
        poster_html = f'<img class="poster" src="{TMDB_IMAGE_BASE}{html.escape(poster)}" alt="" loading="lazy">'
    add_html = ""
    if tmdb_id:
        add_html = f"""<form method="post" action="/add">
          <input type="hidden" name="tmdb_id" value="{tmdb_id}">
          <input type="hidden" name="kind" value="{kind}">
          <input type="hidden" name="title" value="{title}">
          <input type="hidden" name="ts" value="{ts}">
          <button class="add" type="submit">+ Add to library</button>
        </form>"""
    return f"""<div class="card pick">
      {poster_html}
      <div class="meta">
        <h3>{title} <span class="muted">({year})</span></h3>
        <p>{reason}</p>
        <div class="row">
          <form method="post" action="/seen">
            <input type="hidden" name="title" value="{title}">
            <input type="hidden" name="year" value="{year}">
            <input type="hidden" name="kind" value="{kind}">
            <input type="hidden" name="ts" value="{ts}">
            <button class="seen" type="submit">✓ Seen it</button>
          </form>
          <form method="post" action="/not-interested">
            <input type="hidden" name="title" value="{title}">
            <input type="hidden" name="ts" value="{ts}">
            <button class="skip" type="submit">✗ Not interested</button>
          </form>
          {add_html}
        </div>
      </div>
    </div>"""


def render_picks(picks, ts):
    visible = filter_picks(picks, ts)
    parts = []
    if visible["shows"]:
        parts.append('<div class="section">TV Shows</div>')
        for s in visible["shows"]:
            parts.append(render_card(s, "show", ts))
    if visible["movies"]:
        parts.append('<div class="section">Movies</div>')
        for m in visible["movies"]:
            parts.append(render_card(m, "movie", ts))
    if not parts:
        parts.append('<div class="card"><p>All picks from this batch dismissed.</p></div>')
    return "\n".join(parts)


def render_index(flash=None, flash_kind="ok"):
    ts, picks = latest_recs()
    expecting_run = _status["running"] or (flash or "").startswith("run started")
    poll_js = render_poll_js(ts) if expecting_run else ""
    running_banner = '<div class="flash">Generating new batch... page will refresh when done.</div>' if expecting_run else ""
    if not picks:
        body = f"""{running_banner}<div class="card"><p>No picks yet.</p>
          <form method="post" action="/run-now">
            <button class="run" type="submit">Run now</button>
          </form></div>{poll_js}"""
        return render("Picks", body, flash, flash_kind)
    cards = render_picks(picks, ts)
    body = f"""{running_banner}<form method="post" action="/run-now" style="margin-bottom:1rem">
      <button class="run" type="submit">Run a new batch</button>
      <span class="muted">picks from {html.escape(ts)}</span>
    </form>
    {cards}{poll_js}"""
    return render("Picks", body, flash, flash_kind)


def render_poll_js(baseline_ts):
    baseline = json.dumps(baseline_ts or "")
    return f"""<script>
(function() {{
  const baseline = {baseline};
  async function tick() {{
    try {{
      const r = await fetch('/status', {{cache: 'no-store'}}).then(r => r.json());
      if (!r.running && r.last_run && r.last_run !== baseline) {{
        location.replace('/');
        return;
      }}
    }} catch (e) {{}}
    setTimeout(tick, 2000);
  }}
  setTimeout(tick, 2000);
}})();
</script>"""


def render_history():
    items = all_recs()
    if not items:
        return render("Archive", '<div class="card"><p>No history yet.</p></div>')
    lis = "\n".join(f'<li><a href="/history/{html.escape(ts)}">{html.escape(ts)}</a></li>' for ts in items)
    return render("Archive", f'<ul class="history">{lis}</ul>')


def render_watched():
    try:
        token = trakt_refresh()
        shows, movies, _ = fetch_trakt_history(token)
    except Exception as e:
        return render("Watched", f'<div class="card"><p>Trakt error: {html.escape(str(e))}</p></div>')
    show_lis = "\n".join(f"<li>{html.escape(t)}</li>" for t in shows) or "<li>(none)</li>"
    movie_lis = "\n".join(f"<li>{html.escape(t)}</li>" for t in movies) or "<li>(none)</li>"
    body = f"""<div class="section">TV Shows ({len(shows)})</div>
<ul class="watched">{show_lis}</ul>
<div class="section">Movies ({len(movies)})</div>
<ul class="watched">{movie_lis}</ul>"""
    return render("Watched", body)


def render_history_entry(ts):
    picks = get_recs(ts)
    if not picks:
        return None
    body = f'<p class="muted">Picks from {html.escape(ts)}</p>' + render_picks(picks, ts)
    return render(ts, body)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[http] {self.address_string()} {fmt % args}", flush=True)

    def _send(self, status, body, content_type="text/html; charset=utf-8"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _redirect(self, location):
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def _form(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode() if length else ""
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        flash = qs.get("flash", [None])[0]
        flash_kind = qs.get("kind", ["ok"])[0]
        if path == "/":
            return self._send(200, render_index(flash, flash_kind))
        if path == "/history":
            return self._send(200, render_history())
        if path == "/watched":
            return self._send(200, render_watched())
        if path.startswith("/history/"):
            ts = path[len("/history/"):]
            page = render_history_entry(ts)
            if page is None:
                return self._send(404, render("Not found", '<div class="card"><p>No such run.</p></div>'))
            return self._send(200, page)
        if path == "/healthz":
            if not TOKEN_FILE.exists():
                return self._send(503, "no trakt token", "text/plain")
            try:
                json.loads(TOKEN_FILE.read_text())
            except Exception as e:
                return self._send(503, f"trakt token invalid: {e}", "text/plain")
            err = _status.get("last_error") or ""
            if err and ("trakt" in err.lower() or "401" in err):
                return self._send(503, f"recent trakt error: {err}", "text/plain")
            return self._send(200, "ok", "text/plain")
        if path == "/status":
            return self._send(200, json.dumps({
                "running": _status["running"],
                "last_run": _status["last_run"],
            }), "application/json")
        return self._send(404, render("Not found", '<div class="card"><p>Not found.</p></div>'))

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        form = self._form()
        if path == "/seen":
            return self._handle_seen(form)
        if path == "/not-interested":
            return self._handle_not_interested(form)
        if path == "/add":
            return self._handle_add(form)
        if path == "/run-now":
            return self._handle_run_now()
        return self._send(404, "not found", "text/plain")

    def _handle_seen(self, form):
        title = form.get("title", "").strip()
        year = form.get("year", "").strip() or None
        kind = form.get("kind", "").strip()
        if not title or kind not in ("show", "movie"):
            return self._redirect("/?flash=missing+fields&kind=err")
        ts = form.get("ts", "").strip()
        try:
            token = trakt_refresh()
            ok, msg = trakt_mark_watched(token, kind, title, year)
            if ok and ts:
                add_dismissed(ts, title)
            kind_qs = "" if ok else "&kind=err"
            return self._redirect("/?flash=" + urllib.parse.quote(msg) + kind_qs)
        except Exception as e:
            return self._redirect("/?flash=" + urllib.parse.quote(f"trakt error: {e}") + "&kind=err")

    def _handle_not_interested(self, form):
        title = form.get("title", "").strip()
        ts = form.get("ts", "").strip()
        if not title:
            return self._redirect("/?flash=no+title&kind=err")
        items = load_exclusions()
        if title not in items:
            items.append(title)
            save_exclusions(items)
        if ts:
            add_dismissed(ts, title)
        return self._redirect("/?flash=" + urllib.parse.quote(f"'{title}' added to exclusions"))

    def _handle_add(self, form):
        tmdb_id = form.get("tmdb_id", "").strip()
        kind = form.get("kind", "").strip()
        title = form.get("title", "").strip()
        ts = form.get("ts", "").strip()
        if not tmdb_id or kind not in ("show", "movie"):
            return self._redirect("/?flash=missing+fields&kind=err")
        try:
            jellyseerr_request(kind, tmdb_id)
            if ts:
                add_dismissed(ts, title)
            return self._redirect("/?flash=" + urllib.parse.quote(f"requested '{title}' via Jellyseerr"))
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", "ignore")[:200]
            if e.code == 409:
                if ts:
                    add_dismissed(ts, title)
                return self._redirect("/?flash=" + urllib.parse.quote(f"'{title}' already requested or in library"))
            return self._redirect("/?flash=" + urllib.parse.quote(f"jellyseerr {e.code}: {body_text}") + "&kind=err")
        except Exception as e:
            return self._redirect("/?flash=" + urllib.parse.quote(f"jellyseerr error: {e}") + "&kind=err")

    def _handle_run_now(self):
        if _status["running"]:
            return self._redirect("/?flash=already+running&kind=err")
        threading.Thread(target=run_cycle, kwargs={"notify": False}, daemon=True).start()
        return self._redirect("/?flash=run+started")


def next_scheduled():
    now = datetime.now()
    days_ahead = (SCHEDULE_DAY - now.weekday()) % 7
    candidate = now.replace(hour=SCHEDULE_HOUR, minute=0, second=0, microsecond=0) + timedelta(days=days_ahead)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def scheduler_loop():
    print(f"[sched] next run: {next_scheduled().isoformat()}", flush=True)
    while True:
        try:
            target = next_scheduled()
            sleep_s = max(30, (target - datetime.now()).total_seconds())
            time.sleep(min(sleep_s, 3600))
            now = datetime.now()
            if now.weekday() == SCHEDULE_DAY and now.hour == SCHEDULE_HOUR:
                last = _status.get("last_run") or ""
                today_tag = now.strftime("%Y-%m-%d")
                if not last.startswith(today_tag):
                    print(f"[sched] firing weekly run at {now.isoformat()}", flush=True)
                    run_cycle(notify=True)
        except Exception:
            traceback.print_exc()
            time.sleep(60)


def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    RECS_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=scheduler_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler)
    print(f"[http] listening on :{HTTP_PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
