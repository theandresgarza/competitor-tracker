"""Save how full each studio's upcoming sessions are.

Run every 30 minutes. For each studio with a public Mariana Tek or Ferb schedule it reads the
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
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parent
DATA = ROOT / "data"
DAYS_AHEAD = 3
# bookings are saved as each session passes these points before its start, to show how early people book
CHECKPOINTS_H = [72, 48, 24, 12, 6, 3, 2, 1]
HEADERS = {"Accept": "application/json", "User-Agent": "Mozilla/5.0 (schedule research; low frequency)"}
FIELDS = [
    "studio", "location", "session_id", "type", "start_utc", "capacity", "booked", "available",
    "waitlist", "status", "timezone", "first_seen_utc", "last_seen_utc", "minutes_before_start", "readings",
] + [f"booked_{h}h" for h in CHECKPOINTS_H]


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
        time.sleep(0.3)
    return out


def fetch_ferb(brand, start, end):
    """Ferb (used by The Sacred Sauna) lists classes with local start times and spots left.
    Returned in the same shape as Mariana Tek classes so the rest of the script is shared."""
    query = {"start": start, "end": end, "includeCourseSessions": "true", "includePrivateEvents": "false"}
    url = f"https://core.ferb.co/v1/public/brands/{brand}/schedule?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        page = json.load(r)
    out = []
    for x in page.get("data") or []:
        if x.get("itemType") != "class":
            continue
        loc = x.get("location") or {}
        tz = loc.get("timezone") or "UTC"
        local = dt.datetime.fromisoformat(f"{x['startDate']}T{x['startTime']}").replace(tzinfo=ZoneInfo(tz))
        out.append({
            "id": x["id"],
            "start_datetime": local.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "capacity": x.get("capacity") or 0,
            "available_spot_count": x.get("spotsRemaining") or 0,
            "is_cancelled": bool(x.get("cancelledAt")),
            "status": "" if x.get("bookable", True) else "Not bookable",
            "location": {"name": (loc.get("name") or "").replace("\U0001f4cd", "").strip(), "timezone": tz},
            "class_type": {"name": (x.get("classType") or {}).get("name") or x.get("title") or ""},
        })
    return out


READERS = {"marianatek": fetch_marianatek, "ferb": fetch_ferb}


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
        w = csv.DictWriter(f, fieldnames=FIELDS, restval="")
        w.writeheader()
        w.writerows(ordered)
    tmp.replace(path)


def main():
    DATA.mkdir(exist_ok=True)
    studios = json.loads((ROOT / "studios.json").read_text())["studios"]
    now = dt.datetime.now(dt.timezone.utc)
    now_s = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    months = {}
    run = {"ran_at_utc": now_s, "studios": {}}

    def safe_fetch(job):
        platform, tenant, day = job
        try:
            return READERS[platform](tenant, day, day)
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            return e

    # one request per booking system and day (shared by its locations), all at once to keep runs
    # under a billed minute; a big schedule (Sweat and Tonic) takes ~45 s in one go
    tenants = sorted({(s["track"], s["tenant"]) for s in studios if s.get("track") in READERS})
    days = [(now + dt.timedelta(days=d)).date().isoformat() for d in range(-1, DAYS_AHEAD + 1)]
    jobs = [(p, t, d) for p, t in tenants for d in days]
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = dict(zip(jobs, pool.map(safe_fetch, jobs)))
    fetched = {}
    for (p, t, d), r in results.items():
        t = (p, t)
        if isinstance(r, Exception) or isinstance(fetched.get(t), Exception):
            fetched[t] = r if isinstance(r, Exception) else fetched[t]
        else:
            fetched.setdefault(t, []).extend(r)
    for s in studios:
        if s.get("track") not in READERS:
            continue
        sessions = fetched[(s["track"], s["tenant"])]
        if isinstance(sessions, Exception):
            run["studios"][s["id"]] = {"error": str(sessions)[:200]}
            continue
        seen = 0
        for x in sessions:
            if x.get("is_cancelled"):
                continue
            begins = dt.datetime.fromisoformat(x["start_datetime"].replace("Z", "+00:00"))
            cap = x.get("capacity") or 0
            loc_name = (x.get("location") or {}).get("name", "")
            type_name = (x.get("class_type") or {}).get("name", "")
            if s.get("include_locations") and loc_name.lower() not in [x.lower() for x in s["include_locations"]]:
                continue
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
            booked = max(0, cap - available)
            minutes_before = round((begins - now).total_seconds() / 60)
            old = rows.get(key)
            # keep checkpoints already saved; save a new one when this reading is the first past it
            # (only if an earlier reading was taken before the point, so the value is accurate)
            marks = {f"booked_{h}h": (old or {}).get(f"booked_{h}h", "") for h in CHECKPOINTS_H}
            if old:
                prev = int(old["minutes_before_start"])
                for h in CHECKPOINTS_H:
                    if not marks[f"booked_{h}h"] and prev > h * 60 >= minutes_before:
                        marks[f"booked_{h}h"] = booked
            rows[key] = {
                "studio": s["id"],
                "location": loc_name,
                "session_id": str(x["id"]),
                "type": type_name,
                "start_utc": begins.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "capacity": cap,
                "booked": booked,
                "available": available,
                "waitlist": x.get("waitlist_count") or 0,
                "status": x.get("status") or "",
                "timezone": (x.get("location") or {}).get("timezone") or "",
                "first_seen_utc": old["first_seen_utc"] if old else now_s,
                "last_seen_utc": now_s,
                "minutes_before_start": minutes_before,
                "readings": int(old["readings"]) + 1 if old else 1,
                **marks,
            }
            seen += 1
        run["studios"][s["id"]] = {"upcoming_sessions": seen}

    for month, rows in months.items():
        save_month(month, rows)
    (DATA / "last-run.json").write_text(json.dumps(run, indent=2) + "\n")
    # one line per run, so the view can show missed or failed runs
    log = DATA / "runs.csv"
    new_log = not log.exists()
    with log.open("a", newline="") as f:
        w = csv.writer(f)
        if new_log:
            w.writerow(["ran_at_utc", "studios_ok", "studios_failed", "upcoming_sessions"])
        ok = [v for v in run["studios"].values() if "error" not in v]
        w.writerow([now_s, len(ok), len(run["studios"]) - len(ok), sum(v["upcoming_sessions"] for v in ok)])
    print(json.dumps(run))


if __name__ == "__main__":
    main()
