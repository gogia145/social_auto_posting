
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
MAX_POSTS = max(1, int(os.getenv("MAX_POSTS_PER_RUN", "3")))
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

FB_PAGE_ID = os.getenv("FACEBOOK_PAGE_ID", "").strip()

# Preferred: a valid long-lived User Access Token.
# Fallback: the existing Page Access Token secret.
FB_USER_TOKEN = os.getenv("FB_USER_ACCESS_TOKEN", "").strip()
FB_PAGE_TOKEN = os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN", "").strip()

GRAPH_VERSION = os.getenv("META_GRAPH_API_VERSION", "v26.0")
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"

RSS_FEEDS = [
    "https://feeds.bbci.co.uk/news/rss.xml",
    "https://feeds.bbci.co.uk/news/technology/rss.xml",
    "https://feeds.reuters.com/reuters/topNews",
    "https://www.theguardian.com/world/rss",
    "https://techcrunch.com/feed/",
]

HEADERS = {
    "User-Agent": (
        "FreeTrendingPublisher/1.0 "
        "(RSS reader; contact site owner before high-volume use)"
    )
}
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


def api_error(response):
    """Return safe error details without exposing access tokens."""
    try:
        error = response.json().get("error", {})
        return {
            "message": error.get("message", "Unknown Facebook API error"),
            "code": error.get("code"),
            "type": error.get("type"),
            "is_transient": error.get("is_transient", False),
        }
    except (ValueError, AttributeError):
        return {"message": "Unrecognized Facebook API response"}


def get_page_access_token():
    """
    Obtain the target Page token dynamically using a valid User token.
    Fall back to a configured Page token for backward compatibility.
    """
    if not FB_PAGE_ID:
        raise RuntimeError("Missing FACEBOOK_PAGE_ID GitHub Secret.")

    if not FB_USER_TOKEN:
        if FB_PAGE_TOKEN:
            return FB_PAGE_TOKEN

        raise RuntimeError(
            "Missing FB_USER_ACCESS_TOKEN. Add a valid long-lived "
            "User Access Token to GitHub Actions Secrets."
        )

    url = f"{GRAPH_BASE}/me/accounts"
    params = {
        "fields": "id,name,access_token",
        "limit": 100,
        "access_token": FB_USER_TOKEN,
    }

    try:
        while url:
            response = requests.get(
                url, params=params, timeout=TIMEOUT
            )

            if not response.ok:
                error = api_error(response)

                if error.get("code") == 190:
                    raise RuntimeError(
                        "Facebook User Access Token is expired or invalid. "
                        "Generate a valid token and update FB_USER_ACCESS_TOKEN."
                    )

                raise RuntimeError(
                    f"Facebook Page-token lookup failed: {error}"
                )

            data = response.json()

            for page in data.get("data", []):
                if str(page.get("id")) == str(FB_PAGE_ID):
                    token = page.get("access_token")
                    if token:
                        return token

                    raise RuntimeError(
                        "Facebook returned the target Page without a token. "
                        "Check Page access and permissions."
                    )

            # Follow pagination without printing URLs or tokens.
            url = data.get("paging", {}).get("next")
            params = None

    except requests.RequestException as exc:
        raise RuntimeError(
            f"Could not retrieve Facebook Page token: "
            f"{type(exc).__name__}"
        ) from None

    raise RuntimeError(
        "Target Facebook Page was not found in /me/accounts. "
        "Check FACEBOOK_PAGE_ID, Page access, and permissions."
    )


def fetch_feed(url):
    try:
        response = requests.get(
            url, headers=HEADERS, timeout=TIMEOUT
        )
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

            enclosure = node.find("enclosure")
            if enclosure is not None:
                media_url = enclosure.attrib.get("url", "")
                media_type = enclosure.attrib.get("type", "").lower()

                if media_type.startswith("image/"):
                    image_url = media_url
                elif media_type.startswith("video/"):
                    video_url = media_url

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
        print(f"Feed unavailable: {url} ({type(exc).__name__})")
        return []


def item_id(item):
    return hashlib.sha256(
        (item["link"] or item["title"]).encode("utf-8")
    ).hexdigest()


def make_original_caption(item):
    title = item["title"].strip().rstrip(".")
    summary = clean_text(item.get("summary", ""))
    domain = urlparse(item["link"]).netloc.replace("www.", "")

    if len(summary) > 500:
        summary = summary[:497].rsplit(" ", 1)[0] + "..."

    parts = [f"📰 {title}", ""]

    if summary:
        parts.extend(["Kya hua?", summary, ""])

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
    if not FB_PAGE_ID:
        raise RuntimeError("Missing FACEBOOK_PAGE_ID GitHub Secret.")

    # Resolve a Page token immediately before publishing.
    page_token = get_page_access_token()

    image_url = item.get("image_url", "")
    video_url = item.get("video_url", "")

    if video_url.lower().split("?")[0].endswith(
        (".mp4", ".mov", ".m4v")
    ):
        endpoint = f"{GRAPH_BASE}/{FB_PAGE_ID}/videos"
        payload = {
            "file_url": video_url,
            "description": message,
            "access_token": page_token,
        }
        timeout = 90

    elif image_url.startswith("https://"):
        endpoint = f"{GRAPH_BASE}/{FB_PAGE_ID}/photos"
        payload = {
            "url": image_url,
            "caption": message,
            "access_token": page_token,
        }
        timeout = 60

    else:
        endpoint = f"{GRAPH_BASE}/{FB_PAGE_ID}/feed"
        payload = {
            "message": message,
            "access_token": page_token,
        }
        timeout = 60

    try:
        response = requests.post(
            endpoint, data=payload, timeout=timeout
        )
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Facebook publishing request failed: "
            f"{type(exc).__name__}. Verify post status before retrying."
        ) from None

    if not response.ok:
        error = api_error(response)

        if error.get("code") == 190:
            raise RuntimeError(
                "Facebook authentication failed (code 190). "
                "Check FB_USER_ACCESS_TOKEN and Page permissions. "
                "Re-authentication may be required."
            )

        raise RuntimeError(
            f"Facebook API error {response.status_code}: {error}"
        )

    return response.json()


def main():
    print(
        f"Run started at {datetime.now(timezone.utc).isoformat()}"
    )
    print(f"DRY_RUN={DRY_RUN}; MAX_POSTS={MAX_POSTS}")

    seen = load_state()
    items = []

    for feed in RSS_FEEDS:
        items.extend(fetch_feed(feed))

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

        try:
            result = publish_facebook(item, caption)
        except RuntimeError as exc:
            print(f"Publishing stopped: {exc}")
            # Do not mark failed items as posted.
            # Avoid further attempts with a broken token.
            break

        post_id = (
            result.get("post_id")
            or result.get("id")
            or "unknown"
        )
        print(f"Published Facebook post ID: {post_id}")

        newly_posted.add(key)
        posted_count += 1

    if not DRY_RUN and posted_count:
        save_state(newly_posted)

    print(
        f"Finished. Published {posted_count} post(s). "
        f"DRY_RUN={DRY_RUN}"
    )


if __name__ == "__main__":
    main()
