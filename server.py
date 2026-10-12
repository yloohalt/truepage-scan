"""TruePage Scan cloud API + MCP Streamable HTTP endpoint.

Install requirements.txt before starting; for local engine-only testing use pytest.
"""
from __future__ import annotations
import base64
import binascii
import io
import json
import os
import secrets
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

import cv2
import numpy as np
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from PIL import Image, UnidentifiedImageError
from scan_engine import scan_page

from mcp.server.fastmcp import FastMCP

Image.MAX_IMAGE_PIXELS = 30_000_000
MAX_UPLOAD = 15 * 1024 * 1024
TTL_SECONDS = 60 * 60 * 12
STORE = Path(os.getenv('SCAN_OUTPUT_DIR', tempfile.gettempdir() + '/truepage-scans'))
STORE.mkdir(mode=0o700, parents=True, exist_ok=True)
ACCESS_KEY = os.getenv('SCAN_ACCESS_KEY', '')
PUBLIC_URL = os.getenv('PUBLIC_BASE_URL', '').rstrip('/')


def auth_header(authorization: str | None):
    if ACCESS_KEY and not secrets.compare_digest(authorization or '', f'Bearer {ACCESS_KEY}'):
        raise HTTPException(status_code=401, detail='Invalid access key')


def clean_old_files() -> None:
    for path in STORE.glob('*.png'):
        try:
            if path.stat().st_mtime < time.time() - TTL_SECONDS:
                path.unlink(missing_ok=True)
        except OSError:
            pass


def _read_photo(data: bytes) -> np.ndarray:
    if not 0 < len(data) <= MAX_UPLOAD:
        raise ValueError('Image exceeds the 15 MB upload limit or is empty')
    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.format not in ('JPEG', 'PNG', 'WEBP'):
                raise ValueError('Only JPEG, PNG, and WebP photographs are supported')
            if im.width * im.height > 30_000_000:
                raise ValueError('Image is too large (30 megapixel limit)')
            im.verify()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError('Invalid or oversized photograph') from exc
    array = np.frombuffer(data, dtype=np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError('Could not decode the image')
    return image


def process_photo(data: bytes, page: str = 'left', rotation: str = 'auto',
                  spread: str = 'auto', width: int = 0, enhance: bool = True,
                  corners_json: str = '') -> dict:
    clean_old_files()
    image = _read_photo(data)
    if spread not in ('auto','two','single'):
        raise ValueError('spread must be auto, two, or single')
    two_pages = {'auto': None, 'two': True, 'single': False}[spread]
    corners = json.loads(corners_json) if corners_json else None
    result = scan_page(image, page=page, rotation=rotation,
                       two_pages=two_pages, corners=corners,
                       output_width=width, light_enhance=enhance)
    ok, image_bytes = cv2.imencode('.png', result.image, [cv2.IMWRITE_PNG_COMPRESSION, 3])
    if not ok:
        raise ValueError('Could not encode the PNG scan')
    scan_id = secrets.token_urlsafe(24)
    path = STORE / f'{scan_id}.png'
    path.write_bytes(image_bytes.tobytes())
    os.chmod(path, 0o600)
    prefix = PUBLIC_URL
    return {'status':'ok', 'scan_id':scan_id, 'page':result.page,
            'two_pages':result.two_pages, 'rotation':result.rotation,
            'geometry':result.geometry, 'width':result.image.shape[1],
            'height':result.image.shape[0], 'warnings':result.warnings,
            'download_url':f'{prefix}/files/{scan_id}.png',
            'expires_in_hours':12}


mcp = FastMCP('TruePage Scan', stateless_http=True, json_response=True,
              streamable_http_path='/')


@mcp.tool()
def scan_image_base64(image_base64: str, page: str = 'left',
                      rotation: str = 'auto', spread: str = 'auto',
                      output_width: int = 0, enhance: bool = True,
                      corners_json: str = '') -> dict:
    """Scan an uploaded image provided as base64 JPEG/PNG/WebP bytes. For two English
    pages, scan ONLY the LEFT page first. Ask the user before scanning right.
    Do not claim fidelity you cannot guarantee. May use manual normalized corners.
    Large chat attachments may not be available as base64: offer web upload.
    """
    try:
        # Accept standard base64 or data:...;base64,...
        encoded = image_base64.split(',', 1)[-1] if image_base64.startswith('data:') else image_base64
        # Early cap: a 15 MiB image has about 20 MiB base64 characters.
        if len(encoded) > (MAX_UPLOAD * 4 // 3 + 16):
            raise ValueError('Encoded image exceeds 15 MB')
        data = base64.b64decode(encoded, validate=True)
        return process_photo(data, page, rotation, spread, output_width, enhance, corners_json)
    except (ValueError, json.JSONDecodeError, binascii.Error) as exc:
        return {'status':'error', 'message':str(exc)}


@mcp.tool()
def get_scan_download(scan_id: str) -> dict:
    """Return a temporary download link for an existing TruePage Scan result."""
    if not scan_id or not all(c.isalnum() or c in '_-' for c in scan_id) or len(scan_id) > 128:
        return {'status':'error','message':'Invalid scan ID'}
    p = STORE / f'{scan_id}.png'
    if not p.is_file() or p.stat().st_mtime < time.time() - TTL_SECONDS:
        return {'status':'error','message':'Scan not found or expired'}
    return {'status':'ok','download_url':f'{PUBLIC_URL}/files/{scan_id}.png',
            'expires_in_hours':12}


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with mcp.session_manager.run():
        yield

app = FastAPI(title='TruePage Scan', version='0.1.0', lifespan=lifespan)


@app.middleware('http')
async def guard_mcp(request: Request, call_next):
    if request.url.path.startswith('/mcp') and ACCESS_KEY:
        try:
            auth_header(request.headers.get('authorization'))
        except HTTPException:
            return JSONResponse({'error':'Authentication required'}, status_code=401)
    return await call_next(request)


@app.get('/health')
async def health():
    return {'status':'ok', 'service':'TruePage Scan'}


@app.post('/api/scan')
async def scan_upload(file: Annotated[UploadFile, File()],
                      authorization: Annotated[str | None, Header()] = None,
                      page: Annotated[str, Form()] = 'left',
                      rotation: Annotated[str, Form()] = 'auto',
                      spread: Annotated[str, Form()] = 'auto',
                      output_width: Annotated[int, Form()] = 0,
                      enhance: Annotated[bool, Form()] = True,
                      corners_json: Annotated[str, Form()] = ''):
    auth_header(authorization)
    try:
        # Extra byte detects over-limit without reading whole upload.
        data = await file.read(MAX_UPLOAD+1)
        return process_photo(data, page, rotation, spread, output_width, enhance, corners_json)
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        await file.close()


@app.get('/files/{scan_id}.png')
async def download(scan_id: str):
    # Unpredictable ID acts as a capability URL; avoid listing or indexing.
    if not scan_id or not all(c.isalnum() or c in '_-' for c in scan_id) or len(scan_id)>128:
        raise HTTPException(status_code=404)
    path = STORE / f'{scan_id}.png'
    if not path.is_file() or path.stat().st_mtime < time.time()-TTL_SECONDS:
        raise HTTPException(status_code=404, detail='Scan not found or expired')
    return FileResponse(path, media_type='image/png', filename='truepage-scan.png',
                        headers={'Cache-Control':'private, no-store',
                                 'X-Content-Type-Options':'nosniff'})


HTML = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>TruePage Scan</title>
<style>body{font:16px system-ui;background:#f3f6f9;color:#172a40;margin:0}main{max-width:720px;margin:30px auto;padding:24px;background:white;border-radius:20px;box-shadow:0 10px 30px #14283716}h1{margin:0 0 4px}p{color:#516277;line-height:1.5}label{display:block;margin-top:15px;font-weight:600}input,select{display:block;margin-top:5px;padding:10px;width:100%;max-width:100%;box-sizing:border-box;border:1px solid #bbc9d3;border-radius:9px;background:white}button{margin:18px 8px 8px 0;padding:12px 17px;border:0;border-radius:9px;background:#126b75;color:white;font-weight:700;cursor:pointer}.secondary{background:#234271}img{display:block;max-width:100%;margin-top:14px;border:1px solid #cbd4dc}a{color:#09616b}small{color:#596d7f}#message{white-space:pre-wrap;margin-top:10px}input[type=checkbox]{width:auto;display:inline;margin-right:8px}</style></head><body><main><h1>TruePage Scan</h1><p>Pixel-preserving OpenCV page scans. For two English pages, the <b>left page is processed first</b>. No generated text or artwork.</p>
<label>Photo (JPEG, PNG, WebP; max 15 MB)<input type=file id=photo accept="image/png,image/jpeg,image/webp"></label>
<label>Access key (if configured)<input type=password id=key placeholder="Your private server key" autocomplete=off></label>
<label>Page to process<select id=page><option value=left>Left page first</option><option value=right>Right page</option><option value=single>Single page</option></select></label>
<label>Photo orientation<select id=rotation><option value=auto>Automatic</option><option value=none>Already upright</option><option value=ccw>Rotate 90° counterclockwise</option><option value=cw>Rotate 90° clockwise</option><option value=180>Rotate 180°</option></select></label>
<label>Photo layout<select id=spread><option value=auto>Automatic</option><option value=two>Two-page spread</option><option value=single>Single page</option></select></label>
<label><input type=checkbox id=enhance checked>Conservative brightness/sharpness enhancement</label>
<button onclick="go()" id=start>Scan page</button><button class=secondary id=right style="display:none" onclick="document.getElementById('page').value='right';go()">Approve: scan right page</button>
<div id=message role=status></div><div id=result></div><small>Manual page-corner override is available through the API. Downloads expire after 12 hours. Curved pages may require manual correction.</small></main>
<script>
async function go(){const file=document.getElementById('photo').files[0];if(!file){alert('Choose a photo');return};const page=document.getElementById('page').value;const fd=new FormData();fd.append('file',file);for(const key of ['page','rotation','spread'])fd.append(key,document.getElementById(key).value);fd.append('enhance',document.getElementById('enhance').checked?'true':'false');
const msg=document.getElementById('message'),res=document.getElementById('result');msg.textContent='Scanning…';res.innerHTML='';document.getElementById('start').disabled=true;
try{const token=document.getElementById('key').value;const response=await fetch('/api/scan',{method:'POST',headers:token?{'Authorization':'Bearer '+token}:{},body:fd});const data=await response.json();if(!response.ok)throw Error(data.detail||'Scan failed');let url=data.download_url;msg.textContent='Page: '+data.page+' • '+data.width+'×'+data.height+' • geometry: '+data.geometry+(data.warnings.length?'\n'+data.warnings.join('\n'):'');const link=document.createElement('a');link.href=url;link.textContent='Download original-content PNG';link.download='truepage-'+data.page+'.png';res.append(link);const img=document.createElement('img');img.src=url;img.alt='Scanned page preview';res.append(img);document.getElementById('right').style.display=(data.two_pages && page==='left')?'inline-block':'none';}catch(e){msg.textContent='Error: '+e.message}finally{document.getElementById('start').disabled=false}}
</script></body></html>'''


@app.get('/', response_class=HTMLResponse)
async def home():
    return HTML

# MCP Streamable HTTP endpoint: /mcp/
app.mount('/mcp', mcp.streamable_http_app())
