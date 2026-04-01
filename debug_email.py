"""Debug script — prints raw HTML from the missed emails without modifying labels."""
import base64, re
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from pathlib import Path

TOKEN_FILE = Path("token.json")
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
if creds.expired and creds.refresh_token:
    creds.refresh(Request())
service = build("gmail", "v1", credentials=creds)

# Search INCLUDING already-labeled emails
results = service.users().messages().list(
    userId="me",
    q="from:updates@tadpoles.com subject:'Aydin at Goddard' after:2026/03/30"
).execute()

messages = results.get("messages", [])
print(f"Found {len(messages)} email(s)\n")

for msg in messages[:3]:
    message = service.users().messages().get(userId="me", id=msg["id"], format="full").execute()
    payload = message.get("payload", {})
    headers = {h["name"]: h["value"] for h in payload.get("headers", [])}
    print(f"=== Subject: {headers.get('Subject','?')} | Date: {headers.get('Date','?')} ===")

    def walk(parts, depth=0):
        for part in parts:
            mime = part.get("mimeType", "")
            size = part.get("body", {}).get("size", 0)
            print(f"{'  '*depth}MIME: {mime} | size: {size}")
            if mime == "text/html":
                data = part.get("body", {}).get("data")
                if data:
                    html = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
                    imgs = re.findall(r'<img[^>]+>', html, re.IGNORECASE)
                    print(f"{'  '*depth}  <img> tags: {len(imgs)}")
                    for img in imgs[:5]:
                        print(f"{'  '*depth}    {img[:200]}")
                    hrefs = re.findall(r'href=["\']([^"\']*(?:jpg|jpeg|png|mp4)[^"\']*)["\']', html, re.IGNORECASE)
                    print(f"{'  '*depth}  image hrefs: {len(hrefs)}")
                    for h in hrefs[:5]:
                        print(f"{'  '*depth}    {h[:200]}")
                    all_urls = re.findall(r'https?://[^\s"\'<>]+(?:jpg|jpeg|png|mp4)[^\s"\'<>]*', html, re.IGNORECASE)
                    print(f"{'  '*depth}  raw image URLs: {len(all_urls)}")
                    for u in all_urls[:5]:
                        print(f"{'  '*depth}    {u[:200]}")
            if "parts" in part:
                walk(part["parts"], depth+1)

    if "parts" in payload:
        walk(payload["parts"])
    else:
        # Single-part email — content is directly in payload body
        mime = payload.get("mimeType", "")
        size = payload.get("body", {}).get("size", 0)
        print(f"Single-part: MIME={mime} | size={size}")
        data = payload.get("body", {}).get("data")
        if data:
            html = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
            imgs = re.findall(r'<img[^>]+>', html, re.IGNORECASE)
            print(f"  <img> tags: {len(imgs)}")
            for img in imgs[:5]:
                print(f"    {img[:200]}")
            all_urls = re.findall(r'https?://[^\s"\'<>]+(?:jpg|jpeg|png|mp4)[^\s"\'<>]*', html, re.IGNORECASE)
            print(f"  raw image URLs: {len(all_urls)}")
            for u in all_urls[:5]:
                print(f"    {u[:200]}")
            # Print a snippet of raw HTML to see structure
            print(f"  HTML snippet (first 500 chars):")
            print(f"    {html[:500]}")
    print()
