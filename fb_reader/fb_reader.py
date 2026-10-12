#!/usr/bin/env python3
"""Read the newest post of each configured public Facebook page into a Home
Assistant sensor. Runs once and exits; the app's cron runs it on a schedule.

Facebook sends logged-out requests from datacenter IPs to its login page, so
this is meant to run at home. Without a token it uses the same extraction as
RSS-Bridge's FacebookBridge: the desktop page ships the newest timeline post as
JSON inside a <script type="application/json"> element. With a Page access
token it reads the Graph API instead.

Standard library only.
"""

import datetime as dt
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

OPTIONS_FILE = os.environ.get("OPTIONS_FILE", "/data/options.json")
TZ = ZoneInfo(os.environ.get("TZ") or "UTC")
GRAPH_VERSION = os.environ.get("GRAPH_VERSION", "v23.0")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Linux"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


class Blocked(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def log(msg):
    print(f"{dt.datetime.now(TZ):%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def slugify(page):
    return re.sub(r"[^a-z0-9]+", "_", page.lower()).strip("_")


def load_options():
    """Pages to read, from the add-on options or the env."""
    opts = {}
    try:
        with open(OPTIONS_FILE) as f:
            opts = json.load(f)
    except (OSError, ValueError):
        pass
    entries = opts.get("pages") or []
    if not entries and os.environ.get("FB_PAGES"):
        entries = os.environ["FB_PAGES"].split(",")
    pages = []
    for p in entries:
        if isinstance(p, str):
            p = {"page": p}
        name = (p.get("page") or "").strip()
        name = re.sub(r"^https?://(www\.|m\.)?facebook\.com/", "", name).strip("/")
        if not name:
            continue
        pages.append({
            "page": name,
            "token": (p.get("token") or "").strip(),
            "sensor": f"sensor.fb_{slugify(p.get('sensor') or name)}",
        })
    return pages


def fetch_page(page):
    req = urllib.request.Request(f"https://www.facebook.com/{page}/", headers=HEADERS)
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(req, timeout=30) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            raise Blocked(f"redirected to {e.headers.get('Location', '?')}")
        raise


def find_first(data, key):
    """Depth-first search for the first value stored under key."""
    stack = [data]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if key in cur:
                return cur[key]
            stack.extend(v for v in cur.values() if isinstance(v, (dict, list)))
        elif isinstance(cur, list):
            stack.extend(v for v in cur if isinstance(v, (dict, list)))
    return None


SCRIPT_RE = re.compile(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', re.S)


def extract_posts(page_html, page):
    if 'property="og:title"' not in page_html:
        raise Blocked("login wall (no og:title on the page)")
    nodes = []
    for raw in SCRIPT_RE.findall(page_html):
        if "timeline_list_feed_units" not in raw:
            continue
        try:
            data = json.loads(html.unescape(raw))
        except json.JSONDecodeError:
            continue
        stack = [data]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                edges = (cur.get("timeline_list_feed_units") or {}).get("edges")
                if isinstance(edges, list):
                    nodes.extend(e["node"] for e in edges if "node" in e)
                stack.extend(v for v in cur.values() if isinstance(v, (dict, list)))
            elif isinstance(cur, list):
                stack.extend(cur)
    posts = []
    for node in nodes:
        story = ((node.get("comet_sections") or {}).get("content") or {}).get("story") or {}
        message = find_first(story, "message")
        text = message.get("text", "") if isinstance(message, dict) else ""
        created = node.get("creation_time")
        images = []
        for att in node.get("attachments") or []:
            img = find_first(att, "photo_image")
            if isinstance(img, dict) and img.get("uri"):
                images.append(img["uri"])
        posts.append({
            "id": str(node.get("post_id") or created),
            "url": find_first(story, "wwwURL") or f"https://www.facebook.com/{page}/",
            "text": text,
            "created": int(created) if created else None,
            "images": images,
        })
    if not posts:
        raise ValueError("page loaded but no timeline post found in it")
    return posts


def fetch_graph(page, token):
    """Newest posts via the Graph API. Needs a Page access token for a page the
    token's user has a role on (pages_read_engagement); reading other pages needs
    Page Public Content Access, which takes Meta app review."""
    q = urllib.parse.urlencode({
        "fields": "id,message,created_time,permalink_url,full_picture",
        "limit": "5",
        "access_token": token,
    })
    try:
        with urllib.request.urlopen(
            f"https://graph.facebook.com/{GRAPH_VERSION}/{page}/posts?{q}", timeout=30
        ) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        raise Blocked(f"Graph API {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")
    posts = []
    for d in data.get("data", []):
        created = dt.datetime.strptime(d["created_time"], "%Y-%m-%dT%H:%M:%S%z")
        posts.append({
            "id": d["id"],
            "url": d.get("permalink_url") or f"https://www.facebook.com/{page}/",
            "text": d.get("message", ""),
            "created": int(created.timestamp()),
            "images": [d["full_picture"]] if d.get("full_picture") else [],
        })
    if not posts:
        raise ValueError("Graph API returned no posts")
    return posts


def enrich(post):
    local = dt.datetime.fromtimestamp(post["created"], TZ) if post["created"] else None
    post["published"] = local.isoformat() if local else None
    post["is_today"] = bool(local) and local.date() == dt.datetime.now(TZ).date()
    post["age_hours"] = (
        round((time.time() - post["created"]) / 3600, 1) if post["created"] else None
    )
    return post


def ha_request(method, path, body=None):
    req = urllib.request.Request(
        f"http://supervisor/core/api/{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {os.environ['SUPERVISOR_TOKEN']}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def push_to_ha(cfg, post, status, error):
    """Write the sensor. On a failed check, keep the last good post's attributes
    and only update status, error and checked."""
    if not os.environ.get("SUPERVISOR_TOKEN"):
        return
    path = f"states/{cfg['sensor']}"
    attrs, state = {}, status
    if post is None:
        try:
            old = ha_request("GET", path)
            attrs, state = old.get("attributes") or {}, old.get("state") or status
        except Exception:
            pass
    else:
        attrs = {k: post.get(k) for k in
                 ("text", "url", "published", "is_today", "age_hours", "images", "id", "source")}
        text = post.get("text", "").strip()
        state = text.splitlines()[0][:250] if text else "(no text)"
    attrs.update({
        "friendly_name": f"Facebook {cfg['page']}",
        "icon": "mdi:facebook",
        "page": cfg["page"],
        "status": status,
        "error": error,
        "checked": dt.datetime.now(TZ).isoformat(),
    })
    try:
        ha_request("POST", path, {"state": state, "attributes": attrs})
    except Exception as e:
        log(f"{cfg['page']}: could not update {cfg['sensor']}: {e}")


def check_page(cfg):
    page = cfg["page"]
    try:
        if cfg["token"]:
            posts, source = fetch_graph(page, cfg["token"]), "graph"
        else:
            posts, source = extract_posts(fetch_page(page), page), "page"
        posts.sort(key=lambda p: p["created"] or 0, reverse=True)
        post = enrich(posts[0])
        post["source"] = source
        status, error = "ok", None
        log(f"{page}: ok via {source}, post {post['id']} from {post['published']}")
    except Blocked as e:
        post, status, error = None, "blocked", str(e)
        log(f"{page}: blocked: {e}")
    except Exception as e:
        post, status, error = None, "error", f"{type(e).__name__}: {e}"
        log(f"{page}: error: {error}")
    push_to_ha(cfg, post, status, error)


def main():
    """One pass over every page; cron runs this on the configured schedule."""
    pages = load_options()
    if not pages:
        log("no pages configured; add some under the app's Configuration tab")
    for i, cfg in enumerate(pages):
        if i:
            time.sleep(20)  # don't hit Facebook with a burst
        check_page(cfg)


if __name__ == "__main__":
    main()
