import sys
import json
import base64
import re
import shutil
import requests
from datetime import datetime, timedelta
from pathlib import Path
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# --- Config ---
import os
SENDER = "updates@tadpoles.com"
PHOTOS_DIR = Path(__file__).parent / "photos"
PHOTOS_JSON = Path(__file__).parent / "photos.json"
_override = os.environ.get("CREDENTIALS_OVERRIDE")
CREDENTIALS_FILE = Path(_override) if _override else next(Path(__file__).parent.glob("client_secret_*.json"), None)
TOKEN_FILE = Path(__file__).parent / "token.json"
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# Known logo/junk image sizes in bytes — same files repeated in every email
KNOWN_JUNK_SIZES = {42, 5854, 9430, 23429, 27234, 45580}
MIN_PHOTO_SIZE = 30_000  # 30KB minimum — real photos are always larger

def is_real_photo(img_bytes):
    size = len(img_bytes)
    return size >= MIN_PHOTO_SIZE and size not in KNOWN_JUNK_SIZES

def authenticate():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return creds

def get_or_create_label(service):
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    label_id = next((l["id"] for l in labels if l["name"] == "aydin-fetched"), None)
    if not label_id:
        label = service.users().labels().create(
            userId="me", body={"name": "aydin-fetched"}
        ).execute()
        label_id = label["id"]
    return label_id

def reset(service, label_id):
    """Remove aydin-fetched label from all emails and wipe the photos folder."""
    print("--- RESET MODE ---")

    # Remove label from all labeled emails
    print("Removing 'aydin-fetched' label from Gmail emails...")
    results = service.users().messages().list(
        userId="me", q="label:aydin-fetched"
    ).execute()
    messages = results.get("messages", [])
    for msg in messages:
        service.users().messages().modify(
            userId="me", id=msg["id"],
            body={"removeLabelIds": [label_id]}
        ).execute()
    print(f"  Removed label from {len(messages)} email(s)")

    # Wipe photos folder and json
    if PHOTOS_DIR.exists():
        shutil.rmtree(PHOTOS_DIR)
        print(f"  Deleted photos folder")
    if PHOTOS_JSON.exists():
        PHOTOS_JSON.unlink()
        print(f"  Deleted photos.json")

    print("\nReset complete. Run the script normally to re-fetch.\n")

def get_images_from_email(service, msg_id):
    message = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    images = []

    def walk_parts(parts):
        for part in parts:
            mime = part.get("mimeType", "")
            if mime.startswith("image/"):
                data = part.get("body", {}).get("data")
                if data:
                    images.append(("attachment", base64.urlsafe_b64decode(data), mime))
            elif mime == "text/html":
                data = part.get("body", {}).get("data")
                if data:
                    html = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
                    urls = re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE)
                    for url in urls:
                        if url.startswith("http") and not url.endswith(".gif"):
                            images.append(("url", url, "image/jpeg"))
            if "parts" in part:
                walk_parts(part["parts"])

    payload = message.get("payload", {})
    if "parts" in payload:
        walk_parts(payload["parts"])
    elif payload.get("mimeType", "").startswith("image/"):
        data = payload.get("body", {}).get("data")
        if data:
            images.append(("attachment", base64.urlsafe_b64decode(data), payload["mimeType"]))

    headers = {h["name"]: h["value"] for h in payload.get("headers", [])}
    date_str = headers.get("Date", "")
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(date_str)
    except Exception:
        dt = datetime.now()

    return images, dt

def save_image(img_bytes, dt, index, ext="jpg"):
    PHOTOS_DIR.mkdir(exist_ok=True)
    month_dir = PHOTOS_DIR / dt.strftime("%Y-%m")
    month_dir.mkdir(exist_ok=True)
    filename = f"{dt.strftime('%Y-%m-%d_%H-%M-%S')}_{index:02d}.{ext}"
    filepath = month_dir / filename
    if not filepath.exists():
        filepath.write_bytes(img_bytes)
        print(f"  Saved: {filepath.name}")
        return filepath
    return None

def update_photos_json(new_photos):
    existing = []
    if PHOTOS_JSON.exists():
        with open(PHOTOS_JSON) as f:
            existing = json.load(f)
    existing_paths = {p["path"] for p in existing}
    for p in new_photos:
        if p["path"] not in existing_paths:
            existing.append(p)
    existing.sort(key=lambda x: x["date"], reverse=True)
    with open(PHOTOS_JSON, "w") as f:
        json.dump(existing, f, indent=2)
    print(f"\nphotos.json updated — {len(existing)} total photos")

def cleanup_existing_photos():
    """Remove junk photos already in folder and rebuild photos.json."""
    import hashlib
    print("Cleaning up existing photos...")
    removed = 0
    kept = []
    seen_hashes = set()

    all_files = sorted(PHOTOS_DIR.rglob("*.jpg")) + sorted(PHOTOS_DIR.rglob("*.mp4"))

    for media_file in all_files:
        img_bytes = media_file.read_bytes()
        is_video = media_file.suffix.lower() == ".mp4"

        if not is_video and not is_real_photo(img_bytes):
            media_file.unlink()
            removed += 1
            continue

        # Deduplicate by content hash
        content_hash = hashlib.md5(img_bytes).hexdigest()
        if content_hash in seen_hashes:
            media_file.unlink()
            removed += 1
            continue
        seen_hashes.add(content_hash)

        rel_path = media_file.relative_to(Path(__file__).parent)
        try:
            dt = datetime.strptime(media_file.stem[:19], "%Y-%m-%d_%H-%M-%S")
        except Exception:
            dt = datetime.fromtimestamp(media_file.stat().st_mtime)

        entry = {
            "path": str(rel_path).replace("\\", "/"),
            "date": dt.isoformat(),
            "filename": media_file.name
        }
        if is_video:
            entry["type"] = "video"
        kept.append(entry)

    kept.sort(key=lambda x: x["date"], reverse=True)
    with open(PHOTOS_JSON, "w") as f:
        json.dump(kept, f, indent=2)
    print(f"  Removed {removed} junk/dupes, kept {len(kept)} media files\n")

def main():
    if not CREDENTIALS_FILE:
        print("ERROR: No client_secret_*.json file found in this folder.")
        return

    print("Authenticating with Gmail...")
    creds = authenticate()
    service = build("gmail", "v1", credentials=creds)
    label_id = get_or_create_label(service)

    # --reset flag: wipe everything and start fresh
    if "--reset" in sys.argv:
        reset(service, label_id)
        return

    # Clean up junk from any previous run
    if PHOTOS_DIR.exists():
        cleanup_existing_photos()

    one_week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y/%m/%d")
    print(f"Searching for emails from {SENDER} since {one_week_ago}...")
    results = service.users().messages().list(
        userId="me",
        q=f"from:{SENDER} -label:aydin-fetched after:{one_week_ago}"
    ).execute()

    messages = results.get("messages", [])
    print(f"Found {len(messages)} new email(s)\n")

    new_photos = []

    for msg in messages:
        msg_id = msg["id"]
        print(f"Processing email {msg_id}...")
        images, dt = get_images_from_email(service, msg_id)
        print(f"  {len(images)} image(s) in email — dated {dt.strftime('%Y-%m-%d %H:%M')}")

        photo_count = 0
        for i, (source, data, mime) in enumerate(images):
            ext = "jpg" if "jpeg" in mime or "jpg" in mime else mime.split("/")[-1]
            if source == "url":
                try:
                    response = requests.get(data, timeout=15)
                    if response.status_code != 200:
                        continue
                    img_bytes = response.content
                except Exception as e:
                    print(f"  Failed to download: {e}")
                    continue
            else:
                img_bytes = data

            if not is_real_photo(img_bytes):
                continue

            saved = save_image(img_bytes, dt, i, ext)
            if saved:
                rel_path = saved.relative_to(Path(__file__).parent)
                new_photos.append({
                    "path": str(rel_path).replace("\\", "/"),
                    "date": dt.isoformat(),
                    "filename": saved.name
                })
                photo_count += 1

        print(f"  Kept {photo_count} real photo(s)")

        try:
            service.users().messages().modify(
                userId="me", id=msg_id,
                body={"addLabelIds": [label_id]}
            ).execute()
        except Exception as e:
            print(f"  Warning: could not label email: {e}")

    if new_photos:
        update_photos_json(new_photos)
    else:
        print("No new photos to save.")

if __name__ == "__main__":
    main()
