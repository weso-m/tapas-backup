#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tapas_backup.py - one-shot archival backup for a Tapas.io series.

Archives, for every episode of a series:
  * episode info JSON,
  * the full server-rendered <article> HTML (novel prose + inline base64
    images; COMIC panels are downloaded from their signed data-src URLs,
    validated by magic bytes, localized in the article and logged in
    images.json),
  * every reader comment (all pages) and every reply in each comment thread.

Comment body HTML is preserved verbatim (entities like &rsquo; / &quot; stay
intact); only the surrounding <div> is stripped.

Stdlib only (urllib.request + regex/html parsing). Python 3.8+.

Resumable: manifest.json records every completed episode; re-runs skip them.
Atomic manifest writes (temp file + os.replace). Global polite rate limiting.

Endpoints (all verified anonymously, 2026-09-24):
  1. GET /series/{sid}                      JSON series meta (requires Accept:
                                            application/json + X-Requested-With)
  2. GET /series/{sid}/episodes?page=N&sort=OLDEST&last_access=0&max_limit=20
                                            JSON w/ HTML-in-JSON episode list.
                                            eid=<first episode id> is passed on
                                            pages 2+ (optional). max_limit MUST
                                            stay 20 (100 -> HTTP 500).
  3. GET /episode/{eid}/info                JSON episode meta (comment_cnt
                                            includes replies)
  4. GET /episode/{eid}                     HTML page; body is
                                            <article ... js-episode-article ...>
  5. GET /comment/{eid}?page=N&sort=NEWEST&since=0&init_load=0&wr=true&ep=false
                                            JSON w/ HTML-in-JSON comments
                                            (10 top-level rows/page)
  6. GET /comment/{eid}/{cid}/replies?page=N
                                            JSON w/ HTML-in-JSON reply rows
                                            (up to 20/page, flat within thread)
  7. GET <signed us-a.tapas.io panel URL>   comic panels: the URL sits in the
                                            data-src="" of <img class="js-lazy">
                                            inside the article (src is only a
                                            ~50-byte placeholder GIF). The attr
                                            value is HTML-escaped (&amp; -> &);
                                            the signed URL EXPIRES ~5h after the
                                            episode page fetch (exp= epoch in
                                            __token__), so panels must download
                                            in the same run as the article.

Usage:
  python tapas_backup.py --series 123456 --out archive
  python tapas_backup.py --series 123456 --out archive --episode-range 1-3 \
      --limit-comments-pages 4
"""

import argparse
import html as html_mod
import json
import os
import random
import re
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = "https://tapas.io"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
JSON_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}

MAX_LIST_PAGES = 300      # 862 eps / 20 per page = 44; generous ceiling
MAX_COMMENT_PAGES = 500   # 10 comments/page  -> 5000 comments ceiling
MAX_REPLY_PAGES = 100     # 20 replies/page   -> 2000 replies per thread


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg):
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode("ascii"), flush=True)


# --------------------------------------------------------------------------
# HTTP layer: global rate limit + retries with exponential backoff
# --------------------------------------------------------------------------

class FetchError(Exception):
    pass


class Fetcher:
    def __init__(self, rate=2.0, max_tries=4, timeout=30, cookie=None, jitter=0.25):
        self.min_interval = 1.0 / rate if rate and rate > 0 else 0.0
        self.max_tries = max(1, max_tries)
        self.timeout = timeout
        self.cookie = cookie
        self.jitter = jitter
        self.last_ts = 0.0
        self.requests = 0
        self.retries = 0

    def _throttle(self):
        if self.min_interval <= 0:
            return
        wait = self.min_interval - (time.monotonic() - self.last_ts)
        if wait > 0:
            time.sleep(wait * (1.0 + random.uniform(0.0, self.jitter)))
        self.last_ts = time.monotonic()

    def _backoff(self, attempt):
        time.sleep((2 ** (attempt - 1)) * (1.0 + random.uniform(0.0, 0.5)))
        self.retries += 1

    def get(self, url, as_json=False, referer=None):
        headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
        if referer:
            headers["Referer"] = referer
        if as_json:
            headers.update(JSON_HEADERS)
        else:
            headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        if self.cookie:
            headers["Cookie"] = self.cookie

        last_err = None
        for attempt in range(1, self.max_tries + 1):
            self._throttle()
            self.requests += 1
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                text = raw.decode("utf-8", "replace")
                if as_json:
                    text = text.strip()
                    obj = json.loads(text)  # JSONDecodeError -> retryable
                    if obj.get("code") is not None and obj.get("code") != 200:
                        raise FetchError("endpoint code=%s msg=%r for %s"
                                         % (obj.get("code"), obj.get("msg"), url))
                    return obj
                return text
            except urllib.error.HTTPError as e:
                if e.code == 429 or e.code >= 500:
                    last_err = FetchError("HTTP %d for %s" % (e.code, url))
                    if attempt == self.max_tries:
                        raise last_err
                    self._backoff(attempt)
                    continue
                raise FetchError("HTTP %d (non-retryable) for %s" % (e.code, url))
            except (urllib.error.URLError, socket.timeout, TimeoutError,
                    ConnectionError, json.JSONDecodeError, ValueError, OSError) as e:
                last_err = FetchError("%s: %s for %s" % (type(e).__name__, e, url))
                if attempt == self.max_tries:
                    raise last_err
                self._backoff(attempt)
        raise last_err  # unreachable

    def get_bytes(self, url, referer=None):
        """Binary GET (comic panel images) with the same throttle/retry policy
        as get(): global rate limit, exponential backoff on 429/5xx/network."""
        headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
                   "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"}
        if referer:
            headers["Referer"] = referer
        if self.cookie:
            headers["Cookie"] = self.cookie
        last_err = None
        for attempt in range(1, self.max_tries + 1):
            self._throttle()
            self.requests += 1
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.read()
            except urllib.error.HTTPError as e:
                if e.code == 429 or e.code >= 500:
                    last_err = FetchError("HTTP %d for %s" % (e.code, url))
                    if attempt == self.max_tries:
                        raise last_err
                    self._backoff(attempt)
                    continue
                raise FetchError("HTTP %d (non-retryable) for %s" % (e.code, url))
            except (urllib.error.URLError, socket.timeout, TimeoutError,
                    ConnectionError, OSError) as e:
                last_err = FetchError("%s: %s for %s" % (type(e).__name__, e, url))
                if attempt == self.max_tries:
                    raise last_err
                self._backoff(attempt)
        raise last_err  # unreachable


# --------------------------------------------------------------------------
# Atomic file writes
# --------------------------------------------------------------------------

def atomic_write_text(path, text):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path, obj):
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=1))


def atomic_write_bytes(path, data):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# HTML parsing helpers (regex + depth-aware tag walking; stdlib only)
# --------------------------------------------------------------------------

def maybe_unescape(h):
    """The HTML-in-JSON fields are normally raw HTML. Some responses come fully
    entity-escaped; unescape only when clearly escaped (has &lt; but no raw <)."""
    if h and "&lt;" in h and "<" not in h:
        return html_mod.unescape(h)
    return h


def element_inner(text, start, tag):
    """Inner HTML of the element opened just before `start`, depth-aware for
    the given tag. Returns None if unbalanced."""
    pat = re.compile(r"<(/?)" + re.escape(tag) + r"\b[^>]*?(/?)>", re.IGNORECASE | re.DOTALL)
    depth = 1
    for m in pat.finditer(text, start):
        if m.group(1):          # closing tag
            depth -= 1
        elif not m.group(2):    # opening tag (ignore self-closing)
            depth += 1
        if depth == 0:
            return text[start:m.start()]
    return None


def tag_attr(tag_text, name):
    m = re.search(name + r'\s*=\s*"([^"]*)"', tag_text)
    return m.group(1) if m else None


def strip_tags(s):
    return re.sub(r"<[^>]+>", "", s)


ARTICLE_RE = re.compile(r"<article\b[^>]*>", re.IGNORECASE | re.DOTALL)


def extract_article(page_html):
    """Return (full_element_html, inner_html) of the js-episode-article element,
    or (None, None)."""
    matches = list(ARTICLE_RE.finditer(page_html))
    chosen = None
    for m in matches:
        if "js-episode-article" in m.group(0):
            chosen = m
            break
    if chosen is None and matches:
        chosen = matches[0]  # fallback: first <article>
    if chosen is None:
        return None, None
    inner = element_inner(page_html, chosen.end(), "article")
    if inner is None:
        return None, None
    full = page_html[chosen.start():chosen.end() + len(inner) + len("</article>")]
    return full, inner


PARENT_ROW_RE = re.compile(r"<div\b[^>]*js-comment-parent-row[^>]*>", re.IGNORECASE)
REPLY_ROW_RE = re.compile(r"<div\b[^>]*js-comment-reply[^>]*>", re.IGNORECASE)
ROW_ID_RE = re.compile(r'id="comment-row-(\d+)"')
NAME_RE = re.compile(r'<a\b(?P<attrs>[^>]*\bwriter__name[^>]*)>(?P<inner>.*?)</a>',
                     re.IGNORECASE | re.DOTALL)
DATE_RE = re.compile(r'<p\b[^>]*class="[^"]*writer__date[^"]*"[^>]*>([^<]*)</p>')
BODY_OPEN_RE = re.compile(r"<div\b[^>]*js-comment-body[^>]*>", re.IGNORECASE)
CREATOR_RE = re.compile(r'class="[^"]*writer__label')
TOGGLE_BTN_RE = re.compile(r"<a\b[^>]*js-toggle-reply-btn[^>]*>", re.IGNORECASE)


def _parse_common(chunk, row_id):
    rec = {"id": row_id, "username": None, "display_name": "",
           "date": None, "text": None, "likes": 0, "is_creator": False}
    m = NAME_RE.search(chunk)
    if m:
        href = tag_attr(m.group("attrs"), "href") or ""
        rec["username"] = urllib.parse.unquote(href.lstrip("/")) or None
        rec["display_name"] = html_mod.unescape(strip_tags(m.group("inner"))).strip()
    m = DATE_RE.search(chunk)
    if m:
        rec["date"] = html_mod.unescape(m.group(1)).strip()
    m = BODY_OPEN_RE.search(chunk)
    if m:
        inner = element_inner(chunk, m.end(), "div")
        if inner is None:  # unbalanced fallback
            fm = re.compile(r"</div>").search(chunk, m.end())
            inner = chunk[m.end():fm.start()] if fm else ""
        # verbatim inner HTML: entities (&rsquo; &quot; &amp;) preserved as-is
        rec["text"] = inner.strip()
    # likes: try id-anchored anchor tag in both attr orders, then the span,
    # then the first data-cnt in the chunk (reply rows lack comment-like id)
    for pat in (r'id="comment-like-%s"[^>]*?data-cnt="(\d+)"' % row_id,
                r'data-cnt="(\d+)"[^>]*?id="comment-like-%s"' % row_id,
                r'<span class="js-like-cnt">\s*(\d+)\s*</span>',
                r'data-cnt="(\d+)"'):
        lm = re.search(pat, chunk)
        if lm:
            try:
                rec["likes"] = int(lm.group(1))
            except (TypeError, ValueError):
                pass
            break
    rec["is_creator"] = bool(CREATOR_RE.search(chunk))
    return rec


def parse_comment_rows(h):
    """Parse top-level comment rows. Rows are split on the parent-row anchor so
    injected markup between rows can never merge two comments."""
    rows = []
    anchors = list(PARENT_ROW_RE.finditer(h))
    for i, m in enumerate(anchors):
        end = anchors[i + 1].start() if i + 1 < len(anchors) else len(h)
        chunk = h[m.start():end]
        idm = ROW_ID_RE.search(m.group(0)) or ROW_ID_RE.search(chunk)
        if not idm:
            continue
        rec = _parse_common(chunk, int(idm.group(1)))
        rec["replies"] = []
        # reply-toggle button: prefer the one whose data-id matches this row
        reply_cnt, found_matching = 0, False
        for bm in TOGGLE_BTN_RE.finditer(chunk):
            cnt = tag_attr(bm.group(0), "data-reply-cnt")
            did = tag_attr(bm.group(0), "data-id")
            if did is not None and str(did) == str(rec["id"]):
                reply_cnt = int(cnt) if cnt and cnt.isdigit() else 0
                found_matching = True
                break
            if cnt and cnt.isdigit() and not found_matching:
                reply_cnt = int(cnt)
        rec["reply_cnt"] = reply_cnt
        rows.append(rec)
    return rows


def parse_reply_rows(h):
    rows = []
    anchors = list(REPLY_ROW_RE.finditer(h))
    for i, m in enumerate(anchors):
        end = anchors[i + 1].start() if i + 1 < len(anchors) else len(h)
        chunk = h[m.start():end]
        idm = ROW_ID_RE.search(m.group(0)) or ROW_ID_RE.search(chunk)
        if not idm:
            continue
        rows.append(_parse_common(chunk, int(idm.group(1))))
    return rows


def parse_episode_ids(body_html):
    """Ordered, de-duplicated episode ids from the episode-list HTML body."""
    ids, seen = [], set()
    for m in re.finditer(r'data-href="/episode/(\d+)"', body_html):
        eid = m.group(1)
        if eid not in seen:
            seen.add(eid)
            ids.append(eid)
    return ids


# --------------------------------------------------------------------------
# Comic panel images (lazy-load data-src -> signed, expiring CDN URLs)
# --------------------------------------------------------------------------

# Comics embed panels as <img class="content__img js-lazy" src="<~50-byte
# inline base64 placeholder GIF>" data-src="<signed us-a.tapas.io URL>">.
# The data-src value is HTML-escaped (&amp;) and its token expires ~5h after
# the episode page fetch, so panels must download in the same run as the page.
PANEL_IMG_RE = re.compile(
    r'<img\b[^>]*?\bdata-src\s*=\s*(?P<q>["\'])(?P<url>[^"\'>]*?)(?P=q)[^>]*>',
    re.IGNORECASE | re.DOTALL)

IMG_EXT_OK = ("png", "jpg", "jpeg", "gif")
MIN_PANEL_BYTES = 1000    # a real panel; the inline placeholder GIF is ~50 B


def find_panels(article_html):
    """Every <img> tag carrying a data-src attribute, in document order.
    Returns [{"url": <unescaped signed URL>, "tag": <full tag text>}]."""
    out = []
    for m in PANEL_IMG_RE.finditer(article_html or ""):
        out.append({"url": html_mod.unescape(m.group("url")), "tag": m.group(0)})
    return out


def sniff_image(data):
    """Image type from magic bytes: 'png' / 'jpg' / 'gif', else None."""
    if not data:
        return None
    if data.startswith(b"\x89PNG"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"GIF8"):
        return "gif"
    return None


def valid_image_bytes(data):
    """A panel download is good only with a real image magic AND a size that
    rules out the ~50-byte inline placeholder GIF."""
    return bool(data) and len(data) > MIN_PANEL_BYTES and sniff_image(data) is not None


def panel_ext_from_url(url):
    """Local file extension taken from the URL path (.png/.jpg/.jpeg/.gif);
    anything else falls back to .png (magic-byte validation gates the bytes)."""
    try:
        path = urllib.parse.urlparse(url).path
    except ValueError:
        path = ""
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    return ext if ext in IMG_EXT_OK else "png"


def rewrite_panel_tag(tag_text, local_path):
    """Point one panel <img> at its local file: src -> local_path (replacing
    the placeholder/data-URI src), data-src removed, all other attributes and
    their order preserved verbatim."""
    t = re.sub(r"\s+data-src\s*=\s*(\"[^\"]*\"|'[^']*')", "", tag_text, count=1)
    if re.search(r'\bsrc\s*=\s*"', t):
        t = re.sub(r'(\bsrc\s*=\s*")([^"]*)(")',
                   lambda mm: mm.group(1) + local_path + mm.group(3), t, count=1)
    elif re.search(r"\bsrc\s*=\s*'", t):
        t = re.sub(r"(\bsrc\s*=\s*')([^']*)(')",
                   lambda mm: mm.group(1) + local_path + mm.group(3), t, count=1)
    else:
        t = t[:4] + ' src="%s"' % local_path + t[4:]     # insert after <img
    return t


def rewrite_article_panels(article_html, panels, prov):
    """Rewrite every panel img to its local images/ path. `panels` and `prov`
    are parallel lists in document order; entries marked ok=False (failed
    download) are left untouched so a dead local link is never written."""
    if not panels:
        return article_html
    matches = list(PANEL_IMG_RE.finditer(article_html))
    out, last = [], 0
    for i, m in enumerate(matches):
        p = prov[i] if i < len(prov) else None
        out.append(article_html[last:m.start()])
        out.append(rewrite_panel_tag(m.group(0), p["file"])
                   if (p and p.get("ok")) else m.group(0))
        last = m.end()
    out.append(article_html[last:])
    return "".join(out)


def download_panels(fetcher, ep_dir, panels, referer=None):
    """Download every panel into episodes/<...>/images/, validating each file
    by magic bytes; a ~50-byte placeholder response is a failure, retried once
    and then reported. Panels whose local file exists AND validates are
    skipped (resume safety -- the caller re-fetches the episode page, so the
    URLs seen here are always freshly signed). Returns (prov, failed): prov
    has one entry per panel in document order with an "ok" flag; failed lists
    the panels that could not be fetched/validated."""
    img_dir = os.path.join(ep_dir, "images")
    os.makedirs(img_dir, exist_ok=True)
    prev = {}   # file -> url provenance recorded by a previous partial run
    prev_path = os.path.join(ep_dir, "images.json")
    if os.path.isfile(prev_path):
        try:
            with open(prev_path, "r", encoding="utf-8") as f:
                for e in json.load(f):
                    if isinstance(e, dict) and e.get("file"):
                        prev[e["file"]] = e.get("url")
        except (OSError, ValueError):
            prev = {}
    prov, failed = [], []
    for i, p in enumerate(panels, 1):
        url = p["url"]
        rel = "images/panel-%03d.%s" % (i, panel_ext_from_url(url))
        apath = os.path.join(ep_dir, "images", os.path.basename(rel))
        data = None
        if os.path.isfile(apath):
            try:
                with open(apath, "rb") as f:
                    data = f.read()
            except OSError:
                data = None
        if data is not None and valid_image_bytes(data):
            prov.append({"panel": i, "url": prev.get(rel) or url,
                         "file": rel, "bytes": len(data), "ok": True})
            continue
        ok = False
        for attempt in (1, 2):      # one extra try if the body was a placeholder
            try:
                data = fetcher.get_bytes(url, referer=referer)
            except FetchError:
                data = None
            if data is not None and valid_image_bytes(data):
                ok = True
                break
            if attempt == 1:
                time.sleep(1.0)
        if not ok:
            prov.append({"panel": i, "url": url, "file": rel,
                         "bytes": len(data or b""), "ok": False})
            failed.append({"panel": i, "url": url, "file": rel,
                           "reason": "download failed or placeholder/invalid image bytes"})
            continue
        atomic_write_bytes(apath, data)
        prov.append({"panel": i, "url": url, "file": rel,
                     "bytes": len(data), "ok": True})
    return prov, failed


# --------------------------------------------------------------------------
# Paginated walks
# --------------------------------------------------------------------------

def _next_page(pg, page):
    """pagination['page'] reports the NEXT page number on tapas; use it with a
    guard so a server hiccup can never loop forever."""
    nxt = pg.get("page")
    if isinstance(nxt, int) and nxt > page:
        return nxt
    return page + 1


def walk_episode_list(fetcher, sid):
    ids, seen = [], set()
    page, has_next, pages = 1, True, 0
    total = None
    while has_next and pages < MAX_LIST_PAGES:
        if page == 1:
            url = ("%s/series/%s/episodes?page=1&sort=OLDEST&last_access=0&max_limit=20"
                   % (BASE, sid))
        else:
            url = ("%s/series/%s/episodes?eid=%s&page=%d&sort=OLDEST&last_access=0&max_limit=20"
                   % (BASE, sid, ids[0], page))
        j = fetcher.get(url, as_json=True)
        data = j.get("data") or {}
        pg = data.get("pagination") or {}
        total = pg.get("total", total)
        body = maybe_unescape(data.get("body") or "")
        new = 0
        for m in re.finditer(r'data-href="/episode/(\d+)"', body):
            if m.group(1) not in seen:
                seen.add(m.group(1))
                ids.append(m.group(1))
                new += 1
        pages += 1
        if new == 0:
            has_next = False
            break
        has_next = bool(pg.get("has_next"))
        page = _next_page(pg, page)
    complete = (not has_next) and pages < MAX_LIST_PAGES
    return ids, pages, total, complete


def fetch_comment_pages(fetcher, ep_id, limit_pages=None):
    """All top-level comments for an episode. Returns (rows, pages, capped)."""
    comments, pages, capped = [], 0, False
    page, has_next = 1, True
    while has_next:
        if limit_pages is not None and pages >= limit_pages:
            capped = True
            break
        if pages >= MAX_COMMENT_PAGES:
            capped = True
            break
        url = ("%s/comment/%s?page=%d&sort=NEWEST&since=0&init_load=0&wr=true&ep=false"
               % (BASE, ep_id, page))
        j = fetcher.get(url, as_json=True, referer="%s/episode/%s" % (BASE, ep_id))
        data = j.get("data") or {}
        pg = data.get("pagination") or {}
        h = maybe_unescape(data.get("html") or "")
        rows = parse_comment_rows(h)
        if not rows:
            break
        comments.extend(rows)
        pages += 1
        has_next = bool(pg.get("has_next"))
        page = _next_page(pg, page)
    return comments, pages, capped


def fetch_replies(fetcher, ep_id, comment_id, limit_pages=None):
    """All replies of one comment thread. Returns (rows, pages, capped)."""
    replies, pages, capped = [], 0, False
    page, has_next = 1, True
    while has_next:
        if limit_pages is not None and pages >= limit_pages:
            capped = True
            break
        if pages >= MAX_REPLY_PAGES:
            capped = True
            break
        url = "%s/comment/%s/%s/replies?page=%d" % (BASE, ep_id, comment_id, page)
        j = fetcher.get(url, as_json=True, referer="%s/episode/%s" % (BASE, ep_id))
        data = j.get("data") or {}
        pg = data.get("pagination") or {}
        h = maybe_unescape(data.get("html") or "")
        rows = parse_reply_rows(h)
        if not rows:
            break
        replies.extend(rows)
        pages += 1
        has_next = bool(pg.get("has_next"))
        page = _next_page(pg, page)
    return replies, pages, capped


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------

def manifest_path(out):
    return os.path.join(out, "manifest.json")


def load_manifest(out, sid):
    path = manifest_path(out)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                m = json.load(f)
            if m.get("series_id") == sid:
                m.setdefault("episodes", {})
                m.setdefault("gaps", [])
                m.setdefault("warnings", [])
                return m
            log("[manifest] existing manifest is for series %s; starting fresh" % m.get("series_id"))
        except (OSError, ValueError) as e:
            log("[manifest] unreadable (%s); starting fresh" % e)
    return {"series_id": sid, "created": now_iso(), "updated": now_iso(),
            "episodes": {}, "gaps": [], "warnings": []}


def save_manifest(out, m):
    m["updated"] = now_iso()
    atomic_write_json(manifest_path(out), m)


def add_gap(m, num, eid, reason):
    for g in m["gaps"]:
        if g.get("episode_id") == eid and g.get("reason") == reason:
            return
    m["gaps"].append({"num": num, "episode_id": eid, "reason": reason, "ts": now_iso()})


def add_warning(m, msg):
    if len(m["warnings"]) < 300:
        m["warnings"].append(msg)
    elif len(m["warnings"]) == 300:
        m["warnings"].append("(warning list capped)")


def write_gaps(out, m):
    lines = ["# gaps for series %s - episodes locked (free=false) or failed after retries"
             % m.get("series_id"), "# generated %s" % now_iso(), ""]
    for g in m["gaps"]:
        lines.append("ep %s (id %s): %s" % (g.get("num"), g.get("episode_id"), g.get("reason")))
    atomic_write_text(os.path.join(out, "gaps.txt"), "\n".join(lines) + "\n")


# --------------------------------------------------------------------------
# Episode processing
# --------------------------------------------------------------------------

def episode_dir(out, num, eid):
    return os.path.join(out, "episodes", "%04d-%s" % (num, eid))


def to_json_comment(c):
    return {"id": c["id"], "username": c["username"], "display_name": c["display_name"],
            "date": c["date"], "text": c["text"], "likes": c["likes"],
            "is_creator": c["is_creator"],
            "replies": [to_json_reply(r) for r in c["replies"]]}


def to_json_reply(r):
    return {"id": r["id"], "username": r["username"], "display_name": r["display_name"],
            "date": r["date"], "text": r["text"], "likes": r["likes"],
            "is_creator": r["is_creator"]}


def should_skip(out, manifest, num, eid, args):
    rec = manifest["episodes"].get(str(eid))
    if not rec or rec.get("status") != "complete":
        return False
    d = episode_dir(out, num, eid)
    if not os.path.isfile(os.path.join(d, "index.json")):
        return False
    if not os.path.isfile(os.path.join(d, "comments.json")):
        return False
    need_content = (not args.comments_only) and rec.get("free", True)
    if need_content and rec.get("content_saved") and not os.path.isfile(os.path.join(d, "article.html")):
        return False
    # comic episodes: every panel must be downloaded+validated (and the
    # provenance file present) before the episode counts as complete
    if rec.get("images", 0):
        if not rec.get("images_complete", True):
            return False
        ipath = os.path.join(d, "images.json")
        if not os.path.isfile(ipath):
            return False
        try:
            with open(ipath, "r", encoding="utf-8") as f:
                entries = json.load(f)
        except (OSError, ValueError):
            return False
        if len(entries) < rec.get("images", 0):
            return False
        for e in entries:      # disk must match provenance (exists, same size)
            try:
                if os.path.getsize(os.path.join(d, e.get("file") or "")) != e.get("bytes"):
                    return False
            except OSError:
                return False
    # partially-fetched comments (user limit): redo only if limits now allow more
    if rec.get("comments_capped"):
        lim = args.limit_comments_pages
        if lim is None or lim > rec.get("comment_pages", 0):
            return False
    if rec.get("replies_capped"):
        lim = args.limit_reply_pages
        if lim is None or lim > rec.get("reply_pages_max", 0):
            return False
    return True


def process_episode(fetcher, out, manifest, num, eid, args):
    d = episode_dir(out, num, eid)
    os.makedirs(d, exist_ok=True)
    rec = {"num": num, "episode_id": eid, "ts": now_iso(), "status": "complete"}

    # -- info --------------------------------------------------------------
    j = fetcher.get("%s/episode/%s/info" % (BASE, eid), as_json=True)
    info = j.get("data") or {}
    if not info:
        raise FetchError("episode info returned no data")
    atomic_write_json(os.path.join(d, "index.json"),
                      {"num": num, "episode_id": eid, "fetched_at": now_iso(), "data": info})
    free = bool(info.get("free"))
    open_comments = bool(info.get("open_comments"))
    comment_cnt = info.get("comment_cnt") or 0
    rec.update({"title": info.get("title"), "free": free,
                "open_comments": open_comments, "comment_cnt": comment_cnt})
    log("  info ok (free=%s, open_comments=%s, comment_cnt=%s)" % (free, open_comments, comment_cnt))

    # -- article -----------------------------------------------------------
    rec["content_saved"] = False
    rec["images"] = 0
    rec["images_complete"] = True
    if not args.comments_only:
        if free:
            page_html = fetcher.get("%s/episode/%s" % (BASE, eid))
            full, inner = extract_article(page_html)
            if full is None:
                add_gap(manifest, num, eid, "article element not found on free episode")
                rec["status"] = "error"
                rec["error"] = "no-article"
                return rec
            panels = find_panels(full)
            if panels:
                # Comic episode: the data-src URLs are signed and expire ~5h
                # after THIS page fetch, so panels must download in this run.
                # (On resume the episode page is re-fetched first, giving
                # fresh tokens; already-valid panel files are then skipped.)
                log("  article has %d comic panel image(s) -> downloading" % len(panels))
                prov, failed = download_panels(fetcher, d, panels,
                                               referer="%s/episode/%s" % (BASE, eid))
                full = rewrite_article_panels(full, panels, prov)
                ok = [e for e in prov if e.get("ok")]
                atomic_write_json(os.path.join(d, "images.json"),
                                  [{"panel": e["panel"], "url": e["url"],
                                    "file": e["file"], "bytes": e["bytes"]}
                                   for e in ok])
                rec["images"] = len(panels)
                rec["images_ok"] = len(ok)
                rec["images_complete"] = not failed
                if failed:
                    for fp in failed:
                        add_gap(manifest, num, eid,
                                "panel %d/%d failed (%s): %s"
                                % (fp["panel"], len(panels), fp["reason"], fp["url"]))
                    rec["status"] = "error"
                    rec["error"] = ("images-incomplete: %d of %d panels failed"
                                    % (len(failed), len(panels)))
                log("  panels: %d/%d downloaded + validated%s"
                    % (len(ok), len(panels), "  FAILED -> gaps.txt" if failed else ""))
            atomic_write_text(os.path.join(d, "article.html"), full)
            rec["content_saved"] = True
            rec["article_bytes"] = len(full.encode("utf-8"))
            log("  article %d bytes%s" % (rec["article_bytes"],
                                         " (panels localized)" if panels
                                         else " (with base64 images, verbatim)"))
        else:
            add_gap(manifest, num, eid, "locked (free=false): content not fetched; comments still archived")
            log("  LOCKED (free=false) -> content skipped, logged to gaps.txt")

    # -- comments ----------------------------------------------------------
    comments = []
    cpages = 0
    ccapped = False
    if comment_cnt > 0 or open_comments:
        comments, cpages, ccapped = fetch_comment_pages(fetcher, eid, args.limit_comments_pages)
    rec["comments"] = len(comments)
    rec["comment_pages"] = cpages
    rec["comments_capped"] = bool(ccapped)
    if ccapped:
        rec["partial"] = True

    # -- replies -----------------------------------------------------------
    reply_total = 0
    rpages_max = 0
    rcapped_any = False
    for c in comments:
        if c.get("reply_cnt", 0) > 0:
            reps, rpages, rcapped = fetch_replies(fetcher, eid, c["id"], args.limit_reply_pages)
            c["replies"] = reps
            reply_total += len(reps)
            rpages_max = max(rpages_max, rpages)
            if rcapped:
                rcapped_any = True
                add_gap(manifest, num, eid,
                        "reply thread %s truncated at page limit (%d fetched, site says %d)"
                        % (c["id"], len(reps), c["reply_cnt"]))
            elif len(reps) != c["reply_cnt"]:
                add_warning(manifest, "ep %s: thread %s fetched %d replies, site said %d"
                            % (eid, c["id"], len(reps), c["reply_cnt"]))
    rec["replies"] = reply_total
    rec["reply_pages_max"] = rpages_max
    rec["replies_capped"] = bool(rcapped_any)
    if rcapped_any:
        rec["partial"] = True

    # -- sanity checks -----------------------------------------------------
    if not ccapped and comment_cnt and (len(comments) + reply_total) != comment_cnt:
        add_warning(manifest, "ep %s: collected %d+%d=%d comments, info comment_cnt=%d "
                    "(deleted comments or counting quirk; comment_cnt includes replies)"
                    % (eid, len(comments), reply_total, len(comments) + reply_total, comment_cnt))

    atomic_write_json(os.path.join(d, "comments.json"),
                      [to_json_comment(c) for c in comments])
    log("  comments: %d top-level in %d page(s)%s; replies: %d%s"
        % (len(comments), cpages,
           " (page-limit reached)" if ccapped else "",
           reply_total, " (page-limit reached)" if rcapped_any else ""))
    return rec


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def parse_range(spec):
    m = re.match(r"^\s*(\d+)(?:\s*-\s*(\d+))?\s*$", spec or "")
    if not m:
        raise argparse.ArgumentTypeError("bad --episode-range (use N or N-M), got %r" % spec)
    lo = int(m.group(1))
    hi = int(m.group(2)) if m.group(2) else lo
    if lo < 1 or hi < lo:
        raise argparse.ArgumentTypeError("bad --episode-range %r" % spec)
    return lo, hi


def run(args):
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    fetcher = Fetcher(rate=args.rate, max_tries=args.max_tries,
                      timeout=args.timeout, cookie=args.cookie)
    manifest = load_manifest(out, args.series)

    # 1. series meta
    sj = os.path.join(out, "series.json")
    if os.path.isfile(sj) and not args.force:
        log("[series] series.json exists -> cached, not re-fetching")
        try:
            with open(sj, "r", encoding="utf-8") as f:
                sdata = json.load(f).get("data") or {}
            manifest["series_title"] = sdata.get("title")
        except (OSError, ValueError):
            pass
    else:
        j = fetcher.get("%s/series/%s" % (BASE, args.series), as_json=True)
        sdata = j.get("data") or {}
        atomic_write_json(sj, {"series_id": args.series, "fetched_at": now_iso(), "data": sdata})
        manifest["series_title"] = sdata.get("title")
        log("[series] %s (%s, url=%s)" % (sdata.get("title"), sdata.get("type"), sdata.get("url")))
        save_manifest(out, manifest)

    # 2. episode list
    if manifest.get("episode_ids") and not args.refresh_list and not args.force:
        ids = [str(i) for i in manifest["episode_ids"]]
        log("[list] %d episode ids (cached in manifest)" % len(ids))
    else:
        log("[list] walking episode list (max_limit=20, sort=OLDEST)...")
        raw_ids, pages, total, complete = walk_episode_list(fetcher, args.series)
        ids = raw_ids
        manifest["episode_ids"] = ids
        manifest["list_pages"] = pages
        manifest["list_complete"] = complete
        manifest["episodes_total_reported"] = total
        log("[list] %d ids over %d page(s); site total=%s; walk complete=%s"
            % (len(ids), pages, total, complete))
        if total and len(ids) < total:
            add_warning(manifest, "episode list walk got %d of %s reported" % (len(ids), total))
        save_manifest(out, manifest)

    # 3. selection
    lo, hi = parse_range(args.episode_range) if args.episode_range else (1, len(ids))
    if args.episode_range is None:
        lo, hi = 1, len(ids)
    hi = min(hi, len(ids))
    if args.limit_episodes is not None:
        hi = min(hi, lo + args.limit_episodes - 1) if lo > 1 else min(hi, args.limit_episodes)
    selected = [(n, ids[n - 1]) for n in range(lo, hi + 1)]
    if not selected:
        log("[select] nothing selected (range %d-%d, %d episodes known)" % (lo, hi, len(ids)))
    else:
        log("[select] episodes %d-%d: %d selected (first id %s, last id %s)"
            % (lo, hi, len(selected), selected[0][1], selected[-1][1]))

    processed = skipped = failed = 0
    for num, eid in selected:
        if not args.force and should_skip(out, manifest, num, eid, args):
            rec = manifest["episodes"].get(str(eid)) or {}
            log("[ep %d] id %s: skipped (already complete: content=%s, %s comments, %s replies, %s panel images)"
                % (num, eid, rec.get("content_saved"), rec.get("comments"),
                   rec.get("replies"), rec.get("images", 0)))
            skipped += 1
            continue
        log("[ep %d] id %s: fetching..." % (num, eid))
        try:
            rec = process_episode(fetcher, out, manifest, num, eid, args)
            manifest["episodes"][str(eid)] = rec
            if rec.get("status") == "complete":
                processed += 1
                log("[ep %d] done" % num)
            else:
                failed += 1
                log("[ep %d] INCOMPLETE: %s" % (num, rec.get("error")))
        except (FetchError, OSError) as e:
            failed += 1
            add_gap(manifest, num, eid, "fetch failed after retries: %s" % e)
            manifest["episodes"][str(eid)] = {"num": num, "episode_id": eid,
                                              "status": "error", "error": str(e), "ts": now_iso()}
            log("[ep %d] FAILED after retries: %s" % (num, e))
        save_manifest(out, manifest)

    write_gaps(out, manifest)
    log("")
    log("== summary ==")
    log("series: %s (id %s)" % (manifest.get("series_title"), args.series))
    log("episodes known: %d | selected: %d | processed this run: %d | skipped: %d | failed: %d"
        % (len(ids), len(selected), processed, skipped, failed))
    log("gaps recorded: %d | warnings: %d" % (len(manifest["gaps"]), len(manifest["warnings"])))
    log("HTTP requests this run: %d (retries: %d)" % (fetcher.requests, fetcher.retries))
    log("output dir: %s" % out)
    return 0 if failed == 0 else 1


# --------------------------------------------------------------------------
# Selftest (parser unit tests with real fixtures captured from tapas.io)
# --------------------------------------------------------------------------

FIXTURE_COMMENT_ROW = r'''<div class="comment-row-wrap js-comment-parent-row" id="comment-row-23291273">
            <div class="comment-row">
                <div class="row__writer">
                    <a class="writer__thumb" href="/sample_reader_01">
                        <img class="circle" src="https://lh3.googleusercontent.com/a/ACg8ocJ=s96-c" alt="SampleReader One">
                        <div class="thumb-overlay circle"></div>
                    </a>
                </div>
                <div class="row__body">
                    <div class="body__row" id="comment-box-23291273">
                        <div class="body__writer"><a class="writer__name" href="/sample_reader_01">SampleReader One</a><p class="writer__date">Jul 29, 2026</p></div>
                        <div class="body__comment js-comment-body">Rereading in 2026 and good Lordy</div>
                        <div class="body__info body__info--has-reply">
                            <a class="info__button js-have-to-sign "
                               id="comment-like-23291273"
                               data-id="23291273"
                               data-cnt="7"
                               data-permalink="/episode/1701734?_=_#comment-section"
                               data-where="Comment"><i class="ico"></i><span class="js-like-cnt">7</span></a>
                            <hr/>
                            <a class="info__button info__button--reply js-have-to-sign"
                               data-permalink="/episode/1701734"
                               data-where="Comment"
                               data-id="23291273"></a>
                        </div>
                        <div>
                            <a class="body__button js-toggle-reply-btn hidden"
                               data-reply-cnt="0" data-id="23291273">
                                <span class="button__label">View 0 reply</span></a>
                        </div>
                    </div>
                    <script type="text/template" id="tpCommentEdit23291273">
                        <div class="body__edit js-comment-edit-box js-comment-row">
                            <textarea class="autogrow js-edit-box">{{body}}</textarea>
                        </div>
                    </script>
                </div>
            </div>
            <div class="comment-row__reply hidden js-reply-list"></div>
        </div>'''

FIXTURE_COMMENT_ROW_2 = r'''<div class="comment-row-wrap js-comment-parent-row" id="comment-row-22283645">
            <div class="comment-row"><div class="row__writer"></div>
                <div class="row__body"><div class="body__row" id="comment-box-22283645">
                        <div class="body__writer"><a class="writer__name" href="/some_user">Some &amp; User</a><p class="writer__date">Sep 1, 2025</p></div>
                        <div class="body__comment js-comment-body">Loved the &ldquo;twist&rdquo; here<br/>so much</div>
                        <div class="body__info">
                            <a class="info__button" id="comment-like-22283645" data-id="22283645" data-cnt="12"><span class="js-like-cnt">12</span></a>
                        </div>
                        <div><a class="body__button js-toggle-reply-btn" data-reply-cnt="3" data-id="22283645"><span>View 3 replies</span></a></div>
                </div></div>
            </div>
        </div>'''

FIXTURE_REPLY_ROW = r'''<div class="reply-wrapper js-comment-reply" id="comment-row-22283646">
            <div></div>
            <div class="body__reply">
                <div class="reply__writer">
                    <a class="writer__thumb" href="/sample_reader_02">
                        <img class="circle" src="https://us-a.tapas.io/ua/x.jpg" alt="SampleReader Two">
                    </a>
                </div>
                <div class="reply__body" id="comment-box-22283646">
                    <div class="body__writer"><a class="writer__name" href="/sample_reader_02">SampleReader Two</a><p class="writer__date">Oct 12, 2025</p></div>
                    <div class="body__comment js-comment-body">Also, I like the ironoc twists in the writing. &quot;She would be the worst friend if...&quot; Happens.<br>
&quot;I wil never do another favor...&quot; Okay.</div>
                    <div class="body__info">
                        <a class="info__button js-have-to-sign "
                           data-id="22283646"
                           data-cnt="1"
                           data-permalink="/episode/1701734"
                           data-where="Comment"><span class="js-like-cnt">1</span></a>
                            <hr/>
                            <a class="info__button info__button--reply-conversation js-have-to-sign"
                               data-parent-id="22283645"></a>
                    </div>
                </div>
                <script type="text/template" id="tpCommentEdit22283646"><div class="body__edit">{{body}}</div></script>
            </div>
        </div>'''

FIXTURE_CREATOR_REPLY = r'''<div class="reply-wrapper js-comment-reply" id="comment-row-10555604">
            <div class="body__reply">
                <div class="reply__body" id="comment-box-10555604">
                    <div class="body__writer"><a class="writer__name" href="/sample_creator">SampleCreator</a><p class="writer__label">Creator</p><p class="writer__date">Jun 14, 2020</p></div>
                    <div class="body__comment js-comment-body">Omg, I love to hear that someone is re-reading!! &#128525;</div>
                </div>
            </div>
        </div>'''

FIXTURE_ARTICLE_PAGE = (
    '<html><body><div>noise</div>'
    '<article class="viewer__body js-episode-article main__body--book" style="font-family: Lato;">'
    '<p>First &amp; foremost&hellip;</p>'
    '<img src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAE=">'
    '<div class="wrap"><p>nested para</p></div>'
    '<p>Fuck. Fuck my life.&nbsp;</p>'
    '</article><div>comment avatar <img class="circle" src="https://us-a.tapas.io/ua/avatar.jpg"></div>'
    '</body></html>')

FIXTURE_LIST_BODY = (
    '<ul>'
    '<li class="js-tiara-tracking body__item--selected" data-href="/episode/1701734" data-id="1701734" id="ep-1701734" data-is-wait-or-pay="false"><span>Part One</span></li>'
    '<li class="js-tiara-tracking" data-href="/episode/1701769" data-id="1701769" id="ep-1701769"><span>Part Two</span></li>'
    '<li class="js-tiara-tracking" data-href="/episode/1701734" data-id="1701734"><span>dupe should be ignored</span></li>'
    '</ul>')

FIXTURE_COMIC_ARTICLE = (
    '<article class="viewer__body js-episode-article">'
    '<p>Episode text</p>'
    '<img class="content__img js-lazy" '
    'src="data:image/gif;base64,R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw==" '
    'data-src="https://us-a.tapas.io/pc/6c/76199a28-7750-48d3-81e5-8e82321214d4.png'
    '?__token__=exp=1790338722~acl=/pc/6c/76199a28-7750-48d3-81e5-8e82321214d4.png*'
    '~hmac=deadbeef&amp;version=v4">'
    '<img class="content__img js-lazy" '
    'src="data:image/gif;base64,R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw==" '
    'data-src="https://us-a.tapas.io/pc/7b/second-panel.jpg?__token__=exp=1790338722~acl=x~hmac=y&amp;version=v4">'
    '<p>The end.</p>'
    '</article>')


def selftest():
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))
        log("  %s %s" % ("PASS" if cond else "FAIL", name))

    # comment row parsing
    rows = parse_comment_rows(FIXTURE_COMMENT_ROW)
    check("comment: 1 row parsed", len(rows) == 1)
    c = rows[0] if rows else {}
    check("comment: id", c.get("id") == 23291273)
    check("comment: username", c.get("username") == "sample_reader_01")
    check("comment: display_name", c.get("display_name") == "SampleReader One")
    check("comment: date", c.get("date") == "Jul 29, 2026")
    check("comment: text verbatim", c.get("text") == "Rereading in 2026 and good Lordy")
    check("comment: likes via comment-like id", c.get("likes") == 7)
    check("comment: not creator", c.get("is_creator") is False)
    check("comment: reply_cnt 0", c.get("reply_cnt") == 0)

    # two adjacent rows must not merge; entity preservation
    both = FIXTURE_COMMENT_ROW + "\n" + FIXTURE_COMMENT_ROW_2
    rows2 = parse_comment_rows(both)
    check("two rows: no merging", len(rows2) == 2 and rows2[1]["id"] == 22283645)
    c2 = rows2[1] if len(rows2) > 1 else {}
    check("row2: entity text preserved", c2.get("text") == 'Loved the &ldquo;twist&rdquo; here<br/>so much')
    check("row2: display name unescaped", c2.get("display_name") == "Some & User")
    check("row2: likes", c2.get("likes") == 12)
    check("row2: reply_cnt 3", c2.get("reply_cnt") == 3)

    # reply row: entities + <br> preserved, likes via js-like-cnt fallback
    reps = parse_reply_rows(FIXTURE_REPLY_ROW)
    check("reply: 1 row parsed", len(reps) == 1)
    r = reps[0] if reps else {}
    check("reply: id", r.get("id") == 22283646)
    check("reply: username", r.get("username") == "sample_reader_02")
    check("reply: likes fallback span", r.get("likes") == 1)
    check("reply: entities intact", "&quot;She would be the worst friend if...&quot;" in (r.get("text") or ""))
    check("reply: <br> intact", "<br>" in (r.get("text") or ""))

    # creator label detection + numeric entity preserved
    creps = parse_reply_rows(FIXTURE_CREATOR_REPLY)
    cr = creps[0] if creps else {}
    check("creator reply: is_creator", cr.get("is_creator") is True)
    check("creator reply: username", cr.get("username") == "sample_creator")
    check("creator reply: numeric entity intact", "&#128525;" in (cr.get("text") or ""))

    # article extraction: full element, nested div, entities, base64 kept
    full, inner = extract_article(FIXTURE_ARTICLE_PAGE)
    check("article: found", full is not None)
    check("article: full starts with <article", bool(full and full.startswith("<article")))
    check("article: full ends with </article>", bool(full and full.rstrip().endswith("</article>")))
    check("article: nested div inner kept", bool(inner and '<div class="wrap"><p>nested para</p></div>' in inner))
    check("article: entity kept", bool(inner and "&hellip;" in inner))
    check("article: base64 kept", bool(inner and "data:image/gif;base64," in inner))
    check("article: avatar img NOT included", bool(full and "us-a.tapas.io" not in full))

    # episode list id extraction + dedupe
    ids = parse_episode_ids(FIXTURE_LIST_BODY)
    check("list: ordered dedupe", ids == ["1701734", "1701769"])

    # depth-aware element_inner
    check("element_inner: nested balance",
          element_inner('<div id="o"><div>x</div>y</div>', len('<div id="o">'), "div") == "<div>x</div>y")
    check("element_inner: unbalanced -> None",
          element_inner('<div id="o"><div>x</div>', len('<div id="o">'), "div") is None)

    # maybe_unescape
    check("maybe_unescape: raw html untouched",
          maybe_unescape('<div class="x">&lt;p&gt;</div>') == '<div class="x">&lt;p&gt;</div>')
    check("maybe_unescape: fully-escaped unescaped",
          maybe_unescape('&lt;div class=&quot;x&quot;&gt;') == '<div class="x">')

    # JSON shape
    shape = to_json_comment({"id": 1, "username": "u", "display_name": "d", "date": "x",
                            "text": "t", "likes": 2, "is_creator": False,
                            "replies": [{"id": 2, "username": "v", "display_name": "e",
                                         "date": "y", "text": "r", "likes": 0,
                                         "is_creator": True}]})
    check("json shape: keys", set(shape.keys()) ==
          {"id", "username", "display_name", "date", "text", "likes", "is_creator", "replies"})
    check("json shape: reply keys", set(shape["replies"][0].keys()) ==
          {"id", "username", "display_name", "date", "text", "likes", "is_creator"})

    # ---- comic panels: data-src extraction ----
    panels = find_panels(FIXTURE_COMIC_ARTICLE)
    check("panels: 2 found in order", len(panels) == 2)
    check("panels: url unescaped (&amp; -> &)", bool(panels) and panels[0]["url"] == (
        "https://us-a.tapas.io/pc/6c/76199a28-7750-48d3-81e5-8e82321214d4.png"
        "?__token__=exp=1790338722~acl=/pc/6c/76199a28-7750-48d3-81e5-8e82321214d4.png*"
        "~hmac=deadbeef&version=v4"))
    check("panels: second in document order",
          bool(panels) and panels[1]["url"].startswith("https://us-a.tapas.io/pc/7b/second-panel.jpg"))
    check("panels: full tag captured", bool(panels) and panels[0]["tag"].startswith("<img"))

    # ---- novel path: only base64 data-URI srcs -> zero panel downloads ----
    nfull, _inner = extract_article(FIXTURE_ARTICLE_PAGE)
    novel_panels = find_panels(nfull or "")
    check("novel: zero data-src panels", novel_panels == [])
    check("novel: article untouched when no panels",
          rewrite_article_panels(nfull, [], []) == nfull)
    check("novel: base64 src survives panel processing",
          bool(nfull) and "data:image/gif;base64," in rewrite_article_panels(nfull, [], []))

    class _BoomFetcher:                    # any panel fetch on a novel = failure
        def get_bytes(self, url, referer=None):
            raise AssertionError("novel articles must trigger zero panel fetches")

    _tmpd = tempfile.mkdtemp(prefix="tapas-selftest-")
    try:
        _prov_n, _fail_n = download_panels(_BoomFetcher(), _tmpd, novel_panels)
    finally:
        os.rmdir(os.path.join(_tmpd, "images"))
        os.rmdir(_tmpd)
    check("novel: download_panels fetches nothing", _prov_n == [] and _fail_n == [])

    # ---- article rewrite to local paths ----
    prov_stub = [
        {"panel": 1, "url": panels[0]["url"], "file": "images/panel-001.png", "bytes": 52818, "ok": True},
        {"panel": 2, "url": panels[1]["url"], "file": "images/panel-002.jpg", "bytes": 48011, "ok": True}]
    rw = rewrite_article_panels(FIXTURE_COMIC_ARTICLE, panels, prov_stub)
    check("rewrite: panel-001 localized", 'src="images/panel-001.png"' in rw)
    check("rewrite: panel-002 localized", 'src="images/panel-002.jpg"' in rw)
    check("rewrite: data-src removed", "data-src" not in rw)
    check("rewrite: other attributes kept", 'class="content__img js-lazy"' in rw)
    check("rewrite: placeholder base64 gone", "data:image/gif;base64" not in rw)
    check("rewrite: prose kept", "Episode text" in rw and "The end." in rw)
    prov_fail = [prov_stub[0], dict(prov_stub[1], ok=False)]
    rw2 = rewrite_article_panels(FIXTURE_COMIC_ARTICLE, panels, prov_fail)
    check("rewrite: failed panel left untouched", "second-panel.jpg" in rw2 and "data-src" in rw2)
    check("rewrite_tag: src inserted when absent",
          rewrite_panel_tag('<img data-src="https://x/y.png" alt="p">', "images/panel-001.png")
          == '<img src="images/panel-001.png" alt="p">')

    # ---- magic-byte validation ----
    check("magic: PNG accepted", valid_image_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 2000))
    check("magic: JPEG accepted", valid_image_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 2000))
    check("magic: GIF accepted", valid_image_bytes(b"GIF89a" + b"\x00" * 2000))
    check("magic: placeholder GIF (~50B) rejected",
          not valid_image_bytes(b"GIF89a" + b"\x00" * 44))
    check("magic: non-image garbage rejected", not valid_image_bytes(b"<html>oops " * 200))
    check("magic: empty rejected", not valid_image_bytes(b""))

    # ---- panel extension from URL path ----
    check("ext: png from url path", panel_ext_from_url("https://us-a.tapas.io/pc/6c/abc.png?x=1") == "png")
    check("ext: jpg from url path", panel_ext_from_url("https://us-a.tapas.io/pc/6c/abc.JPG?x=1") == "jpg")
    check("ext: query string ignored", panel_ext_from_url("https://us-a.tapas.io/pc/a/b.jpeg?a=.png") == "jpeg")
    check("ext: unknown -> png fallback", panel_ext_from_url("https://us-a.tapas.io/pc/a/noext") == "png")

    # ---- entity unescape for signed URLs ----
    check("unescape: &amp; entity", html_mod.unescape("~hmac=deadbeef&amp;version=v4") == "~hmac=deadbeef&version=v4")

    # ---- comic resume gating in should_skip ----
    import shutil
    class _NS: pass
    ns = _NS()
    ns.comments_only = False
    ns.limit_comments_pages = None
    ns.limit_reply_pages = None
    _sd = tempfile.mkdtemp(prefix="tapas-selftest-skip-")
    try:
        _ed = episode_dir(_sd, 1, 12345)
        os.makedirs(os.path.join(_ed, "images"), exist_ok=True)
        for fn in ("index.json", "comments.json", "article.html"):
            with open(os.path.join(_ed, fn), "w") as f:
                f.write("x")
        _png = b"\x89PNG" + b"\x00" * 2000
        _pf = os.path.join(_ed, "images", "panel-001.png")
        with open(_pf, "wb") as f:
            f.write(_png)
        _rec = {"num": 1, "episode_id": "12345", "status": "complete",
                "content_saved": True, "free": True, "images": 1,
                "images_complete": True, "comments": 0, "replies": 0,
                "comment_pages": 0, "reply_pages_max": 0}
        _m = {"episodes": {"12345": _rec}}
        atomic_write_json(os.path.join(_ed, "images.json"),
                          [{"panel": 1, "url": "https://x/y.png",
                            "file": "images/panel-001.png", "bytes": len(_png)}])
        check("resume: intact comic episode skipped",
              should_skip(_sd, _m, 1, "12345", ns) is True)
        _rec["images_complete"] = False
        check("resume: images_complete False -> redo",
              should_skip(_sd, _m, 1, "12345", ns) is False)
        _rec["images_complete"] = True
        os.unlink(_pf)
        check("resume: missing panel file -> redo",
              should_skip(_sd, _m, 1, "12345", ns) is False)
        with open(_pf, "wb") as f:
            f.write(b"\x89PNG")          # truncated: size no longer matches
        check("resume: truncated panel -> redo",
              should_skip(_sd, _m, 1, "12345", ns) is False)
        os.unlink(os.path.join(_ed, "images.json"))
        check("resume: images.json missing -> redo",
              should_skip(_sd, _m, 1, "12345", ns) is False)
        atomic_write_json(os.path.join(_ed, "images.json"),
                          [{"panel": 1, "url": "https://x/y.png",
                            "file": "images/panel-001.png", "bytes": len(_png)}])
        _rec["images"] = 0
        check("resume: images=0 (novel) ignores panel disk checks",
              should_skip(_sd, _m, 1, "12345", ns) is True)
    finally:
        shutil.rmtree(_sd, ignore_errors=True)

    failed = [n for n, ok in checks if not ok]
    log("")
    log("SELFTEST: %d/%d passed%s" % (len(checks) - len(failed), len(checks),
                                      "" if not failed else "  FAILED: %s" % failed))
    return 0 if not failed else 1


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    p = argparse.ArgumentParser(description="Archive a Tapas.io series: episodes, comments, replies.")
    p.add_argument("--series", help="series id, e.g. 123456")
    p.add_argument("--out", default=None, help="output directory (default: tapas-archive-<series>)")
    p.add_argument("--cookie", default=None, help="optional Cookie header string for signed-in access")
    p.add_argument("--episode-range", default=None,
                   help="episode numbers to process, e.g. 1-50 or 7 (1-based, OLDEST order)")
    p.add_argument("--limit-episodes", type=int, default=None,
                   help="process at most N episodes (from the start of the range)")
    p.add_argument("--comments-only", action="store_true",
                   help="skip episode article/content fetch, archive comments only")
    p.add_argument("--limit-comments-pages", type=int, default=None,
                   help="cap comment pages per episode (testing; marks run partial)")
    p.add_argument("--limit-reply-pages", type=int, default=None,
                   help="cap reply pages per comment thread (testing)")
    p.add_argument("--rate", type=float, default=2.0, help="max requests/sec (default 2.0)")
    p.add_argument("--max-tries", type=int, default=4, help="max attempts per request (default 4)")
    p.add_argument("--timeout", type=int, default=30, help="per-request timeout seconds (default 30)")
    p.add_argument("--refresh-list", action="store_true", help="re-walk the episode list even if cached")
    p.add_argument("--force", action="store_true", help="re-fetch completed episodes too")
    p.add_argument("--selftest", action="store_true", help="run parser unit tests and exit")
    args = p.parse_args(argv)

    if args.selftest:
        return selftest()
    if not args.series:
        p.error("--series is required (e.g. --series 123456)")
    if args.out is None:
        args.out = "tapas-archive-%s" % args.series
    return run(args)


if __name__ == "__main__":
    sys.exit(main())

#           *     ,MMM8&&&.            *
#                MMMM88&&&&&    .
#               MMMM88&&&&&&&
#   *           MMM88&&&&&&&&
#               MMM88&&&&&&&&
#               'MMM88&&&&&&'
#                 'MMM8&&&'      *
#        |\___/|
#        )     (             .              '
#       =\     /=
#         )===(       *
#        /     \
#        |     |
#       /       \
#       \       /
#_/\_/\_/\__  _/_/\_/\_/\_/\_/\_/\_/\_/\_/\_
#|  |  |  |( (  |  |  |  |  |  |  |  |  |  |
#|  |  |  | ) ) |  |  |  |  |  |  |  |  |  |
#|  |  |  |(_(  |  |  |  |  |  |  |  |  |  |
#|  |  |  |  |  |  |  |  |  |  |  |  |  |  |
#jgs|  |  |  |  |  |  |  |  |  |  |  |  |  |