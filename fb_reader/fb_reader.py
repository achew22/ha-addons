#!/usr/bin/env python3
"""Mirror the newest posts of public Facebook pages into Home Assistant sensors
and Atom feeds.

Facebook sends logged-out requests from datacenter IPs to its login page, so
this is meant to run at home. Without a token it uses the same extraction as
RSS-Bridge's FacebookBridge: the desktop page ships the newest timeline post as
JSON inside a <script type="application/json"> element. With a Page access
token it reads the Graph API instead.

Standard library only.
"""

import datetime as dt
import html
import http.server
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

OPTIONS_FILE = os.environ.get("OPTIONS_FILE", "/data/options.json")
TZ = ZoneInfo(os.environ.get("TZ") or "UTC")
PORT = int(os.environ.get("PORT", "8884"))
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

states = {}
lock = threading.Lock()


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
    """Pages to read and the poll interval, from the add-on options or the env."""
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
    minutes = int(opts.get("poll_minutes") or os.environ.get("POLL_MINUTES") or 30)
    return pages, max(minutes, 5)


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


def push_to_ha(cfg, entry):
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return
    post = entry.get("post")
    attrs = {
        "friendly_name": f"Facebook {cfg['page']}",
        "icon": "mdi:facebook",
        "page": cfg["page"],
        "status": entry["status"],
        "error": entry["error"],
        "checked": entry["checked"],
    }
    if post:
        attrs.update({k: post.get(k) for k in
                      ("text", "url", "published", "is_today", "age_hours", "images", "id", "source")})
    text = (post or {}).get("text", "").strip()
    state = text.splitlines()[0][:250] if text else entry["status"]
    req = urllib.request.Request(
        f"http://supervisor/core/api/states/{cfg['sensor']}",
        data=json.dumps({"state": state, "attributes": attrs}).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:  # keep serving the feed even if HA is restarting
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
    with lock:
        entry = states.setdefault(page, {"post": None})
        entry.update(status=status, error=error, checked=dt.datetime.now(TZ).isoformat())
        if post:
            entry["post"] = post
        entry = dict(entry)
    push_to_ha(cfg, entry)


def poll_forever(pages, minutes):
    while True:
        for i, cfg in enumerate(pages):
            if i:
                time.sleep(20)  # don't hit Facebook with a burst
            check_page(cfg)
        time.sleep(minutes * 60)


def esc(s):
    return html.escape(s or "", quote=True)


def atom(page):
    with lock:
        entry = dict(states.get(page) or {})
    post = entry.get("post")
    updated = (post or {}).get("published") or entry.get("checked") or dt.datetime.now(TZ).isoformat()
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<feed xmlns="http://www.w3.org/2005/Atom">',
        f"<title>{esc(page)} (fb-reader)</title>",
        f'<link rel="alternate" href="https://www.facebook.com/{esc(page)}/"/>',
        f"<id>urn:fb-reader:{esc(page)}</id>",
        f"<updated>{esc(updated)}</updated>",
    ]
    if post:
        text = post["text"].strip()
        title = text.splitlines()[0][:100] if text else "Post"
        content = "<p>" + esc(post["text"]).replace("\n", "<br/>") + "</p>"
        content += "".join(f'<p><img src="{esc(u)}"/></p>' for u in post["images"])
        out += [
            "<entry>",
            f"<title>{esc(title)}</title>",
            f'<link href="{esc(post["url"])}"/>',
            f"<id>urn:fbpost:{esc(post['id'])}</id>",
            f"<published>{esc(post['published'])}</published>",
            f"<updated>{esc(post['published'])}</updated>",
            f'<content type="html">{esc(content)}</content>',
            "</entry>",
        ]
    out.append("</feed>")
    return "\n".join(out)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path.strip("/")
        if path.startswith("feed/") and path[5:] in states:
            body, ctype = atom(path[5:]).encode(), "application/atom+xml; charset=utf-8"
        elif path == "status":
            with lock:
                body = json.dumps(states, indent=2).encode()
            ctype = "application/json"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    pages, minutes = load_options()
    if not pages:
        log("no pages configured; add some under the app's Configuration tab")
    for cfg in pages:
        states[cfg["page"]] = {"post": None, "status": "starting", "error": None, "checked": None}
    if "--once" in sys.argv:
        for cfg in pages:
            check_page(cfg)
        print(json.dumps(states, indent=2))
        return
    log(f"reading {', '.join(c['page'] for c in pages) or 'nothing'} every {minutes} min; "
        f"serving /feed/<page> and /status on :{PORT}")
    threading.Thread(target=poll_forever, args=(pages, minutes), daemon=True).start()
    http.server.ThreadingHTTPServer(("", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
