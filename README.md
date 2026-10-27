# ReelVault — Instagram video downloader website

A responsive, single-purpose web app for **public Instagram Reels and video posts**. Inspired by common Instagram downloader workflows, with its own visual design. Includes a real server-side downloader powered by `yt-dlp` (not a fake frontend-only download button).

## Features

- Paste an Instagram video or Reel link and get a media preview
- One-click server-side MP4 download when Instagram exposes a usable stream
- Works on phones and desktop; accessible keyboard and error states
- No user accounts, Instagram password requests, cookies, or API keys
- URL allowlisting (Instagram host and Reel/post paths only), per-IP request limits, concurrency caps, 250 MB video limit, signed short-lived tokens, temporary file cleanup
- Docker deployment with `ffmpeg` included

## Deploy for free (GitHub + Render)

GitHub hosts the **source code**, while Render hosts the **Python application and downloader**. Do **not** use GitHub Pages for the whole project: GitHub Pages does not execute Python.

1. Download and unzip the project. Open the `reelvault-site` folder.
2. Go to [github.com/new](https://github.com/new), create a repository (for example, `reelvault`), then **Add file → Upload files**. Upload the **contents** of `reelvault-site` (including the `Dockerfile`, `render.yaml`, `app.py`, `requirements.txt`, and `static/` folder) into the repository root. Do **not** upload only the `.zip` or an extra enclosing folder. Never upload a `.env` file.
3. Sign in to [dashboard.render.com](https://dashboard.render.com) and connect your GitHub account. Choose **New → Blueprint**, select your repository, and apply the `render.yaml` blueprint. It specifies a **Free** Docker-based web service with a generated `APP_SECRET`, plus `/api/health` as the health check. **Verify the displayed plan is Free before confirming.**
4. Let Render build the Docker image (which installs `ffmpeg`). Once deployed, open the `https://<assigned-name>.onrender.com` URL it provides. Check `/api/health` first, then test with a public Instagram Reel that you own or have permission to download.
5. Future pushes to GitHub will automatically redeploy the service.

**Manual alternative:** On Render choose **New → Web Service**, connect the GitHub repo, set **Language: Docker**, choose **Free**, keep root directory as repository root, optionally set health check to `/api/health`, and set environment variable `APP_SECRET` to a long random secret. The `Dockerfile` includes the build and start commands; no custom commands are needed.

**Free tier reality:** Render's free instances sleep after about 15 minutes of no requests and can take roughly a minute to wake up; they have **512 MB RAM** and usage limits. Temporary downloaded files are discarded if the service restarts. The current download cap is **250 MB per file**, but resource limits can cause smaller uploads/downloads to fail. Heavy traffic or outbound video transfers can exhaust bandwidth or trigger suspension. Instagram may block downloads from Render's IPs even for public posts. This is suitable for personal testing, not a guaranteed high-volume service. Check Render's [current free-tier details](https://render.com/docs/free) before publishing.

## Start it locally

```bash
cd reelvault-site
python -m venv .venv
source .venv/bin/activate                 # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```

Open **http://localhost:8000**. The UI also loads without `yt-dlp`, but video requests require all dependencies.

Install `ffmpeg` locally (`brew install ffmpeg` on macOS; your package manager on Linux) for best video/audio merging.

## Docker deployment (recommended)

```bash
cp .env.example .env
# Change APP_SECRET in .env to a long random value

docker build -t reelvault .
docker run --rm --env-file .env -p 8000:8000 reelvault
```

Deploy this Dockerfile to a VPS, Render, Railway, Fly.io, or another platform that supports long requests, `ffmpeg`, and writable temporary storage. Point your domain to the host and enable HTTPS. Set `APP_SECRET` in deployment environment variables, never commit `.env`.

### Production checklist

1. Add a real domain and HTTPS through your host or proxy.
2. Configure a strong persistent `APP_SECRET`.
3. Put an edge reverse proxy and rate limiting in front of the service. The built-in limiter is per process, intended as a basic guard rather than full abuse prevention.
4. Set CPU, memory, disk, networking, bandwidth, and concurrent-job limits at the infrastructure level.
5. Monitor `yt-dlp` updates: public Instagram extraction may break when Instagram changes its site, rate limits requests, blocks datacenter IPs, or requires authentication. **No ability to download any particular link is guaranteed.** Do not attempt to bypass login or privacy restrictions.
6. Scale with care: the short-lived file registry lives in one process, so run **one worker** unless you replace it with shared storage and a shared registry. In production, add storage quotas and periodic filesystem cleanup if the process crashes while downloading.
7. Replace placeholder brand details, add actual support contact and your own legal/privacy pages before public launch.

## API

- `POST /api/inspect` with `{ "url": "https://www.instagram.com/reel/.../" }` returns title, thumbnail, duration, signed token.
- `POST /api/prepare` with `{ "token": "..." }` downloads the video into temporary storage and returns `{ "download_url": "/api/file/..." }`.
- `GET /api/file/{id}` serves the file as an attachment once and deletes the temporary folder when serving finishes.
- `GET /api/health` returns `{ "status": "ok" }`.

## Limitations and rights

Only **publicly accessible** direct Reel (`/reel/`) and video post (`/p/` or `/tv/`) URLs are supported. This site does not access private accounts, stories, or photo-only posts. Video quality is limited to what Instagram makes available. Instagram may block automated downloading entirely for some public videos. Download content only when you own it or have the relevant permission; follow Instagram's terms and creators' rights. This project is **not affiliated with Instagram or Meta**.

No request URLs or media are intentionally stored persistently by the app. Files are held in temporary storage during download; prepared files expire after 15 minutes and are removed when successfully served. A crash may leave a temporary folder for host-level cleanup. Your host/reverse proxy may separately record access logs.

## Tests

```bash
python -m unittest discover -s tests -v
```

These tests validate URL handling and media type checks. They do not test actual Instagram downloads because the external platform frequently changes and may block server IPs.
