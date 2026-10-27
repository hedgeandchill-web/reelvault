"""ReelVault: web UI and public Instagram video downloader API.

Run locally with: uvicorn app:app --reload
Install dependencies first: pip install -r requirements.txt
"""
from __future__ import annotations

import asyncio
import collections
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

BASE = Path(__file__).resolve().parent
STATIC = BASE / 'static'
MAX_VIDEO_BYTES = 250 * 1024 * 1024
FILE_TTL_SECONDS = 15 * 60
TOKEN_TTL_SECONDS = 15 * 60
INSTAGRAM_HOSTS = {'instagram.com', 'www.instagram.com', 'm.instagram.com'}
IG_PATH = re.compile(r'^/(?:reel|reels|p|tv)/([A-Za-z0-9_-]{5,40})/?$')
SECRET = os.getenv('APP_SECRET') or secrets.token_urlsafe(48)
signer = URLSafeTimedSerializer(SECRET, salt='reelvault-media')

app = FastAPI(title='ReelVault', docs_url=None, redoc_url=None, openapi_url=None)


@dataclass
class ReadyFile:
    path: Path
    folder: Path
    created: float
    filename: str


ready_files: dict[str, ReadyFile] = {}
ready_files_lock = Lock()
request_buckets: dict[tuple[str, str], collections.deque[float]] = {}
rate_lock = Lock()
inspect_slots = asyncio.Semaphore(4)
download_slots = asyncio.Semaphore(2)


class URLRequest(BaseModel):
    url: str = Field(min_length=15, max_length=700)


class PrepareRequest(BaseModel):
    token: str = Field(min_length=10, max_length=2000)


def normalize_instagram_url(raw: str) -> str:
    """Reject anything except direct URLs to Instagram Reels/video posts.

    Narrow allowlisting also makes untrusted URL fetches much safer.
    """
    raw = raw.strip()
    if len(raw) > 700 or any(c in raw for c in ('\x00', '\r', '\n', '\t', '\\')):
        raise ValueError('Paste a valid Instagram Reel or video post link.')
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError('Paste a valid Instagram link.') from exc
    if parsed.scheme != 'https' or host not in INSTAGRAM_HOSTS or port not in (None, 443):
        raise ValueError('Use a full https://www.instagram.com link.')
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError('Paste a direct Instagram post or Reel link.')
    match = IG_PATH.fullmatch(parsed.path)
    if not match:
        raise ValueError('Supported links look like instagram.com/reel/… or instagram.com/p/…')
    content_type = parsed.path.strip('/').split('/')[0]
    if content_type == 'reels':
        content_type = 'reel'
    return urlunsplit(('https', 'www.instagram.com', f'/{content_type}/{match.group(1)}/', '', ''))


def clean_expired_files() -> None:
    now = time.monotonic()
    with ready_files_lock:
        stale = [k for k, v in ready_files.items() if now - v.created > FILE_TTL_SECONDS]
        dirs = [ready_files.pop(k).folder for k in stale]
    for folder in dirs:
        shutil.rmtree(folder, ignore_errors=True)


def check_rate_limit(request: Request, action: str, max_calls: int) -> None:
    """Simple per-process sliding-window limiter; use an edge limiter in production."""
    client = request.client.host if request.client else 'anonymous'
    now = time.monotonic()
    key = (client, action)
    with rate_lock:
        q = request_buckets.setdefault(key, collections.deque())
        while q and now - q[0] > 600:
            q.popleft()
        if len(q) >= max_calls:
            raise HTTPException(status_code=429, detail='Too many requests. Please try again later.')
        q.append(now)
        # Prevent unbounded memory growth from old IP addresses.
        if len(request_buckets) > 5000:
            for k in list(request_buckets)[:1000]:
                if not request_buckets[k] or now - request_buckets[k][-1] > 600:
                    request_buckets.pop(k, None)


def ydl_options(**kwargs) -> dict:
    return {
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'playlist_items': '1',
        'socket_timeout': 18,
        'retries': 1,
        'fragment_retries': 1,
        'extractor_retries': 1,
        'geo_bypass': False,
        'nocheckcertificate': False,
        'cachedir': False,
        **kwargs,
    }


def select_video_info(info: dict) -> dict:
    if info.get('_type') in ('playlist', 'multi_video'):
        entries = info.get('entries') or []
        for entry in entries:
            if entry and video_available(entry):
                return entry
        raise ValueError('This post does not appear to contain a downloadable video.')
    if not video_available(info):
        raise ValueError('This link does not appear to contain a video. Try a Reel or video post.')
    return info


def video_available(info: dict) -> bool:
    if info.get('vcodec') not in (None, 'none'):
        return True
    formats = info.get('formats') or []
    if any(f.get('vcodec') not in (None, 'none') for f in formats):
        return True
    # Some extractors expose format URLs without codec metadata.
    if info.get('vcodec') is None and info.get('ext') in {'mp4', 'mov', 'm4v', 'webm'}:
        return True
    return False


def translate_error(exc: Exception) -> str:
    msg = str(exc).lower()
    if any(word in msg for word in ('login required', 'private', 'not available', 'unavailable', '404', 'not found', 'restricted')):
        return 'This video is unavailable. It may be private, deleted, or restricted.'
    if any(word in msg for word in ('rate-limit', 'rate limit', '429', 'too many requests')):
        return 'Instagram is temporarily limiting requests. Try again later.'
    if 'ffmpeg' in msg:
        return 'The server needs FFmpeg to prepare this video.'
    if 'too large' in msg or 'maximum' in msg:
        return 'This video exceeds the 250 MB download limit.'
    if isinstance(exc, ValueError):
        return str(exc)
    return 'We could not retrieve this video right now. Confirm it is public and try again.'


def _inspect(url: str) -> dict:
    # Lazy import lets the UI load even if the download dependency is not yet installed.
    import yt_dlp

    with yt_dlp.YoutubeDL(ydl_options(skip_download=True)) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise ValueError('No video was found at that link.')
    info = select_video_info(info)
    title = str(info.get('title') or 'Instagram video').replace('\n', ' ').strip()
    if title.lower().startswith('instagram -'):
        title = title[11:].strip() or 'Instagram video'
    duration = info.get('duration')
    duration = int(duration) if isinstance(duration, (float, int)) and 0 < duration < 60_000 else None
    thumb = str(info.get('thumbnail') or '')
    if not thumb.startswith('https://'):
        thumb = ''
    return {'title': title[:130], 'thumbnail': thumb, 'duration': duration,
            'uploader': str(info.get('uploader') or '')[:65]}


# Prefer ordinary MP4/H.264 video with sound so the file plays on phones.
# Every fallback explicitly contains a video stream (no audio-only fallback).
VIDEO_FORMAT = (
    'b[ext=mp4][vcodec^=avc1]/'
    'bv[vcodec^=avc1]+ba[ext=m4a]/'
    'bv*[vcodec^=avc1]+ba/'
    'b[ext=mp4]/'
    'bv[ext=mp4]+ba[ext=m4a]/'
    'bv*+ba/bv*'
)


def probe_media(path: Path) -> tuple[str, str | None]:
    """Identify actual video/audio tracks; extension alone isn't evidence of video."""
    result = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(path)],
        capture_output=True, text=True, timeout=25, check=False,
    )
    if result.returncode != 0:
        raise ValueError('The downloaded media could not be read as a video.')
    try:
        streams = json.loads(result.stdout)['streams']
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError('The downloaded media has no readable video tracks.') from exc
    video_tracks = [
        st for st in streams
        if st.get('codec_type') == 'video'
        and not st.get('disposition', {}).get('attached_pic')
        and st.get('width', 0) > 0 and st.get('height', 0) > 0
    ]
    if not video_tracks:
        raise ValueError('Instagram returned audio without video. Try another public Reel.')
    audio_tracks = [st for st in streams if st.get('codec_type') == 'audio']
    return video_tracks[0].get('codec_name', ''), (audio_tracks[0].get('codec_name', '') if audio_tracks else None)


def ensure_playable_mp4(path: Path) -> Path:
    """Ensure H.264 video and AAC audio, transcode only when necessary."""
    vcodec, acodec = probe_media(path)
    if path.suffix.lower() == '.mp4' and vcodec == 'h264' and acodec in (None, 'aac'):
        return path
    target = path.parent / 'reelvault-playable.mp4'
    args = [
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
        '-i', str(path), '-map', '0:v:0', '-map', '0:a:0?',
        '-c:v', 'copy' if vcodec == 'h264' else 'libx264',
    ]
    if vcodec != 'h264':
        args += ['-preset', 'ultrafast', '-crf', '24', '-pix_fmt', 'yuv420p', '-threads', '1']
    args += [
        '-c:a', 'copy' if acodec in (None, 'aac') else 'aac',
        '-movflags', '+faststart', str(target),
    ]
    result = subprocess.run(args, capture_output=True, text=True, timeout=130, check=False)
    if result.returncode != 0 or not target.is_file():
        raise ValueError('Could not convert this video to a playable MP4.')
    probe_media(target)
    if target.stat().st_size > MAX_VIDEO_BYTES:
        raise ValueError('This video is too large (250 MB maximum).')
    # Save disk space on small free hosting instances.
    path.unlink(missing_ok=True)
    return target


def _download(url: str, folder: Path) -> Path:
    import yt_dlp

    opts = ydl_options(
        outtmpl=str(folder / 'rv-%(id)s.%(ext)s'),
        format=VIDEO_FORMAT,
        merge_output_format='mp4',
        max_filesize=MAX_VIDEO_BYTES,
        skip_download=False,
        restrictfilenames=True,
        # Reject photo-only posts; downloaded content is independently checked with ffprobe.
        match_filter=lambda info, incomplete: (
            None if incomplete or video_available(info)
            else 'No video in this post'
        ),
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        status = ydl.download([url])
    if status != 0:
        raise ValueError('Instagram could not provide this video.')
    files = [
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in {'.mp4', '.webm', '.mov', '.mkv'}
    ]
    if not files:
        raise ValueError('No downloadable video was found at that link.')
    # Never pick an audio-only MP4 just because it is the largest file.
    video_files = []
    for candidate in files:
        try:
            probe_media(candidate)
        except ValueError:
            continue
        video_files.append(candidate)
    if not video_files:
        raise ValueError('Instagram returned audio without video. Try another public Reel.')
    path = max(video_files, key=lambda p: p.stat().st_size)
    if path.stat().st_size > MAX_VIDEO_BYTES:
        raise ValueError('This video is too large (250 MB maximum).')
    return ensure_playable_mp4(path)


@app.middleware('http')
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; base-uri 'self'; form-action 'self'; "
        "frame-ancestors 'none'; img-src 'self' https: data:; "
        "font-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'"
    )
    return response


@app.get('/api/health')
async def health():
    return {'status': 'ok'}


@app.post('/api/inspect')
async def inspect_video(payload: URLRequest, request: Request):
    check_rate_limit(request, 'inspect', 20)
    try:
        url = normalize_instagram_url(payload.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        async with inspect_slots:
            data = await asyncio.wait_for(asyncio.to_thread(_inspect, url), timeout=70)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=translate_error(exc)) from exc
    data['token'] = signer.dumps({'url': url})
    data['type'] = 'Reel' if '/reel/' in url else 'Video post'
    return data


@app.post('/api/prepare')
async def prepare_video(payload: PrepareRequest, request: Request):
    check_rate_limit(request, 'prepare', 8)
    clean_expired_files()
    try:
        signed = signer.loads(payload.token, max_age=TOKEN_TTL_SECONDS)
        url = normalize_instagram_url(signed['url'])
    except (BadSignature, SignatureExpired, KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail='Your download link expired. Paste the Instagram link again.') from exc
    folder = Path(tempfile.mkdtemp(prefix='reelvault-'))
    try:
        async with download_slots:
            path = await asyncio.wait_for(asyncio.to_thread(_download, url, folder), timeout=180)
    except Exception as exc:
        shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(status_code=422, detail=translate_error(exc)) from exc
    file_id = uuid4().hex
    filename = 'instagram-video-' + file_id[:8] + path.suffix.lower()
    with ready_files_lock:
        ready_files[file_id] = ReadyFile(path=path, folder=folder, created=time.monotonic(), filename=filename)
    return {'download_url': f'/api/file/{file_id}', 'filename': filename}


@app.get('/api/file/{file_id}')
async def get_download(file_id: str):
    clean_expired_files()
    if not re.fullmatch(r'[0-9a-f]{32}', file_id):
        raise HTTPException(status_code=404, detail='File not found.')
    with ready_files_lock:
        file = ready_files.pop(file_id, None)
    if not file or not file.path.exists():
        raise HTTPException(status_code=404, detail='The download expired. Please try again.')
    return FileResponse(
        file.path,
        filename=file.filename,
        media_type='video/mp4' if file.path.suffix.lower() == '.mp4' else 'application/octet-stream',
        background=BackgroundTask(shutil.rmtree, file.folder, ignore_errors=True),
        headers={'Cache-Control': 'no-store'},
    )


@app.exception_handler(HTTPException)
async def custom_http_error(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={'detail': exc.detail})


app.mount('/', StaticFiles(directory=STATIC, html=True), name='site')
