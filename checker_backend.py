"""Framewise — evidence-first AI-generated video checker.

A video-classifier score is available only when Sightengine credentials are configured.
Without a model provider, returns provenance and file facts and an INCONCLUSIVE verdict;
it deliberately never fabricates an AI likelihood.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from threading import Lock
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / 'static'
MAX_BYTES = min(100, max(5, int(os.getenv('ANALYSIS_MAX_VIDEO_MB', '30')))) * 1024 * 1024
MAX_SECONDS = 600
QUEUE_LIMIT = min(20, max(1, int(os.getenv('ANALYSIS_QUEUE_LIMIT', '4'))))
WORKERS = max(1, min(2, int(os.getenv('ANALYSIS_WORKERS', '1'))))
JOB_TTL = 20 * 60
API_USER = os.getenv('SIGHTENGINE_API_USER', '').strip()
API_SECRET = os.getenv('SIGHTENGINE_API_SECRET', '').strip()
MAX_MODEL_SCANS_PER_DAY = max(1, int(os.getenv('MAX_MODEL_SCANS_PER_DAY', '20')))
model_lock = Lock()
model_quota = {'date': '', 'used': 0}
VIDEO_SUFFIXES = {'.mp4','.mov','.m4v','.webm','.mkv'}
HOSTS = {'www.instagram.com','instagram.com','m.instagram.com','www.tiktok.com','tiktok.com',
         'vm.tiktok.com','vt.tiktok.com','www.facebook.com','facebook.com','m.facebook.com','fb.watch'}

router = APIRouter(prefix='/api/checker', tags=['AI Video Checker'])
queue: asyncio.Queue[str] = asyncio.Queue(maxsize=QUEUE_LIMIT)
jobs: dict[str, 'Job'] = {}
active_tasks: list[asyncio.Task] = []
limits: dict[str, list[float]] = {}

@dataclass
class Job:
    id: str
    source: str
    folder: Path
    filepath: Path | None = None
    url: str | None = None
    created: float = field(default_factory=time.monotonic)
    status: str = 'queued'
    phase: str = 'Waiting for available analysis capacity'
    result: dict | None = None
    error: str | None = None

class LinkRequest(BaseModel):
    url: str = Field(min_length=15, max_length=600)


def validate_url(raw: str) -> tuple[str,str]:
    raw = raw.strip()
    if any(x in raw for x in ('\\','\r','\n','\t','\x00')):
        raise ValueError('Please paste a valid link.')
    try:
        u = urlsplit(raw)
        port = u.port
    except ValueError as e:
        raise ValueError('Please paste a valid link.') from e
    if u.scheme != 'https' or u.hostname not in HOSTS or port not in (None,443) or u.username or u.password or u.fragment:
        raise ValueError('Use a direct HTTPS TikTok, Instagram, or Facebook video link.')
    path = u.path
    host = u.hostname
    if 'instagram.com' in host:
        if not re.fullmatch(r'/(?:p|reel|reels|tv)/[\w-]{5,40}/?',path):
            raise ValueError('Use a link to a specific Instagram post or Reel.')
        site='Instagram'
    elif 'tiktok.com' in host:
        if host in {'vt.tiktok.com','vm.tiktok.com'}:
            if not re.fullmatch(r'/[\w-]{4,40}/?', path):
                raise ValueError('Use a valid TikTok share link.')
        elif not re.fullmatch(r'/@[^/]+/video/\d+/?',path):
            raise ValueError('Use a link to a specific TikTok video.')
        site='TikTok'
    else:
        if host == 'fb.watch':
            if not re.fullmatch(r'/[\w-]{4,40}/?',path):
                raise ValueError('Use a valid Facebook video link.')
        elif not (re.fullmatch(r'/(?:reel|watch|videos)/[\w.-]+/?',path) or
                  re.fullmatch(r'/share/(?:r|v)/[\w-]+/?',path) or
                  re.fullmatch(r'/[^/]+/videos/\d+/?',path) or
                  (path == '/watch/' and u.query and 'v=' in u.query)):
            raise ValueError('Use a link to a specific Facebook video or Reel.')
        site='Facebook'
    return raw,site


def per_ip_limit(request: Request):
    key = request.client.host if request.client else 'unknown'
    now=time.monotonic()
    history = [t for t in limits.get(key,[]) if now-t<600]
    if len(history)>=6:
        raise HTTPException(429,'Limit reached: please try again in a few minutes.')
    history.append(now); limits[key]=history
    if len(limits)>5000: limits.clear()


def probe_video(path: Path) -> dict:
    cmd=['ffprobe','-v','error','-show_format','-show_streams','-of','json',str(path)]
    out=subprocess.run(cmd,capture_output=True,text=True,timeout=15,check=False)
    if out.returncode!=0:
        raise ValueError('The file could not be read as a video.')
    try: data=json.loads(out.stdout)
    except json.JSONDecodeError: raise ValueError('The file could not be read as a video.')
    streams=data.get('streams') or []
    v=next((s for s in streams if s.get('codec_type')=='video'),None)
    if not v: raise ValueError('This file has no video stream.')
    duration=float((data.get('format') or {}).get('duration') or v.get('duration') or 0)
    if duration<=0 or duration>MAX_SECONDS:
        raise ValueError('Videos must be between 1 second and 10 minutes.')
    tags={str(k).lower():str(v)[:120] for k,v in (data.get('format',{}).get('tags') or {}).items()}
    return {'duration':round(duration,1), 'width':int(v.get('width') or 0),
            'height':int(v.get('height') or 0), 'codec':v.get('codec_name') or 'unknown',
            'audio':any(s.get('codec_type')=='audio' for s in streams),
            'size_mb':round(path.stat().st_size/1048576,2),'tags':tags}


def metadata_hints(tags:dict) -> list[dict]:
    text=' '.join(f'{k} {v}' for k,v in tags.items()).lower()
    patterns={'sora':'Sora', 'runway':'Runway', 'kling':'Kling', 'pika':'Pika',
              'stable diffusion':'Stable Diffusion', 'luma dream':'Luma Dream Machine',
              'veo':'Veo', 'hunyuanvideo':'HunyuanVideo'}
    found=[]
    for pattern,brand in patterns.items():
        if re.search(r'(?<!\w)'+re.escape(pattern)+r'(?!\w)',text):
            found.append({'title':f'{brand} mentioned in file metadata','description':'A modifiable file tag references an AI creation tool. This is a clue, not authentication.'})
    return found[:4]


def provenance(path:Path) -> dict:
    tool=shutil.which('c2patool')
    if not tool:
        return {'status':'not_checked','description':'Content Credentials check unavailable on this server (c2patool not installed).'}
    try:
        process=subprocess.run([tool,str(path)],capture_output=True,text=True,timeout=25)
        if process.returncode!=0:
            return {'status':'unavailable','description':'No readable Content Credentials report was returned.'}
        report=json.loads(process.stdout)
        manifests=report.get('manifests') or {}
        if not manifests:
            return {'status':'none_found','description':'No Content Credentials manifest found. Social networks may strip them.'}
        active=report.get('active_manifest')
        manifest=manifests.get(active,{})
        validation=report.get('validation_status') or manifest.get('validation_status') or []
        return {'status':'present','description':'Content Credentials manifest was found. Inspect its claims and trust chain before relying on it.',
                'claim_generator':str(manifest.get('claim_generator') or '')[:120],
                'validation_warnings':len(validation) if isinstance(validation,list) else None}
    except Exception:
        return {'status':'unavailable','description':'Content Credentials could not be read for this file.'}


def prepare_sample(path:Path, dest:Path, duration:float) -> tuple[Path,str]:
    # Re-encode short samples to constrain API usage. First 45 sec for long footage.
    seconds=min(45,max(1,int(duration)))
    args=['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-y','-i',str(path),
          '-t',str(seconds),'-vf','scale=640:-2:force_original_aspect_ratio=decrease',
          '-an','-c:v','libx264','-preset','ultrafast','-crf','29','-pix_fmt','yuv420p',str(dest)]
    p=subprocess.run(args,capture_output=True,text=True,timeout=90)
    if p.returncode!=0 or not dest.exists() or dest.stat().st_size<200:
        raise ValueError('Unable to prepare a compatible video sample for AI analysis.')
    return dest, 'Full video' if duration<=45 else 'First 45 seconds only'


def sightengine_check(path:Path) -> list[dict]:
    # Best-effort daily spend guard; resets on server restart. Configure hard spend limits at provider.
    day=time.strftime('%Y-%m-%d', time.gmtime())
    with model_lock:
        if model_quota['date'] != day:
            model_quota['date']=day; model_quota['used']=0
        if model_quota['used'] >= MAX_MODEL_SCANS_PER_DAY:
            raise RuntimeError('Daily model analysis quota reached.')
        model_quota['used'] += 1
    with path.open('rb') as f:
        with httpx.Client(timeout=httpx.Timeout(120,connect=12)) as client:
            r=client.post('https://api.sightengine.com/1.0/video/check-sync.json',
                          data={'models':'genai','interval':'5','api_user':API_USER,'api_secret':API_SECRET},
                          files={'media':('sample.mp4',f,'video/mp4')})
    r.raise_for_status()
    body=r.json()
    if body.get('status')!='success':
        raise RuntimeError('The AI analysis provider could not complete this check.')
    rows=(body.get('data') or {}).get('frames') or []
    frames=[]
    for frame in rows:
        value=(frame.get('type') or {}).get('ai_generated')
        if isinstance(value,(int,float)) and 0<=value<=1:
            frames.append({'position':round(float((frame.get('info') or {}).get('position') or 0),1),
                           'score':round(float(value),3)})
    if not frames: raise RuntimeError('The provider returned no usable frame scores.')
    return frames


def summarize_scores(frames:list[dict]) -> dict:
    """Interpret the provider's frame scores conservatively; these are NOT probabilities of truth."""
    vals=sorted(x['score'] for x in frames)
    mean=sum(vals)/len(vals)
    high=sum(v>=.8 for v in vals)
    low=sum(v<=.2 for v in vals)
    if len(vals)>=2 and mean>=.8 and high/len(vals)>=.65:
        verdict='likely_ai'; title='Likely AI-generated'; descr='Multiple sampled frames show strong AI-generation signals.'
    elif len(vals)>=2 and mean<=.2 and low/len(vals)>=.65:
        verdict='likely_real'; title='No strong full-generation signals'; descr='The sampled frames show relatively low fully AI-generated signals. That does not prove the video is authentic.'
    else:
        verdict='uncertain'; title='Mixed or uncertain signals'; descr='The evidence does not support a confident classification.'
    return {'verdict':verdict,'title':title,'description':descr,'score':round(mean*100),
            'frames':frames,'frame_count':len(frames),'range':[round(min(vals)*100),round(max(vals)*100)]}


def analyze_media(job:Job) -> dict:
    assert job.filepath is not None
    info=probe_video(job.filepath)
    hints=metadata_hints(info['tags'])
    prov=provenance(job.filepath)
    facts={k:v for k,v in info.items() if k!='tags'}
    report={'id':job.id,'source':job.source,'facts':facts,'hints':hints,'provenance':prov,
            'provider':'Sightengine' if (API_USER and API_SECRET) else None,
            'sample':'Not analyzed by an AI model', 'model':None}
    if API_USER and API_SECRET:
        sample=job.folder/'sample.mp4'
        sample,coverage=prepare_sample(job.filepath,sample,info['duration'])
        report['sample']=coverage
        try:
            frames=sightengine_check(sample)
            report['model']=summarize_scores(frames)
        except Exception:
            report['model_error']='AI classifier unavailable or provider quota reached. No prediction has been fabricated.'
    if report['model'] is None:
        report['model']={'verdict':'inconclusive','title':'Inconclusive — no AI classification',
                         'description':'Metadata and provenance alone cannot determine if video pixels were AI-generated.',
                         'score':None,'frames':[],'frame_count':0}
    return report


def fetch_public_video(url:str,folder:Path) -> Path:
    try: import yt_dlp
    except ImportError: raise ValueError('Social link support is unavailable on this server.')
    opts={'outtmpl':str(folder/'source.%(ext)s'),'format':'best[ext=mp4]/best',
          'noplaylist':True,'playlist_items':'1','quiet':True,'no_warnings':True,
          'socket_timeout':12,'retries':1,'fragment_retries':1,'extractor_retries':1,
          'max_filesize':MAX_BYTES,'restrictfilenames':True,'cachedir':False,
          'nocheckcertificate':False,'geo_bypass':False}
    with yt_dlp.YoutubeDL(opts) as ydl: ydl.download([url])
    files=[p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES]
    if not files: raise ValueError('No public video could be retrieved. Download it yourself and upload the file instead.')
    file=max(files,key=lambda p:p.stat().st_size)
    if file.stat().st_size>MAX_BYTES: raise ValueError(f'The video exceeds the {MAX_BYTES//1048576} MB size limit.')
    return file


def cleanup_old():
    now=time.monotonic()
    for key in list(jobs):
        j=jobs[key]
        if now-j.created>JOB_TTL and j.status not in ('processing','fetching'):
            jobs.pop(key,None); shutil.rmtree(j.folder,ignore_errors=True)


def process_one(job:Job):
    if job.url:
        job.status='fetching';job.phase='Retrieving public social video'
        try: job.filepath=fetch_public_video(job.url,job.folder)
        except Exception as exc:
            raise ValueError('The platform did not provide an accessible public video. Try uploading a saved file.') from exc
    job.status='processing';job.phase='Inspecting video signals and provenance'
    return analyze_media(job)

async def worker():
    while True:
        id=await queue.get()
        job=jobs.get(id)
        if job:
            try:
                job.result=await asyncio.wait_for(asyncio.to_thread(process_one,job),timeout=210)
                job.status='complete';job.phase='Report ready'
            except asyncio.TimeoutError:
                job.status='error';job.error='Processing timed out. Try a shorter video.'
            except ValueError as e:
                job.status='error';job.error=str(e)
            except Exception:
                job.status='error';job.error='Analysis could not be completed for this video.'
            finally:
                shutil.rmtree(job.folder,ignore_errors=True)
        queue.task_done()

async def start_workers():
    global queue
    queue = asyncio.Queue(maxsize=QUEUE_LIMIT)
    jobs.clear()
    active_tasks.clear()
    for _ in range(WORKERS): active_tasks.append(asyncio.create_task(worker()))

async def stop_workers():
    for task in active_tasks: task.cancel()
    if active_tasks: await asyncio.gather(*active_tasks,return_exceptions=True)
    active_tasks.clear()
    for job in jobs.values():
        shutil.rmtree(job.folder,ignore_errors=True)
    jobs.clear()

@router.get('/health')
async def health():
    return {'status':'ok','model_enabled':bool(API_USER and API_SECRET),'queue_size':queue.qsize()}

@router.post('/jobs/url')
async def add_url(payload:LinkRequest,request:Request):
    per_ip_limit(request)
    try: url,site=validate_url(payload.url)
    except ValueError as e: raise HTTPException(400,str(e)) from e
    if queue.full(): raise HTTPException(503,'All analysis slots are busy. Please try shortly.')
    cleanup_old()
    id=uuid4().hex
    job=Job(id=id,source=site,folder=Path(tempfile.mkdtemp(prefix='framewise-')),url=url)
    jobs[id]=job
    try:queue.put_nowait(id)
    except asyncio.QueueFull:
        jobs.pop(id,None);shutil.rmtree(job.folder,ignore_errors=True)
        raise HTTPException(503,'Analysis queue full. Try again shortly.')
    return {'id':id,'status':'queued'}

@router.post('/jobs/upload')
async def add_upload(request:Request,video:UploadFile=File(...)):
    per_ip_limit(request)
    if queue.full(): raise HTTPException(503,'All analysis slots are busy. Please try shortly.')
    suffix=Path(video.filename or '').suffix.lower()
    if suffix not in VIDEO_SUFFIXES:
        raise HTTPException(400,'Upload an MP4, MOV, WebM, M4V or MKV video.')
    if request.headers.get('content-length') and int(request.headers['content-length'])>MAX_BYTES+2*1024*1024:
        raise HTTPException(413,f'The file is larger than {MAX_BYTES//1048576} MB.')
    cleanup_old()
    folder=Path(tempfile.mkdtemp(prefix='framewise-'))
    path=folder/('upload'+suffix)
    size=0
    try:
        with path.open('wb') as f:
            while True:
                chunk=await video.read(1024*1024)
                if not chunk: break
                size+=len(chunk)
                if size>MAX_BYTES: raise HTTPException(413,f'Maximum file size is {MAX_BYTES//1048576} MB.')
                f.write(chunk)
        if size<512: raise HTTPException(400,'The uploaded file is empty or too small.')
        id=uuid4().hex
        jobs[id]=Job(id=id,source='Uploaded file',folder=folder,filepath=path)
        try:queue.put_nowait(id)
        except asyncio.QueueFull: raise HTTPException(503,'Analysis queue full. Try again shortly.')
    except Exception:
        shutil.rmtree(folder,ignore_errors=True)
        if 'id' in locals(): jobs.pop(id,None)
        raise
    finally:await video.close()
    return {'id':id,'status':'queued'}

@router.get('/jobs/{id}')
async def get_job(id:str):
    if not re.fullmatch(r'[a-f0-9]{32}',id):raise HTTPException(404,'Report not found.')
    j=jobs.get(id)
    if not j or time.monotonic()-j.created>JOB_TTL: raise HTTPException(404,'This report expired. Analyze again.')
    return {'id':id,'status':j.status,'phase':j.phase,'error':j.error,
            'result':j.result if j.status=='complete' else None}


