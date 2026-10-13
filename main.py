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
        # RSS 2.0
        for node in root.findall(".//item"):
            title = clean_text(node.findtext("title"))
            link = clean_text(node.findtext("link"))
            desc = clean_text(node.findtext("description"))
            pubdate = clean_text(node.findtext("pubDate"))
            if title and link:
                items.append({"title": title, "link": link, "summary": desc, "published": pubdate, "feed": url})
        # Atom fallback
        if not items:
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            for node in root.findall(".//atom:entry", ns):
                title = clean_text(node.findtext("atom:title", default="", namespaces=ns))
                summary = clean_text(node.findtext("atom:summary", default="", namespaces=ns))
                link_node = node.find("atom:link", ns)
                link = link_node.attrib.get("href", "") if link_node is not None else ""
                published = clean_text(node.findtext("atom:updated", default="", namespaces=ns))
                if title and link:
                    items.append({"title": title, "link": link, "summary": summary, "published": published, "feed": url})
        return items
    except Exception as exc:
        print(f"Feed unavailable: {url} ({exc})")
        return []

def item_id(item):
    return hashlib.sha256((item["link"] or item["title"]).encode("utf-8")).hexdigest()

def make_original_caption(item):
    """
    Basic no-AI rewrite: uses a headline as a short attributed summary prompt.
    This intentionally does not copy the article body or images.
    It is a starter template, not a guarantee of copyright clearance or factual verification.
    """
    title = item["title"].strip().rstrip(".")
    domain = urlparse(item["link"]).netloc.replace("www.", "")
    summary = item.get("summary", "")
    # Keep any source summary very short; RSS descriptions may contain publisher-owned text.
    # Prefer a question/commentary caption based on the headline alone.
    if len(title) > 180:
        title = title[:177].rsplit(" ", 1)[0] + "..."
    return (
        f"📰 What do you think about this development?\n\n"
        f"{title}\n\n"
        f"Read the full report from {domain}: {item['link']}\n\n"
        f"💬 Share your thoughts in the comments."
    )

def publish_facebook(message):
    if not FB_PAGE_ID or not FB_PAGE_TOKEN:
        raise RuntimeError("Set FACEBOOK_PAGE_ID and FACEBOOK_PAGE_ACCESS_TOKEN as GitHub Actions secrets.")
    endpoint = f"https://graph.facebook.com/{GRAPH_VERSION}/{FB_PAGE_ID}/feed"
    response = requests.post(
        endpoint,
        data={"message": message, "access_token": FB_PAGE_TOKEN},
        timeout=TIMEOUT,
    )
    if not response.ok:
        raise RuntimeError(f"Facebook API error {response.status_code}: {response.text[:800]}")
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
