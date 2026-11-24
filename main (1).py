import base64
import html as htmllib
import http.cookiejar
import mimetypes
import os
import re
import shutil
import tempfile
import time
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import yt_dlp
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

try:
    from yt_dlp.version import __version__ as YTDLP_VERSION
except Exception:
    YTDLP_VERSION = "unknown"

API_KEY = os.getenv("API_KEY", "change-me")
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "https://yourdomain.com").split(",")]
COOKIES_FILE = os.getenv("COOKIES_FILE")  # optional: Netscape cookies.txt (Instagram/Facebook/YouTube)

# Supported platforms (subdomains are allowed automatically; any pinterest.* domain is allowed too)
ALLOWED_DOMAINS = {
    "youtube.com", "youtu.be",
    "instagram.com",
    "facebook.com", "fb.watch", "fb.com",
    "tiktok.com",
    "twitter.com", "x.com",
    "reddit.com", "v.redd.it",
    "pinterest.com", "pin.it",
    "vimeo.com", "dailymotion.com", "twitch.tv",
    "soundcloud.com", "snapchat.com", "threads.net",
    "linkedin.com", "streamable.com", "likee.video",
}
# Real Pinterest country domains only (a loose pattern would let pinterest.com.evil.com through)
PINTEREST_RE = re.compile(
    r"(^|\.)pinterest\.(com|fr|de|ch|jp|cl|ca|it|co\.uk|nz|ru|com\.au|at|pt|co\.kr|es|com\.mx|dk|ph|th|nl|kr|ie|vn|in|mx|pe|hu|co\.in|co\.nz|id|tw|be|uk)$"
)
SHORT_HOSTS = {"pin.it"}  # short links that must be expanded before yt-dlp can read them

MAX_DURATION = 30 * 60            # seconds
MAX_FILESIZE = 150 * 1024 * 1024  # bytes (Render free = 512MB RAM)
MAX_ITEMS = 10                    # max videos taken from one link (carousel / playlist)
RATE_LIMIT = 10                   # requests per minute per IP
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

app = FastAPI(title="Universal Video Downloader API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET"],
    allow_headers=["x-api-key"],
)

BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "allowed_extractors": ["default", "-generic"],  # blocks arbitrary-URL fetching
    "socket_timeout": 20,
    "retries": 3,
    "concurrent_fragment_downloads": 4,
}
if COOKIES_FILE and os.path.isfile(COOKIES_FILE):
    BASE_OPTS["cookiefile"] = COOKIES_FILE

INFO_OPTS = {**BASE_OPTS, "extract_flat": "in_playlist", "playlistend": MAX_ITEMS}

PLATFORM_NAMES = {"Youtube": "YouTube", "Tiktok": "TikTok", "Twitter": "X"}

_hits: dict[str, list[float]] = defaultdict(list)
_avatar_cache: dict[str, str] = {}


# ---------- helpers ----------
def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else request.client.host


def check_auth(key: str):
    if key != API_KEY:
        raise HTTPException(401, "Invalid API key")


def rate_limit(ip: str):
    now = time.time()
    _hits[ip] = [t for t in _hits[ip] if now - t < 60]
    if len(_hits[ip]) >= RATE_LIMIT:
        raise HTTPException(429, "Too many requests, slow down")
    _hits[ip].append(now)


def host_allowed(host: str) -> bool:
    host = (host or "").lower()
    if PINTEREST_RE.search(host):
        return True
    return any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS)


def validate_url(url: str):
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not host_allowed(p.hostname):
        raise HTTPException(400, "This platform is not supported")


def needs_resolve(p) -> bool:
    """Share / short links that redirect to the real video URL."""
    host = (p.hostname or "").lower()
    path = p.path or ""
    if host in SHORT_HOSTS:
        return True
    if (host == "fb.com" or host.endswith("facebook.com")) and path.startswith("/share/"):
        return True
    if host.endswith("reddit.com") and re.search(r"/s/[A-Za-z0-9]+/?$", path):
        return True
    return False


def unwrap_login(u: str) -> str:
    """Facebook/Instagram sometimes redirect to a login page with ?next=<real url>; take the real url."""
    p = urlparse(u)
    if re.match(r"^/(accounts/)?login", p.path):
        nxt = parse_qs(p.query).get("next")
        if nxt:
            return urljoin(u, nxt[0])
    return u


def follow_redirect(url: str):
    """Follow redirects (with cookies if available) and return the final allowed URL, else None."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        with _opener().open(req, timeout=10) as r:
            final = unwrap_login(r.geturl())
    except Exception:
        return None
    try:
        validate_url(final)  # the redirect target must also be an allowed platform
    except HTTPException:
        return None
    return final


def prepare_url(url: str) -> str:
    """Validate the link and expand known share/short links (pin.it, facebook /share/, reddit /s/)."""
    validate_url(url)
    if not needs_resolve(urlparse(url)):
        return url
    return follow_redirect(url) or url


def is_unsupported(e) -> bool:
    low = str(e).lower()
    return "unsupported url" in low or "no suitable extractor" in low


def extract(url: str, opts: dict):
    """extract_info; if the link was an unknown share/redirect link, retry once on its real URL."""
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False), url
    except Exception as e:
        if is_unsupported(e):
            final = follow_redirect(url)
            if final and final != url:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    return ydl.extract_info(final, download=False), final
        raise


def run_download(url: str, opts: dict):
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as e:
        if is_unsupported(e):
            final = follow_redirect(url)
            if final and final != url:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([final])
                return
        raise


def friendly(e) -> str:
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "").strip()
    low = msg.lower()
    if "403" in low or "forbidden" in low or "blocked" in low:
        return "This site blocks the server's IP (common on free hosting). Add cookies.txt or run the API on your VPS. (" + msg[:120] + ")"
    if "no video could be found" in low or "no video formats" in low:
        return "No video found in this post (it may contain only text or images)."
    if "unsupported url" in low or "no suitable extractor" in low:
        return "No downloadable video found at this link. (" + msg[:140] + ")"
    if any(w in low for w in ("login", "log in", "sign in", "cookies", "rate-limit", "private")):
        return "This platform wants a login. Add cookies.txt or try a public link. (" + msg[:160] + ")"
    return msg[:300]


def thumb_of(e: dict):
    if e.get("thumbnail"):
        return e["thumbnail"]
    for t in reversed(e.get("thumbnails") or []):
        if t.get("url"):
            return t["url"]
    return None


def heights_of(e: dict) -> set:
    return {f["height"] for f in (e.get("formats") or []) if f.get("height")}


# ---------- creator avatar (best effort) ----------
IMG_HOSTS = (
    ".cdninstagram.com", ".fbcdn.net", ".googleusercontent.com", ".ggpht.com",
    ".tiktokcdn.com", ".tiktokcdn-us.com", ".pinimg.com", ".twimg.com",
    ".redditmedia.com", ".redd.it", ".fbsbx.com",
)


def img_host_ok(u: str) -> bool:
    p = urlparse(u)
    h = (p.hostname or "").lower()
    return p.scheme == "https" and any(h.endswith(s) for s in IMG_HOSTS)


def profile_page(data: dict, first: dict):
    for src in (data, first):
        u = src.get("uploader_url") or src.get("channel_url")
        if u and host_allowed(urlparse(u).hostname):
            return u
    key = (data.get("extractor_key") or "").lower()
    if key.startswith("instagram"):
        name = data.get("uploader_id") or first.get("uploader_id") or data.get("channel") or first.get("channel")
        if name and re.fullmatch(r"[A-Za-z0-9._]{1,40}", str(name)):
            return f"https://www.instagram.com/{name}/"
    return None


def _opener():
    jar = http.cookiejar.MozillaCookieJar()
    if COOKIES_FILE and os.path.isfile(COOKIES_FILE):
        try:
            jar.load(COOKIES_FILE, ignore_discard=True, ignore_expires=True)
        except Exception:
            pass
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def fetch_avatar(page_url):
    """Read og:image from the creator's profile page and return it as a small data URI."""
    if not page_url or not host_allowed(urlparse(page_url).hostname):
        return None
    if page_url in _avatar_cache:
        return _avatar_cache[page_url]
    try:
        req = urllib.request.Request(page_url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        with _opener().open(req, timeout=6) as r:
            page = r.read(400_000).decode("utf-8", "ignore")
        m = (re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', page)
             or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', page))
        if not m:
            return None
        img = htmllib.unescape(m.group(1))
        img = re.sub(r"=s\d+", "=s160", img)  # smaller google avatar
        if not img_host_ok(img):
            return None
        with urllib.request.urlopen(urllib.request.Request(img, headers={"User-Agent": UA}), timeout=6) as ir:
            raw = ir.read(250_001)
            ctype = ir.headers.get_content_type()
        if len(raw) > 250_000 or not ctype.startswith("image/"):
            return None
        uri = f"data:{ctype};base64,{base64.b64encode(raw).decode()}"
        if len(_avatar_cache) > 200:
            _avatar_cache.clear()
        _avatar_cache[page_url] = uri
        return uri
    except Exception:
        return None


# ---------- routes ----------
@app.get("/", include_in_schema=False)
def home():
    return FileResponse(Path(__file__).parent / "index.html", media_type="text/html")


@app.get("/health")
def health():
    return {"ok": True, "yt_dlp": YTDLP_VERSION}


@app.get("/api/platforms")
def platforms():
    return {"supported": sorted(ALLOWED_DOMAINS) + ["pinterest.*"]}


@app.get("/api/info")
def video_info(request: Request, url: str, x_api_key: str = Header(...)):
    check_auth(x_api_key)
    rate_limit(client_ip(request))
    url = prepare_url(url)

    try:
        data, url = extract(url, INFO_OPTS)
    except Exception as e:
        raise HTTPException(400, friendly(e))
    if not data:
        raise HTTPException(400, "Nothing found at this link")

    entries = None
    if data.get("_type") == "playlist" or data.get("entries") is not None:
        entries = [e for e in (data.get("entries") or []) if e][:MAX_ITEMS]
        if not entries:
            raise HTTPException(400, "No videos found in this link")

    first = entries[0] if entries else data

    if not entries and (data.get("duration") or 0) > MAX_DURATION:
        raise HTTPException(400, "Video is too long")

    def pick(key):
        v = data.get(key)
        return v if v is not None else first.get(key)

    if entries:
        heights = set().union(*(heights_of(e) for e in entries))
    else:
        heights = heights_of(data)

    key = data.get("extractor_key") or ""
    key = re.sub(r"(Post|Playlist|Tab|Story|Reel|Short|Clip|User)$", "", key) or key

    return {
        "title": pick("title"),
        "uploader": pick("uploader") or pick("channel"),
        "platform": PLATFORM_NAMES.get(key, key),
        "duration": None if entries else data.get("duration"),
        "thumbnail": thumb_of(first),
        "view_count": pick("view_count"),
        "like_count": pick("like_count"),
        "follower_count": pick("channel_follower_count"),
        "upload_date": pick("upload_date"),
        "uploader_avatar": fetch_avatar(profile_page(data, first)),
        "available_qualities": sorted(heights, reverse=True),
        "resolved_url": url,
        "is_playlist": bool(entries),
        "count": len(entries) if entries else 1,
        "items": [
            {"index": i + 1, "title": e.get("title"), "thumbnail": thumb_of(e), "duration": e.get("duration")}
            for i, e in enumerate(entries)
        ] if entries else [],
    }


@app.get("/api/download")
def download_video(
    request: Request,
    url: str,
    quality: int = Query(720, ge=144, le=2160),
    type: str = Query("video", pattern="^(video|audio)$"),
    item: str = Query(None, pattern=r"^(\d{1,2}|all)$"),
    x_api_key: str = Header(...),
):
    check_auth(x_api_key)
    rate_limit(client_ip(request))
    url = prepare_url(url)

    multi = item == "all"
    tmpdir = tempfile.mkdtemp(prefix="vdl_")
    opts = {
        **BASE_OPTS,
        "outtmpl": os.path.join(
            tmpdir, "%(playlist_index)s-%(title).70B.%(ext)s" if multi else "%(title).80B.%(ext)s"
        ),
        "max_filesize": MAX_FILESIZE,
        "restrictfilenames": True,
        "match_filter": yt_dlp.utils.match_filter_func(f"duration <=? {MAX_DURATION}"),
    }
    if multi:
        opts["playlist_items"] = f"1-{MAX_ITEMS}"
    elif item:
        opts["playlist_items"] = item

    if type == "audio":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ]
    else:
        opts["format"] = (
            f"bv*[height<={quality}][ext=mp4]+ba[ext=m4a]"
            f"/b[height<={quality}][ext=mp4]"
            f"/bv*[height<={quality}]+ba/b[height<={quality}]/bv*+ba/b"
        )
        opts["merge_output_format"] = "mp4"

    try:
        run_download(url, opts)
    except Exception as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise HTTPException(400, friendly(e))

    files = sorted(f for f in os.listdir(tmpdir) if not f.endswith((".part", ".ytdl", ".temp")))
    if not files:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise HTTPException(400, "Nothing was downloaded (video too long, private, or not available)")

    if multi and len(files) > 1:
        path = os.path.join(tmpdir, "videos.zip")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
            for f in files:
                z.write(os.path.join(tmpdir, f), f)
        name, media_type = "videos.zip", "application/zip"
    else:
        name = files[0]
        path = os.path.join(tmpdir, name)
        media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"

    return FileResponse(
        path,
        media_type=media_type,
        filename=name,
        background=BackgroundTask(shutil.rmtree, tmpdir, ignore_errors=True),
    )
