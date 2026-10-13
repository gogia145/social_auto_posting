import os
import re
import json
import hashlib
import html
from datetime import datetime, timezone
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

import requests

STATE_FILE = "posted_items.json"
MAX_POSTS = int(os.getenv("MAX_POSTS_PER_RUN", "3"))
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
FB_PAGE_ID = os.getenv("FACEBOOK_PAGE_ID", "").strip()
FB_PAGE_TOKEN = os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN", "").strip()
GRAPH_VERSION = os.getenv("META_GRAPH_API_VERSION", "v26.0")
RSS_FEEDS = [
    "https://feeds.bbci.co.uk/news/rss.xml",
    "https://feeds.bbci.co.uk/news/technology/rss.xml",
    "https://feeds.reuters.com/reuters/topNews",
    "https://www.theguardian.com/world/rss",
    "https://techcrunch.com/feed/",
]

HEADERS = {"User-Agent": "FreeTrendingPublisher/1.0 (RSS reader; contact site owner before high-volume use)"}
TIMEOUT = 20

def clean_text(value):
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data if isinstance(data, list) else [])
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def save_state(ids):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f, ensure_ascii=False, indent=2)


def fetch_feed(url):
    try:
        response = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        response.raise_for_status()
        root = ET.fromstring(response.content)
        items = []

        media_ns = {
            "media": "http://search.yahoo.com/mrss/",
            "atom": "http://www.w3.org/2005/Atom",
        }

        for node in root.findall(".//item"):
            title = clean_text(node.findtext("title"))
            link = clean_text(node.findtext("link"))
            summary = clean_text(node.findtext("description"))
            published = clean_text(node.findtext("pubDate"))

            image_url = ""
            video_url = ""

            # Standard RSS enclosure
            enclosure = node.find("enclosure")
            if enclosure is not None:
                media_url = enclosure.attrib.get("url", "")
                media_type = enclosure.attrib.get("type", "").lower()
                if media_type.startswith("image/"):
                    image_url = media_url
                elif media_type.startswith("video/"):
                    video_url = media_url

            # Media RSS image/video
            for media in node.findall("media:content", media_ns):
                media_url = media.attrib.get("url", "")
                media_type = media.attrib.get("type", "").lower()
                if media_type.startswith("image/") and not image_url:
                    image_url = media_url
                elif media_type.startswith("video/") and not video_url:
                    video_url = media_url

            thumbnail = node.find("media:thumbnail", media_ns)
            if thumbnail is not None and not image_url:
                image_url = thumbnail.attrib.get("url", "")

            if title and link:
                items.append({
                    "title": title,
                    "link": link,
                    "summary": summary,
                    "published": published,
                    "feed": url,
                    "image_url": image_url,
                    "video_url": video_url,
                })

        return items

    except Exception as exc:
        print(f"Feed unavailable: {url} ({exc})")
        return []


def item_id(item):
    return hashlib.sha256((item["link"] or item["title"]).encode("utf-8")).hexdigest()


def make_original_caption(item):
    title = item["title"].strip().rstrip(".")
    summary = clean_text(item.get("summary", ""))
    domain = urlparse(item["link"]).netloc.replace("www.", "")

    # RSS summary ko short rakhein; article ko word-for-word copy na karein.
    if len(summary) > 500:
        summary = summary[:497].rsplit(" ", 1)[0] + "..."

    parts = [
        f"📰 {title}",
        "",
    ]

    if summary:
        parts.extend([
            "Kya hua?",
            summary,
            "",
        ])

    parts.extend([
        "Is update ka aage kya impact ho sakta hai? "
        "Aapki kya rai hai? Comments mein batayein.",
        "",
        f"🔎 Source: {domain}",
        item["link"],
        "",
        "#TrendingNews #LatestUpdates #Hinglish",
    ])

    return "\n".join(parts)


def publish_facebook(item, message):
    if not FB_PAGE_ID or not FB_PAGE_TOKEN:
        raise RuntimeError(
            "FACEBOOK_PAGE_ID aur FACEBOOK_PAGE_ACCESS_TOKEN "
            "GitHub Secrets mein set karein."
        )

    base = f"https://graph.facebook.com/{GRAPH_VERSION}"
    image_url = item.get("image_url", "")
    video_url = item.get("video_url", "")

    # Direct video file only; embedded YouTube/article pages are not files.
    if video_url.lower().split("?")[0].endswith((".mp4", ".mov", ".m4v")):
        response = requests.post(
            f"{base}/{FB_PAGE_ID}/videos",
            data={
                "file_url": video_url,
                "description": message,
                "access_token": FB_PAGE_TOKEN,
            },
            timeout=90,
        )

    elif image_url.startswith("https://"):
        response = requests.post(
            f"{base}/{FB_PAGE_ID}/photos",
            data={
                "url": image_url,
                "caption": message,
                "access_token": FB_PAGE_TOKEN,
            },
            timeout=60,
        )

    else:
        response = requests.post(
            f"{base}/{FB_PAGE_ID}/feed",
            data={
                "message": message,
                "access_token": FB_PAGE_TOKEN,
            },
            timeout=60,
        )

    if not response.ok:
        # Token ko kabhi logs mein print na karein.
        try:
            error = response.json().get("error", {})
            safe_error = {
                "message": error.get("message", "Unknown Facebook API error"),
                "code": error.get("code"),
                "type": error.get("type"),
            }
        except ValueError:
            safe_error = {"message": "Unrecognized Facebook API response"}

        raise RuntimeError(
            f"Facebook API error {response.status_code}: {safe_error}"
        )

    return response.json()

def main():
    print(f"Run started at {datetime.now(timezone.utc).isoformat()}")
    seen = load_state()
    items = []
    for feed in RSS_FEEDS:
        items.extend(fetch_feed(feed))

    # Deduplicate and prefer fresh RSS items when feeds provide dates.
    unique = {}
    for item in items:
        key = item_id(item)
        if key not in seen:
            unique.setdefault(key, item)

    candidates = list(unique.items())[:MAX_POSTS]
    if not candidates:
        print("No new RSS items to post.")
        return

    newly_posted = set(seen)
    posted_count = 0
    for key, item in candidates:
        caption = make_original_caption(item)
        print("\n--- Candidate ---")
        print(caption)
        if DRY_RUN:
            print("DRY_RUN=true; not publishing.")
            continue
        result = publish_facebook(caption)
        print(f"Published Facebook post ID: {result.get('id', 'unknown')}")
        newly_posted.add(key)
        posted_count += 1

    if not DRY_RUN and posted_count:
        save_state(newly_posted)
    print(f"Finished. Published {posted_count} post(s). DRY_RUN={DRY_RUN}")

if __name__ == "__main__":
    main()
