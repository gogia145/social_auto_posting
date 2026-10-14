
import os
import re
import json
import html
import hashlib
from datetime import datetime, timezone
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

import requests

# ------------ Configuration ------------
STATE_FILE = "posted_items.json"
MAX_POSTS = max(1, int(os.getenv("MAX_POSTS_PER_RUN", "3")))
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

FB_PAGE_ID = os.getenv("FACEBOOK_PAGE_ID", "").strip()
FB_PAGE_TOKEN = os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN", "").strip()
FB_USER_TOKEN = os.getenv("FB_USER_ACCESS_TOKEN", "").strip()

GRAPH_VERSION = os.getenv("META_GRAPH_API_VERSION", "v26.0")
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"
TIMEOUT = 25

AUTO_COMMENT_ENABLED = (
    os.getenv("AUTO_COMMENT_ENABLED", "true").lower() == "true"
)
AUTO_COMMENT_TEXT = os.getenv(
    "AUTO_COMMENT_TEXT",
    "Aapke hisaab se is update ka sabse bada impact kya hoga? "
    "Apni rai comment karein! 👇"
).strip()

HASHTAGS = os.getenv(
    "POST_HASHTAGS",
    "#TrendingNews #LatestUpdates #BreakingNews #IndiaNews #Hinglish"
).strip()

RSS_FEEDS = [
    "https://feeds.bbci.co.uk/news/rss.xml",
    "https://feeds.bbci.co.uk/news/technology/rss.xml",
    "https://www.theguardian.com/world/rss",
    "https://techcrunch.com/feed/",
]

HEADERS = {"User-Agent": "FreeTrendingPublisher/1.0"}
session = requests.Session()


# ------------ Utilities ------------
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
    try:
        error = response.json().get("error", {})
        return (
            f"HTTP {response.status_code}; "
            f"message={error.get('message', 'Unknown error')}; "
            f"code={error.get('code')}; "
            f"subcode={error.get('error_subcode')}"
        )
    except (ValueError, AttributeError):
        return f"HTTP {response.status_code}: {response.text[:300]}"


def get_page_access_token():
    if not FB_PAGE_ID:
        raise RuntimeError("FACEBOOK_PAGE_ID secret is missing.")

    # Prefer configured Page token.
    if FB_PAGE_TOKEN:
        return FB_PAGE_TOKEN

    # Optional fallback: obtain Page token from User token.
    if not FB_USER_TOKEN:
        raise RuntimeError(
            "Set FACEBOOK_PAGE_ACCESS_TOKEN or FB_USER_ACCESS_TOKEN."
        )

    url = f"{GRAPH_BASE}/me/accounts"
    params = {
        "fields": "id,name,access_token",
        "limit": 100,
        "access_token": FB_USER_TOKEN,
    }

    while url:
        response = session.get(url, params=params, timeout=TIMEOUT)
        if not response.ok:
            raise RuntimeError(
                f"Could not get Page token: {api_error(response)}"
            )

        data = response.json()
        for page in data.get("data", []):
            if str(page.get("id")) == FB_PAGE_ID and page.get("access_token"):
                return page["access_token"]

        url = data.get("paging", {}).get("next")
        params = None

    raise RuntimeError(
        "Target Page not found in /me/accounts. Check Page access and token."
    )


def graph_post(endpoint, data):
    response = session.post(
        f"{GRAPH_BASE}/{endpoint.lstrip('/')}",
        data=data,
        timeout=TIMEOUT,
    )

    if not response.ok:
        raise RuntimeError(f"Facebook API error: {api_error(response)}")

    result = response.json()
    if result.get("error"):
        raise RuntimeError(f"Facebook API error: {result['error']}")

    return result


# ------------ RSS processing ------------
def get_child_text(element, names):
    for child in element:
        if child.tag.split("}")[-1].lower() in names and child.text:
            return child.text.strip()
    return ""


def extract_image(item):
    for child in item.iter():
        tag = child.tag.split("}")[-1].lower()
        media_type = child.attrib.get("type", "")
        url = child.attrib.get("url", "")

        if tag == "thumbnail" and url:
            return url

        if tag in ("content", "enclosure") and url:
            if media_type.startswith("image/"):
                return url

    description = get_child_text(
        item, {"description", "summary", "encoded"}
    )
    match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', description, re.I)
    return html.unescape(match.group(1)) if match else ""


def parse_feed(xml_content, feed_url):
    root = ET.fromstring(xml_content)
    items = []

    for item in root.findall(".//item"):
        title = clean_text(get_child_text(item, {"title"}))
        link = get_child_text(item, {"link"})
        description = clean_text(
            get_child_text(item, {"description", "summary", "encoded"})
        )
        guid = get_child_text(item, {"guid", "id"}) or link or title

        if not title or not link:
            continue

        items.append({
            "id": hashlib.sha256(guid.encode("utf-8")).hexdigest(),
            "title": title,
            "link": link,
            "description": description,
            "image": extract_image(item),
            "feed": feed_url,
        })

    # Support Atom feeds too.
    if not items:
        for entry in root.findall(
            ".//{http://www.w3.org/2005/Atom}entry"
        ):
            title = clean_text(get_child_text(entry, {"title"}))
            link = ""
            for child in entry:
                if child.tag.split("}")[-1].lower() == "link":
                    if child.attrib.get("rel", "alternate") == "alternate":
                        link = child.attrib.get("href", "")
                        if link:
                            break

            description = clean_text(
                get_child_text(entry, {"summary", "content"})
            )
            guid = get_child_text(entry, {"id"}) or link or title

            if title and link:
                items.append({
                    "id": hashlib.sha256(guid.encode("utf-8")).hexdigest(),
                    "title": title,
                    "link": link,
                    "description": description,
                    "image": extract_image(entry),
                    "feed": feed_url,
                })

    return items


def fetch_articles():
    articles = []

    for feed in RSS_FEEDS:
        try:
            response = session.get(
                feed, headers=HEADERS, timeout=TIMEOUT
            )
            response.raise_for_status()
            found = parse_feed(response.content, feed)
            articles.extend(found)
            print(f"RSS: {len(found)} articles from {urlparse(feed).netloc}")
        except (requests.RequestException, ET.ParseError, ValueError) as exc:
            print(f"RSS failed for {urlparse(feed).netloc}: {exc}")

    unique = {}
    for item in articles:
        unique.setdefault(item["id"], item)

    return list(unique.values())


def make_caption(item):
    parts = [
        f"📰 {item['title']}",
        item.get("description", "")[:450].strip(),
        "Is update ka aage kya impact ho sakta hai?",
        "Aapki kya rai hai? Comments mein batayein!",
        item["link"],
    ]

    caption = "\n\n".join(part for part in parts if part)

    # Hashtags belong in the POST caption, not the auto-comment.
    if HASHTAGS:
        caption += "\n\n" + HASHTAGS

    return caption


# ------------ Facebook publishing ------------
def publish_facebook(item, caption, page_token):
    """
    Publish a photo or a link/text post.
    Return the actual feed post ID when available.
    """
    if item.get("image"):
        result = graph_post(
            f"{FB_PAGE_ID}/photos",
            {
                "url": item["image"],
                "caption": caption,
                "access_token": page_token,
            },
        )

        # Photo publishing can return both a photo ID and feed post ID.
        post_id = result.get("post_id") or result.get("id")
        if post_id:
            print(f"Photo published. Returned post ID: {post_id}")
            return str(post_id)

        raise RuntimeError("Photo published but Facebook returned no ID.")

    result = graph_post(
        f"{FB_PAGE_ID}/feed",
        {
            "message": caption,
            "link": item["link"],
            "access_token": page_token,
        },
    )

    post_id = result.get("id")
    if not post_id:
        raise RuntimeError("Facebook returned no feed post ID.")

    print(f"Feed post published. Post ID: {post_id}")
    return str(post_id)


def add_facebook_comment(post_id, message, page_token):
    """
    Add a SEPARATE comment under the published post.
    This text is never added to the main caption.
    """
    if not AUTO_COMMENT_ENABLED:
        print("Auto-comment disabled.")
        return False

    if not post_id or not message:
        print("Auto-comment skipped: post ID or comment text is empty.")
        return False

    try:
        result = graph_post(
            f"{post_id}/comments",
            {
                "message": message,
                "access_token": page_token,
            },
        )

        comment_id = result.get("id")
        if comment_id:
            print(f"Auto-comment published. Comment ID: {comment_id}")
            return True

        print("Auto-comment response had no comment ID.")
        return False

    except (requests.RequestException, RuntimeError, ValueError) as exc:
        print(f"Auto-comment failed: {exc}")
        return False

def validate_page_token(page_token):
    response = session.get(
        f"{GRAPH_BASE}/{FB_PAGE_ID}",
        params={
            "fields": "id,name",
            "access_token": page_token,
        },
        timeout=TIMEOUT,
    )

    if not response.ok:
        raise RuntimeError(
            f"Page token validation failed: {api_error(response)}"
        )

    page = response.json()

    if str(page.get("id")) != FB_PAGE_ID:
        raise RuntimeError("Token does not match the configured Page.")

    print(f"Page token validated for Page: {page.get('name')}")
    
# ------------ Main program ------------
def main():
    print(
        "Started:",
        datetime.now(timezone.utc).isoformat(),
        f"| DRY_RUN={DRY_RUN}",
        f"| MAX_POSTS={MAX_POSTS}",
        f"| AUTO_COMMENT_ENABLED={AUTO_COMMENT_ENABLED}",
    )

    state = load_state()
    articles = fetch_articles()
    new_items = [item for item in articles if item["id"] not in state]

    if not new_items:
        print("No new RSS articles.")
        return

    selected = new_items[:MAX_POSTS]

    if DRY_RUN:
        print(
            f"DRY RUN: {len(selected)} article(s) found. "
            "No Facebook posts or comments will be created."
        )
        for item in selected:
            print(f"[DRY RUN] {item['title']} — {item['link']}")
        return

    page_token = get_page_access_token()
    validate_page_token(page_token)

    for item in selected:
        try:
            caption = make_caption(item)
            post_id = publish_facebook(item, caption, page_token)

            # IMPORTANT: comment is a separate API request after publishing.
            # The main caption contains only news text, link, and hashtags.
            comment_ok = add_facebook_comment(
                post_id, AUTO_COMMENT_TEXT, page_token
            )

            # Mark the article posted if publishing succeeded.
            # A comment failure won't cause the article to be reposted.
            state.add(item["id"])
            save_state(state)

            if not comment_ok:
                print(
                    "Post succeeded but comment failed. "
                    "See the Auto-comment error above."
                )

        except (requests.RequestException, RuntimeError, ValueError) as exc:
            print(f"Publish failed for '{item['title']}': {exc}")

    print(f"Finished. Saved {len(state)} posted article ID(s).")


if __name__ == "__main__":
    main()
