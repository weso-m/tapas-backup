# tapas-backup

One-shot archival backup for a Tapas.io series — **novels AND comics**. Every
episode's article HTML (novel prose + inline base64 story images; comic panels
downloaded from their signed CDN URLs, validated and localized) plus **all
reader comments and every reply** in every comment thread.  **

Stdlib only. Python 3.8+ (developed on 3.11). No `pip install`, no third-party
deps — runs on bare `python`.

## Run

```bash
cd tapas-backup
python tapas_backup.py --series 123456 --out archive
```

Resume after an interruption by re-running the **same command**: completed
episodes are skipped (manifest-driven), only the remainder is fetched.

Full 862-episode archive: observed ~6-7 comment pages/episode on early eps →
roughly `862 info + 862 article + 44 list + ~6k comment pages + reply pages
≈ 8-10k requests ≈ 1.5-2 h` at the default 2 req/s.  

## CLI options

| Option | Default | Meaning |
|---|---|---|
| `--series` | (required) | series id, e.g. `123456` for *My Series* |
| `--out DIR` | `tapas-archive-<series>` | output directory (created if missing) |
| `--cookie STR` | none | raw `Cookie` header for signed-in fetches (not needed; series is free) |
| `--episode-range N` / `N-M` | all | 1-based episode numbers in OLDEST order |
| `--limit-episodes N` | all | process at most N episodes from the start of the range |
| `--comments-only` | off | skip article/content fetch, archive comments only |
| `--limit-comments-pages N` | all | cap comment pages per episode (testing; marks episode partial) |
| `--limit-reply-pages N` | all | cap reply pages per thread (testing) |
| `--rate R` | `2.0` | max requests/sec, global, jittered |
| `--max-tries N` | `4` | attempts per request; exponential backoff on 429/5xx/network/timeout errors |
| `--timeout S` | `30` | per-request timeout |
| `--refresh-list` | off | re-walk the episode list even if the manifest cached it |
| `--force` | off | re-fetch completed episodes too (otherwise resume skips them) |
| `--selftest` | — | run parser unit tests (fixtures captured from live tapas.io) and exit |

## Output layout

```
<out>/
  series.json                      series meta (JSON)
  manifest.json                    progress + per-episode counts + gaps + warnings (atomic writes)
  gaps.txt                         episodes locked (free=false) or failed after retries
  episodes/NNNN-<episode_id>/
    index.json                     episode info JSON
    article.html                   verbatim <article> element (prose + inline base64 images;
                                    comic episodes: panels localized to images/panel-NNN.ext)
    images/                         comic episodes only: downloaded panel images
      panel-001.png ...            one file per panel, in document order
    images.json                     comic episodes only: per-panel provenance (see below)
    comments.json                  array of comments, each with a nested replies array
```

`images.json` entry shape (comic episodes — original signed URL kept for
provenance; the token inside it is expired, the URL is a record, not a usable
link):

```json
[{"panel": 1,
  "url": "https://us-a.tapas.io/pc/6c/76199a28-....png?__token__=exp=...~acl=...~hmac=...&version=v4",
  "file": "images/panel-001.png",
  "bytes": 52818}]
```

`comments.json` entry shape (comment bodies are **raw HTML** — entities such as
`&rsquo;`, `&quot;`, `<br>` are preserved exactly as served; only the wrapping
`<div class="js-comment-body">` is stripped):

```json
{
  "id": 23291273, "username": "sample_reader_01", "display_name": "SampleReader One",
  "date": "Jul 29, 2026", "text": "Rereading in 2026 and good Lordy",
  "likes": 0, "is_creator": false,
  "replies": [{"id": 22283646, "username": "sample_reader_02", "display_name": "SampleReader Two",
               "date": "Oct 12, 2025", "text": "&quot;She would be...&quot;", 
               "likes": 1, "is_creator": false}]
}
```

## Comic support

Comic episodes (e.g. series 654321) embed each panel as a lazy-load
`<img class="content__img js-lazy" ...>` whose `src` is only a ~50-byte inline
base64 placeholder GIF; the **real image URL lives in the `data-src`
attribute** and points at a signed, expiring CDN URL
(`https://us-a.tapas.io/...?__token__=exp=...~acl=...~hmac=...`). The script:

1. finds every `data-src` panel in the article (document order), unescapes
   the HTML-escaped URL (`&amp;` → `&`, via `html.unescape`);
2. downloads each one through the same rate-limited/retried `Fetcher` into
   `episodes/NNNN-<eid>/images/panel-NNN.<ext>` (extension from the URL path);
3. **validates every download by magic bytes** — the file must start with
   PNG (`\x89PNG`), JPEG (`\xff\xd8\xff`) or GIF (`GIF8`) magic AND be larger
   than 1000 bytes. A ~50-byte GIF means the placeholder got saved instead of
   the panel — that's a failure, retried, then recorded in `gaps.txt`;
4. rewrites the saved `article.html` so each panel `src` points at its local
   `images/panel-NNN.ext` (all other attributes preserved, `data-src`
   removed);
5. writes `images.json` next to the article: one provenance entry per panel
   (panel number, original signed URL, local file, byte size).

**The 5-hour token expiry is why panels download in the same run as the
episode page.** The `exp=` epoch inside `__token__` gives the signed URLs a
lifetime of roughly 5 hours from the page fetch — a later run CANNOT re-use
them. Resume handles this correctly: an episode with missing/invalid panels is
re-fetched from its HTML page (fresh tokens), and only panels whose local
files are absent or fail validation are downloaded again.

Novel episodes are unaffected: their images are full base64 data URIs in
`src` with no `data-src` attribute (verified: 0 hits across all 862 archived
*My Series* articles), so they save verbatim with zero panel fetches.

Locked comic episodes (`free:false`) record a gap and skip panels, as with
novel content — pass `--cookie` with a signed-in session's Cookie header to
fetch those.

## Resumability

`manifest.json` is rewritten atomically (temp file + `os.replace`) after every
episode, so a kill mid-run never corrupts it. An episode counts as complete only
when info + article (unless locked/`--comments-only`) + all comment pages + all
reply pages + all panel images (comics: `images`/`images_complete` recorded,
`images.json` present) are on disk. Re-runs skip completed episodes and print
`skipped` lines. Partial fetches (page caps from `--limit-*`) are redone if a
later run allows more pages. Locked episodes are logged to `gaps.txt` and their
comments are still archived (comments stay public on locked episodes).

## Endpoint map (all verified anonymously 2026-09-24)

1. **Series meta** — `GET https://tapas.io/series/123456`
   With `Accept: application/json` + `X-Requested-With: XMLHttpRequest` → JSON
   `{code:200, data:{id, title, url, thumb_url, type:"COMMUNITY_BOOKS", book:true, genre, thumbsup_cnt, ...}}`.
   Without those headers it's an HTML page.

2. **Episode list** — `GET https://tapas.io/series/123456/episodes?eid=1701734&page=1&sort=OLDEST&last_access=0&max_limit=20`
   → JSON `{code, data:{pagination:{page, has_next, total:862, max_limit:20}, body:"<li ... data-href=\"/episode/<id>\" ...>"}}`.
   Walk pages via `pagination.has_next` (server also returns next `page` number).
   862 episodes = 44 pages (43×20 + 2). **`max_limit` must stay 20** — `max_limit=100`
   returns HTTP 500.

3. **Episode info** — `GET https://tapas.io/episode/<eid>/info`
   → JSON with `title, publish_date, free, early_access, must_pay, nsfw, view_cnt,
   like_cnt, comment_cnt, prev_ep_id, next_ep_id, open_comments`.
   `comment_cnt` **includes replies** — used as a sanity check, not a page count.

4. **Episode content** — `GET https://tapas.io/episode/<eid>`
   Server-rendered HTML. The chapter body is the single
   `<article class="... js-episode-article ...">` element (novel prose in `<p>`,
   inline story images as `data:image/...;base64` URIs — kept verbatim). Comment
   avatars elsewhere on the page (`<img class="circle" src="https://us-a.tapas.io/...">`)
   are outside the article and excluded.

   **Comics:** panel images inside the article are
   `<img class="content__img js-lazy" src="<~50-byte inline base64 placeholder
   GIF>" data-src="<signed URL>">`. The `data-src` attribute value is
   HTML-escaped (`&amp;` → `&` before fetching). The URL points at
   `https://us-a.tapas.io/pc/...` with a `?__token__=exp=<epoch>~acl=...~hmac=...`
   signature — served anonymously with HTTP 200, but the token **expires ~5
   hours after the episode page fetch**, so panels must be downloaded in the
   same run as the article fetch.

5. **Comments** — `GET https://tapas.io/comment/<eid>?page=N&sort=NEWEST&since=0&init_load=0&wr=true&ep=false`
   → JSON `{code, data:{pagination:{page, has_next}, html}}`, 10 top-level rows/page.
   Rows: `<div class="comment-row-wrap js-comment-parent-row" id="comment-row-<id>">`
   with `writer__name` / `writer__date` / `js-comment-body` / like `data-cnt` /
   reply count on `<a class="js-toggle-reply-btn" data-reply-cnt="N">`.

6. **Replies** — `GET https://tapas.io/comment/<eid>/<cid>/replies?page=N`
   → same JSON shape, up to 20 reply rows/page, **flat** (all replies to the
   top-level comment; no deeper nesting is exposed). Rows:
   `<div class="reply-wrapper js-comment-reply" id="comment-row-<id>">`;
   creator replies carry `<p class="writer__label">Creator</p>`.

### Endpoint notes (differences/quirks observed live)

- `pagination.page` in list/comment/reply responses reports the **next** page
  number, not the current one; the walker uses `has_next` and treats `page`
  as advisory with a strict monotonic guard.
- Episode list `eid` param (first episode id) is optional — page 1 works
  without it, and `eid=0` works too. The script passes `eid` on pages 2+ as a
  belt-and-braces measure.
- `max_limit=100` on the episode list → HTTP 500 (confirmed; keep `max_limit=20`).
- Replies have **no `sort`/`since`** params; `?page=N` is all they take.
  Walking works via `has_next` (verified on a 27-reply thread: 20 + 7 = 27).
- Comment/reply counts: `comment_cnt` from `/info` = **top-level comments +
  replies** (confirmed on ep 1: walk found 73 top-level + 53 replies = 126 =
  `comment_cnt` exactly). The script compares against this and warns on
  mismatch (deleted comments would show up here).
- Some `replies` responses report `total: 0` and `original_max_limit: 0`
  regardless of actual reply count — do not trust those fields; use `has_next`.
- A `Cookie` header (from a signed-in browser session) can be passed with
  `--cookie` if a series ever requires auth; not needed for free series.
- **Comic panel URLs** (endpoint 4): `data-src` attr on `js-lazy` imgs inside
  the article; value HTML-escaped; signed token expires ~5 h (see *Comic
  support* above). A real panel returns HTTP 200 with the actual image
  bytes (PNG/JPEG); the inline `src` placeholder is a ~50-byte base64 GIF
  (1×1 transparent pixel) — which is why every downloaded panel is
  magic-byte + size validated before being trusted.

## Rate limiting

All requests go through one global limiter: default **2 req/s with jitter**,
exponential backoff (max 4 tries per URL) on HTTP 429/5xx, timeouts and network
errors. The limit applies across all phases (series → list → episodes →
comments → replies). Retries are also counted and reported in the run summary.

## Run it soon

Tapas.io is shutting down. This script was verified against the live site on
2026-09-24 with a limited run (`--episode-range 1-3 --limit-comments-pages 4`)
plus resume testing; the full archive is one command:

```bash
python tapas_backup.py --series 123456 --out archive
```

Keep the `archive/` directory backed up (it contains everything: prose,
base64 story images, all comments and replies).