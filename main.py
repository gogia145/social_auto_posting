
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

# Auto-comment settings (override in GitHub Actions Variables if needed).
AUTO_COMMENT_ENABLED = os.getenv("AUTO_COMMENT_ENABLED", "true").lower() == "true"
AUTO_COMMENT_TEXT = os.getenv(
    "AUTO_COMMENT_TEXT",
    "Aapke hisaab se is update ka sabse bada impact kya hoga? Apni rai comment karein! 👇",
).strip()

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
