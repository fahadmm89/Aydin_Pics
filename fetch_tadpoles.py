"""
One-time script to download all historical photos and videos from Tadpoles.
Run this once to backfill your repository.

Usage:
    python fetch_tadpoles.py
"""

import json
import time
import calendar
import requests
from datetime import datetime, timezone
from pathlib import Path

PHOTOS_DIR = Path(__file__).parent / "photos"
PHOTOS_JSON = Path(__file__).parent / "photos.json"
COOKIE_FILE = Path(__file__).parent / "tadpoles_cookie.txt"
API_BASE = "https://www.tadpoles.com"

# Fetch from this month onwards — adjust if Aydin started earlier
FETCH_FROM_YEAR = 2024
FETCH_FROM_MONTH = 1


def load_cookie():
    return COOKIE_FILE.read_text().strip()


def month_timestamps(year, month):
    """Return (start_ms, end_ms) for a calendar month."""
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    last_day = calendar.monthrange(year, month)[1]
    end = datetime(year, month, last_day, 23, 59, 59, tzinfo=timezone.utc)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def all_months(from_year, from_month):
    """Yield (year, month) tuples from start to today."""
    now = datetime.now()
    year, month = from_year, from_month
    while (year, month) <= (now.year, now.month):
        yield year, month
        month += 1
        if month > 12:
            month = 1
            year += 1


def fetch_month(session, year, month):
    start_ms, end_ms = month_timestamps(year, month)
    payload = {
        "client": "dashboard",
        "direction": "range",
        "event_timestamp": start_ms,
        "end_event_timestamp": end_ms,
        "num_events": 300,
    }
    resp = session.post(f"{API_BASE}/remote/v2/events/query", json=payload, timeout=30)
    if resp.status_code == 401:
        print("  ERROR: Cookie expired. Please re-copy it from the Network tab.")
        return None
    if resp.status_code != 200:
        print(f"  Error {resp.status_code}: {resp.text[:100]}")
        return []
    data = resp.json()
    return data.get("payload", {}).get("events", [])


def download_attachment(cookie_str, evt_key, att_key, dt, index, mime):
    ext = "mp4" if "video" in mime else ("png" if "png" in mime else "jpg")

    PHOTOS_DIR.mkdir(exist_ok=True)
    filename = f"{dt.strftime('%Y-%m-%d_%H-%M-%S')}_tp_{index:04d}.{ext}"
    filepath = PHOTOS_DIR / filename

    if filepath.exists():
        return filepath, False

    try:
        url = f"{API_BASE}/remote/v1/obj_attachment"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
            "Referer": "https://www.tadpoles.com/parents",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Cookie": cookie_str,
        }
        resp = requests.get(url, params={"obj": evt_key, "key": att_key}, headers=headers, timeout=30)
        if resp.status_code == 200:
            ct = resp.headers.get("content-type", "")
            if "video" in ct:
                filepath = filepath.with_suffix(".mp4")
            elif "png" in ct:
                filepath = filepath.with_suffix(".png")
            filepath.write_bytes(resp.content)
            return filepath, True
        else:
            print(f"    Download failed ({resp.status_code}): {resp.text[:300]}")
            return None, False
    except Exception as e:
        print(f"    Error: {e}")
        return None, False


def update_photos_json(new_photos):
    existing = []
    if PHOTOS_JSON.exists():
        with open(PHOTOS_JSON) as f:
            existing = json.load(f)
    existing_paths = {p["path"] for p in existing}
    added = 0
    for p in new_photos:
        if p["path"] not in existing_paths:
            existing.append(p)
            added += 1
    existing.sort(key=lambda x: x["date"], reverse=True)
    with open(PHOTOS_JSON, "w") as f:
        json.dump(existing, f, indent=2)
    print(f"\nphotos.json updated — added {added} new, {len(existing)} total")


def main():
    if not COOKIE_FILE.exists():
        print("ERROR: tadpoles_cookie.txt not found.")
        print("Copy the cookie value from Chrome DevTools Network tab and save it to tadpoles_cookie.txt")
        return

    cookie = load_cookie()
    print(f"Cookie loaded ({len(cookie)} chars)\n")

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        "Content-Type": "application/json",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": "https://www.tadpoles.com/parents",
        "Origin": "https://www.tadpoles.com",
        "X-Tadpoles-App-Id": "com.tadpoles.web.parent",
        "X-Tadpoles-Device-Platform": "web",
        "X-Tadpoles-Uid": "fahadm89@gmail.com",
    })
    # Parse cookie string into session cookie jar
    for chunk in cookie.split(";"):
        chunk = chunk.strip()
        if "=" in chunk:
            name, _, value = chunk.partition("=")
            value = value.strip().strip('"')
            session.cookies.set(name.strip(), value, domain="www.tadpoles.com")

    new_photos = []
    total_index = 0

    for year, month in all_months(FETCH_FROM_YEAR, FETCH_FROM_MONTH):
        label = f"{year}-{month:02d}"
        entries = fetch_month(session, year, month)

        if entries is None:
            print("Stopping — authentication failed.")
            break
        if not entries:
            print(f"  {label}: no entries")
            continue




        # Only keep "fun photo" events with image/video attachments
        media_entries = []
        for e in entries:
            labels = e.get("labels") or e.get("unmodified_labels") or []
            if "fun photo" not in labels:
                continue
            for att in (e.get("attachments") or []):
                if not isinstance(att, dict):
                    continue
                mime = att.get("mime_type", "")
                if mime.startswith("image/") or mime.startswith("video/"):
                    att_key = att.get("key") or att.get("uuid", "")
                    evt_key = e.get("key", "")
                    if att_key and evt_key:
                        media_entries.append((e, evt_key, att_key, mime))

        # Count fun photo events for debug
        fun_count = sum(1 for e in entries if "fun photo" in (e.get("labels") or e.get("unmodified_labels") or []))
        print(f"  {label}: {len(entries)} entries, {fun_count} fun photos, {len(media_entries)} downloadable")

        if not media_entries:
            continue

        print(f"  Downloading {len(media_entries)} files...")

        for entry, evt_key, att_key, mime in media_entries:
            # Get timestamp
            ts = entry.get("capture_time") or entry.get("action_time") or entry.get("event_time", 0)
            if ts > 1e10:
                ts /= 1000
            try:
                dt = datetime.fromtimestamp(ts)
            except Exception:
                dt = datetime(year, month, 1)

            filepath, downloaded = download_attachment(cookie, evt_key, att_key, dt, total_index, mime)
            if filepath and downloaded:
                rel_path = filepath.relative_to(Path(__file__).parent)
                new_photos.append({
                    "path": str(rel_path).replace("\\", "/"),
                    "date": dt.isoformat(),
                    "filename": filepath.name,
                    "type": "video" if "video" in mime else "photo",
                })
            total_index += 1

        time.sleep(0.3)

    if new_photos:
        update_photos_json(new_photos)
        print(f"\nDone! {len(new_photos)} files saved.")
        print("\nNow push to GitHub:")
        print('  git add photos/ photos.json')
        print('  git commit -m "Add historical Tadpoles media"')
        print('  git push')
    else:
        print("\nNo new files downloaded.")


if __name__ == "__main__":
    main()
