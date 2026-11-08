"""Save how full each studio's upcoming sessions are.

Run every 30 minutes. For each studio with a public Mariana Tek schedule it reads the
next few days of sessions and keeps the latest reading of each session taken before
it starts. Booking systems wipe the numbers once a session starts, so the last
reading before the start is the closest we get to how many people came.

Output: data/sessions-YYYY-MM.csv (one row per session, by start month)
        data/last-run.json (when it ran and what it saw)
Standard library only, so it runs anywhere with Python 3.9+.
"""

import csv
import datetime as dt
import json
import pathlib
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent
DATA = ROOT / "data"
DAYS_AHEAD = 3
HEADERS = {"Accept": "application/json", "User-Agent": "Mozilla/5.0 (schedule research; low frequency)"}
FIELDS = [
    "studio", "location", "session_id", "type", "start_utc", "capacity", "booked", "available",
    "waitlist", "status", "first_seen_utc", "last_seen_utc", "minutes_before_start", "readings",
]


def fetch_marianatek(tenant, start, end):
    query = {"min_start_date": start, "max_start_date": end, "page_size": 500, "page": 1}
    url = f"https://{tenant}.marianatek.com/api/customer/v1/classes?{urllib.parse.urlencode(query)}"
    out = []
    while url:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as r:
            page = json.load(r)
        out += page["results"]
        url = page["next"]
        time.sleep(1)
    return out


def load_month(month):
    path = DATA / f"sessions-{month}.csv"
    if not path.exists():
        return {}
    with path.open(newline="") as f:
        return {(row["studio"], row["session_id"]): row for row in csv.DictReader(f)}


def save_month(month, rows):
    path = DATA / f"sessions-{month}.csv"
    ordered = sorted(rows.values(), key=lambda r: (r["start_utc"], r["studio"], r["location"]))
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(ordered)
    tmp.replace(path)


def main():
    DATA.mkdir(exist_ok=True)
    studios = json.loads((ROOT / "studios.json").read_text())["studios"]
    now = dt.datetime.now(dt.timezone.utc)
    now_s = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    # a day back covers time zones west of UTC; sessions that already started are skipped below
    start = (now - dt.timedelta(days=1)).date().isoformat()
    end = (now + dt.timedelta(days=DAYS_AHEAD)).date().isoformat()
    months = {}
    run = {"ran_at_utc": now_s, "studios": {}}

    for s in studios:
        if s.get("track") != "marianatek":
            continue
        try:
            sessions = fetch_marianatek(s["tenant"], start, end)
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            run["studios"][s["id"]] = {"error": str(e)[:200]}
            continue
        seen = 0
        for x in sessions:
            if x.get("is_cancelled"):
                continue
            begins = dt.datetime.fromisoformat(x["start_datetime"].replace("Z", "+00:00"))
            cap = x.get("capacity") or 0
            loc_name = (x.get("location") or {}).get("name", "")
            type_name = (x.get("class_type") or {}).get("name", "")
            if any(e.lower() in loc_name.lower() for e in s.get("exclude_locations", [])):
                continue
            if s.get("include_types") and not any(t.lower() in type_name.lower() for t in s["include_types"]):
                continue
            if begins <= now or cap <= 0:
                continue  # started already (numbers are wiped) or a special event with no places
            month = begins.strftime("%Y-%m")
            rows = months.setdefault(month, load_month(month))
            key = (s["id"], str(x["id"]))
            available = x.get("available_spot_count") or 0
            old = rows.get(key)
            rows[key] = {
                "studio": s["id"],
                "location": loc_name,
                "session_id": str(x["id"]),
                "type": type_name,
                "start_utc": begins.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "capacity": cap,
                "booked": max(0, cap - available),
                "available": available,
                "waitlist": x.get("waitlist_count") or 0,
                "status": x.get("status") or "",
                "first_seen_utc": old["first_seen_utc"] if old else now_s,
                "last_seen_utc": now_s,
                "minutes_before_start": round((begins - now).total_seconds() / 60),
                "readings": int(old["readings"]) + 1 if old else 1,
            }
            seen += 1
        run["studios"][s["id"]] = {"upcoming_sessions": seen}

    for month, rows in months.items():
        save_month(month, rows)
    (DATA / "last-run.json").write_text(json.dumps(run, indent=2) + "\n")
    print(json.dumps(run))


if __name__ == "__main__":
    main()
