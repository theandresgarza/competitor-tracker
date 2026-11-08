"""Build view/index.html from studios.json and the collected sessions.

Shows every studio we know about (tracked or not, and why not), then for tracked
studios: visits and how full sessions were, per location, for the last 7 days and
per week. "Visits" = places booked at the last reading before each session started.
"""

import csv
import datetime as dt
import html
import json
import pathlib
from collections import defaultdict

ROOT = pathlib.Path(__file__).resolve().parent
DATA = ROOT / "data"
VIEW = ROOT / "view"


def esc(x):
    return html.escape(str(x if x is not None else ""))


def num(n):
    return f"{round(n):,}"


def load_sessions():
    rows = []
    for path in sorted(DATA.glob("sessions-*.csv")):
        with path.open(newline="") as f:
            for r in csv.DictReader(f):
                r["start"] = dt.datetime.fromisoformat(r["start_utc"].replace("Z", "+00:00"))
                for k in ("capacity", "booked", "waitlist", "minutes_before_start"):
                    r[k] = int(r[k])
                rows.append(r)
    return rows


def summarize(rows):
    cap = sum(r["capacity"] for r in rows)
    booked = sum(r["booked"] for r in rows)
    full = sum(1 for r in rows if r["booked"] >= r["capacity"] or r["status"] in ("Waitlist Only", "Class Full"))
    return {"sessions": len(rows), "capacity": cap, "visits": booked, "fill": booked / cap if cap else 0, "full": full}


def money(visits, price):
    if not price:
        return "price not set"
    lo, hi = price if isinstance(price, list) else (price, price)
    return f"CA${num(visits * lo)}–{num(visits * hi)}"


def main():
    VIEW.mkdir(exist_ok=True)
    studios = json.loads((ROOT / "studios.json").read_text())["studios"]
    rows = load_sessions()
    now = dt.datetime.now(dt.timezone.utc)
    last_run = {}
    if (DATA / "last-run.json").exists():
        last_run = json.loads((DATA / "last-run.json").read_text())

    done = [r for r in rows if r["start"] <= now]
    week_ago = now - dt.timedelta(days=7)
    by_studio = defaultdict(list)
    for r in done:
        by_studio[r["studio"]].append(r)
    first_day = min((r["start"] for r in done), default=None)

    # ---------- every studio ----------
    status_rows = []
    for s in studios:
        tracked = s.get("track") == "marianatek"
        err = (last_run.get("studios", {}).get(s["id"]) or {}).get("error")
        if tracked and err:
            badge, why = "Error", f"Last run failed: {err}"
        elif tracked:
            badge, why = "Tracking", "Public schedule shows places and spots left before each session"
        else:
            badge = {"later": "Not yet", "cannot": "Can't", "not_open": "Not open"}.get(s.get("track"), "Not yet")
            why = s.get("reason", "")
        status_rows.append(
            f"<tr><td><b>{esc(s['name'])}</b><div class='small'>{esc(s.get('city'))}</div></td>"
            f"<td><a href='{esc(s.get('website'))}'>{esc((s.get('website') or '').replace('https://', '').replace('www.', ''))}</a></td>"
            f"<td>{esc(s.get('platform'))}</td><td><span class='badge {badge.lower().replace(chr(39), '').replace(' ', '-')}'>{esc(badge)}</span></td>"
            f"<td class='small'>{esc(why)}{('<br>' + esc(s['note'])) if s.get('note') else ''}</td></tr>"
        )

    # ---------- last 7 days, per location ----------
    week_rows = []
    for s in studios:
        if s.get("track") != "marianatek":
            continue
        recent = [r for r in by_studio.get(s["id"], []) if r["start"] >= week_ago]
        locs = defaultdict(list)
        for r in recent:
            locs[r["location"]].append(r)
        if not locs:
            week_rows.append(f"<tr><td><b>{esc(s['name'])}</b></td><td colspan='7' class='small'>No finished sessions recorded yet</td></tr>")
            continue
        for loc, rs in sorted(locs.items()):
            m = summarize(rs)
            days = max(1, len({r["start"].date() for r in rs}))
            week_rows.append(
                f"<tr><td><b>{esc(s['name'])}</b><div class='small'>{esc(loc)}</div></td><td class='r'>{num(m['sessions'])}</td>"
                f"<td class='r'>{num(m['capacity'])}</td><td class='r'><b>{num(m['visits'])}</b></td><td class='r'>{num(m['visits'] / days)}</td>"
                f"<td class='r'>{m['fill']:.0%}</td><td class='r'>{m['full']}</td><td class='r'>{esc(money(m['visits'], s.get('price_per_visit_cad')))}</td></tr>"
            )

    # ---------- by week ----------
    weekly = defaultdict(lambda: defaultdict(list))
    for r in done:
        monday = (r["start"] - dt.timedelta(days=r["start"].weekday())).date()
        weekly[monday][(r["studio"], r["location"])].append(r)
    names = {s["id"]: s["name"] for s in studios}
    wk_rows = []
    for monday in sorted(weekly, reverse=True):
        for (sid, loc), rs in sorted(weekly[monday].items()):
            m = summarize(rs)
            wk_rows.append(f"<tr><td>{monday:%d %b %Y}</td><td>{esc(names.get(sid, sid))} · {esc(loc)}</td><td class='r'>{num(m['sessions'])}</td><td class='r'><b>{num(m['visits'])}</b></td><td class='r'>{m['fill']:.0%}</td></tr>")

    # ---------- upcoming, per location (shows every location on the schedule, incl. new ones) ----------
    soon = defaultdict(list)
    for r in rows:
        if now < r["start"] <= now + dt.timedelta(days=3):
            soon[(r["studio"], r["location"])].append(r)
    up_rows = []
    for (sid, loc), rs in sorted(soon.items(), key=lambda kv: (names.get(kv[0][0], ""), kv[0][1])):
        m = summarize(rs)
        up_rows.append(f"<tr><td>{esc(names.get(sid, sid))}</td><td>{esc(loc)}</td><td class='r'>{num(m['sessions'])}</td><td class='r'>{num(m['capacity'])}</td><td class='r'>{num(m['visits'])}</td><td class='r'>{m['fill']:.0%}</td></tr>")

    tracked_n = sum(1 for s in studios if s.get("track") == "marianatek")
    since = f"Data since {first_day:%d %b %Y}." if first_day else "No finished sessions yet."
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Studio Tracker</title>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif&family=Manrope:wght@400;600;700&display=swap" rel="stylesheet">
<style>
:root {{ --navy:#173b57; --accent:#2785b3; --bg:#f5fbff; --line:#d9e9f3; --muted:#5a7489; --hl:#e8f6fd; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--navy); font:15px/1.5 Manrope, system-ui, sans-serif; font-variant-numeric:tabular-nums; }}
main {{ max-width:1160px; margin:0 auto; padding:32px 16px 64px; }}
h1, h2 {{ font-family:'Instrument Serif', Georgia, serif; font-weight:400; margin:0; }}
h1 {{ font-size:40px; }} h2 {{ font-size:26px; margin-top:32px; }}
a {{ color:var(--accent); }}
.small {{ font-size:13px; color:var(--muted); }}
.card {{ background:#fff; border:1px solid var(--line); border-radius:12px; padding:16px 20px; margin-top:12px; }}
.wrap {{ overflow-x:auto; }}
table {{ width:100%; border-collapse:collapse; min-width:640px; }}
th {{ text-align:left; font-size:12px; color:var(--muted); font-weight:600; padding:0 8px 8px; border-bottom:1px solid var(--line); white-space:nowrap; }}
td {{ padding:8px; border-bottom:1px solid var(--line); vertical-align:top; }}
th.r, td.r {{ text-align:right; white-space:nowrap; }}
.badge {{ font-size:12px; font-weight:700; padding:2px 8px; border-radius:999px; border:1px solid var(--line); white-space:nowrap; }}
.badge.tracking {{ background:#e6f4ec; border-color:#9fd1b5; color:#1f6b47; }}
.badge.not-yet {{ background:#fff6e5; border-color:#f0cf8c; color:#8a5a00; }}
.badge.not-open {{ background:#f1f4f7; color:var(--muted); }}
.badge.cant, .badge.error {{ background:#fdecea; border-color:#f1b0a9; color:#9a2c22; }}
</style></head><body><main>
<h1>Studio tracker</h1>
<p class="small">Sauna and cold-plunge studios in Canada. {tracked_n} of {len(studios)} tracked automatically every 30 minutes. Last run: {esc(last_run.get('ran_at_utc', 'never'))} (UTC). {since}</p>
<p class="small">“Visits” are places booked at the last reading before each session started, usually within 30 minutes of the start. Late bookings, no-shows and walk-ins aren’t counted. Revenue = visits × the average a visit brings in, set per studio in studios.json.</p>

<h2>Every studio</h2>
<div class="card wrap"><table><tr><th>Studio</th><th>Website</th><th>Booking system</th><th>Status</th><th>Why</th></tr>{''.join(status_rows)}</table></div>

<h2>Last 7 days</h2>
<div class="card wrap"><table><tr><th>Studio · location</th><th class="r">Sessions</th><th class="r">Places</th><th class="r">Visits</th><th class="r">Visits a day</th><th class="r">Full</th><th class="r">Sold out</th><th class="r">Revenue, 7 days</th></tr>{''.join(week_rows)}</table></div>

<h2>Booked so far, next 3 days</h2>
<p class="small">Every location on each schedule right now. Most bookings come in the last day or two, so these keep rising until each session starts.</p>
<div class="card wrap"><table><tr><th>Studio</th><th>Location</th><th class="r">Sessions</th><th class="r">Places</th><th class="r">Booked</th><th class="r">Full</th></tr>{''.join(up_rows) or '<tr><td colspan="6" class="small">Nothing yet</td></tr>'}</table></div>

<h2>By week</h2>
<p class="small">Weeks start on Monday (UTC). The current week is still filling in.</p>
<div class="card wrap"><table><tr><th>Week of</th><th>Studio · location</th><th class="r">Sessions</th><th class="r">Visits</th><th class="r">Full</th></tr>{''.join(wk_rows) or '<tr><td colspan="5" class="small">Nothing yet</td></tr>'}</table></div>
</main></body></html>
"""
    (VIEW / "index.html").write_text(page)
    print(f"view/index.html: {len(studios)} studios, {len(done)} finished sessions")


if __name__ == "__main__":
    main()
