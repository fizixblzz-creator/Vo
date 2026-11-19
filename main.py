import os
import shutil
import tempfile
import time
from collections import defaultdict
from urllib.parse import urlparse

import yt_dlp
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

API_KEY = os.getenv("API_KEY", "change-me")
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "https://yourdomain.com").split(",")]
COOKIES_FILE = os.getenv("COOKIES_FILE")  # optional: Netscape cookies.txt (Instagram/Facebook/YouTube)

# Supported platforms (subdomains are allowed automatically)
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

MAX_DURATION = 30 * 60            # seconds
MAX_FILESIZE = 150 * 1024 * 1024  # bytes (Render free = 512MB RAM)
RATE_LIMIT = 10                   # requests per minute per IP

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
}
if COOKIES_FILE and os.path.isfile(COOKIES_FILE):
    BASE_OPTS["cookiefile"] = COOKIES_FILE

_hits: dict[str, list[float]] = defaultdict(list)


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


def validate_url(url: str):
    p = urlparse(url)
    host = (p.hostname or "").lower()
    ok = p.scheme in ("http", "https") and any(
        host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS
    )
    if not ok:
        raise HTTPException(400, "This platform is not supported")


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/api/platforms")
def platforms():
    return {"supported": sorted(ALLOWED_DOMAINS)}


@app.get("/api/info")
def video_info(request: Request, url: str, x_api_key: str = Header(...)):
    check_auth(x_api_key)
    rate_limit(client_ip(request))
    validate_url(url)

    try:
        with yt_dlp.YoutubeDL(BASE_OPTS) as ydl:
            data = ydl.extract_info(url, download=False)
    except Exception as e:
        raise HTTPException(400, f"Could not read link: {str(e)[:300]}")

    if (data.get("duration") or 0) > MAX_DURATION:
        raise HTTPException(400, "Video is too long")

    heights = sorted({f["height"] for f in data.get("formats", []) if f.get("height")}, reverse=True)
    return {
        "title": data.get("title"),
        "uploader": data.get("uploader"),
        "platform": data.get("extractor_key"),
        "duration": data.get("duration"),
        "thumbnail": data.get("thumbnail"),
        "available_qualities": heights,
    }


@app.get("/api/download")
def download_video(
    request: Request,
    url: str,
    quality: int = Query(720, ge=144, le=2160),
    type: str = Query("video", pattern="^(video|audio)$"),
    x_api_key: str = Header(...),
):
    check_auth(x_api_key)
    rate_limit(client_ip(request))
    validate_url(url)

    tmpdir = tempfile.mkdtemp(prefix="vdl_")
    opts = {
        **BASE_OPTS,
        "outtmpl": os.path.join(tmpdir, "%(title).80B.%(ext)s"),
        "max_filesize": MAX_FILESIZE,
        "restrictfilenames": True,
    }

    if type == "audio":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ]
        media_type = "audio/mpeg"
    else:
        opts["format"] = f"bv*[height<={quality}]+ba/b[height<={quality}]/bv*+ba/b"
        opts["merge_output_format"] = "mp4"
        media_type = "video/mp4"

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise HTTPException(400, f"Download failed: {str(e)[:300]}")

    files = [f for f in os.listdir(tmpdir) if not f.endswith((".part", ".ytdl", ".temp"))]
    if not files:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise HTTPException(500, "No file produced")

    path = os.path.join(tmpdir, files[0])
    return FileResponse(
        path,
        media_type=media_type,
        filename=files[0],
        background=BackgroundTask(shutil.rmtree, tmpdir, ignore_errors=True),
    )
