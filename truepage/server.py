import asyncio
import contextlib
import os
import secrets
import shutil
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route

from .scanner import MAX_BYTES, decode, png, proposals, rectify, rotate
from .auth import OwnerOAuth

TTL = 1800
MAX_JOBS = 10
ROOT = Path(tempfile.mkdtemp(prefix="truepage-"))
JOBS = {}
LOCK = threading.RLock()
API_KEY = os.environ.get("TRUEPAGE_API_KEY", "")
BASE_URL = os.environ.get("PUBLIC_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL", "http://127.0.0.1:8000")
BASE_URL = BASE_URL.rstrip("/")
if len(API_KEY) < 32:
    raise RuntimeError("Set TRUEPAGE_API_KEY to a random secret of at least 32 characters.")

oauth = OwnerOAuth(BASE_URL, API_KEY)
mcp = FastMCP("TruePage Scan", stateless_http=True, json_response=True, auth_server_provider=oauth,
    auth=AuthSettings(issuer_url=BASE_URL, resource_server_url=BASE_URL+'/mcp', validate_token_resource=True,
        required_scopes=['scan'], client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=['scan'], default_scopes=['scan']),
        revocation_options=RevocationOptions(enabled=True)),
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
        allowed_hosts=[urlparse(BASE_URL).netloc, "127.0.0.1:*", "localhost:*", "testserver"],
        allowed_origins=[BASE_URL]))


def cleanup():
    with LOCK:
        for ident, job in list(JOBS.items()):
            if job["expires"] <= time.time():
                shutil.rmtree(ROOT / ident, ignore_errors=True)
                del JOBS[ident]


def get_job(ident):
    cleanup()
    if ident not in JOBS:
        raise ValueError("Upload session not found or expired. Start a new session.")
    return JOBS[ident]


@mcp.tool()
def create_upload_session() -> dict:
    """Create a private 30-minute browser upload/review link. Open it to upload a photo; attachments are not transferred automatically. Treat the link and session ID as secrets."""
    with LOCK:
        cleanup()
        if len(JOBS) >= MAX_JOBS:
            raise ValueError("Session limit reached. Delete an old session or wait for expiry.")
        ident = secrets.token_urlsafe(32)
        (ROOT / ident).mkdir()
        JOBS[ident] = {"expires": time.time() + TTL, "uploaded": False, "scans": {}, "layout": None}
        return {"session_id": ident, "upload_url": f"{BASE_URL}/#{ident}", "expires_in_seconds": TTL}


@mcp.tool()
def get_scan_status(session_id: str) -> dict:
    """Read upload status and available PNG links. After a left-page scan, ask whether to scan the right page or pass. Download URLs are private bearer links and expire with the session."""
    with LOCK:
        job = get_job(session_id)
        return {"uploaded": job["uploaded"], "layout": job["layout"], "rotation": job.get("rotation", 0),
                "expires_at": job["expires"], "scans": dict(job["scans"]),
                "next_step": "Scan the right page or pass?" if "left" in job["scans"] and "right" not in job["scans"] else "Open the upload link to review and export."}


@mcp.tool()
def scan_uploaded_page(session_id: str, corners: list[list[float]], page: str = "left",
                       layout: str = "spread", clockwise_rotation: int = 0,
                       reviewed: bool = False, enhance: bool = False) -> dict:
    """Export a reviewed page from an uploaded photo. Require the user's explicit confirmation of reading orientation and corners first. Corners are TL, TR, BR, BL in full-resolution pixels AFTER clockwise rotation. For spreads export left first; export right only after the user requests it. No text is generated or reconstructed."""
    with LOCK:
        job = get_job(session_id)
        if not reviewed:
            raise ValueError("Review orientation and page corners before exporting.")
        if layout not in ("single", "spread") or page not in (("single",) if layout == "single" else ("left", "right")):
            raise ValueError("Choose single/single or spread/left or spread/right.")
        if not job["uploaded"]:
            raise ValueError("Upload a photo first.")
        if job["layout"] is not None and (job["layout"] != layout or job["rotation"] != clockwise_rotation):
            raise ValueError("Layout and rotation cannot change after export. Start a new session.")
        if page == "right" and "left" not in job["scans"]:
            raise ValueError("Export the left page first, then choose whether to scan the right.")
        rgb = rotate(decode((ROOT / session_id / "source").read_bytes()), clockwise_rotation)
        out = rectify(rgb, corners, enhance)
        (ROOT / session_id / f"{page}.png").write_bytes(png(out))
        result = {"page": page, "width": out.shape[1], "height": out.shape[0],
                  "download_url": f"{BASE_URL}/download/{session_id}/{page}.png",
                  "enhanced": enhance, "note": "Perspective interpolation changes pixel values; no text or illustration is synthesized."}
        job["scans"][page] = result
        job["layout"], job["rotation"] = layout, clockwise_rotation
        return result


@mcp.tool()
def delete_session(session_id: str) -> dict:
    """Delete this session's uploaded photo and exported scans immediately."""
    with LOCK:
        get_job(session_id)
        shutil.rmtree(ROOT / session_id)
        del JOBS[session_id]
        return {"deleted": True}


def authorize(request):
    return secrets.compare_digest(request.headers.get("authorization", ""), f"Bearer {API_KEY}")


async def home(request):
    return FileResponse(Path(__file__).with_name("index.html"))


async def health(request):
    return JSONResponse({"status": "ok"})


async def new_session(request):
    if not authorize(request):
        return JSONResponse({"error": "Enter your server access key."}, status_code=401)
    try:
        return JSONResponse(await run_in_threadpool(create_upload_session))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=429)


async def job_api(request):
    ident = request.path_params["ident"]
    try:
        if request.method == "GET":
            return JSONResponse(await run_in_threadpool(get_scan_status, ident))
        if request.method == "DELETE":
            return JSONResponse(await run_in_threadpool(delete_session, ident))
        data = await request.body()
        def save():
            with LOCK:
                job = get_job(ident)
                if job["uploaded"]:
                    raise ValueError("This session already contains a photo. Start a new session to replace it.")
                rgb = decode(data)
                (ROOT / ident / "source").write_bytes(data)
                job["uploaded"] = True
                return proposals(rgb)
        return JSONResponse(await run_in_threadpool(save))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


async def analyze(request):
    try:
        body = await request.json()
        rotation = body.get("rotation", 0)
        def inspect():
            with LOCK:
                job = get_job(request.path_params["ident"])
                if not job["uploaded"]:
                    raise ValueError("Upload a photo first.")
                return proposals(rotate(decode((ROOT / request.path_params["ident"] / "source").read_bytes()), rotation))
        return JSONResponse(await run_in_threadpool(inspect))
    except (ValueError, TypeError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)


async def export(request):
    try:
        b = await request.json()
        result = await run_in_threadpool(scan_uploaded_page, request.path_params["ident"],
            b.get("corners", []), b.get("page", "left"), b.get("layout", "spread"),
            b.get("rotation", 0), b.get("reviewed") is True, b.get("enhance") is True)
        return JSONResponse(result)
    except (ValueError, TypeError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)


async def download(request):
    ident, page = request.path_params["ident"], request.path_params["page"]
    # Copy under lock so expiration/deletion cannot race FileResponse's delayed read.
    from starlette.responses import Response
    with LOCK:
        try:
            job = get_job(ident)
            if page not in job["scans"]:
                raise ValueError("No such scan.")
            data = (ROOT / ident / f"{page}.png").read_bytes()
        except ValueError:
            return JSONResponse({"error": "Scan not found or expired."}, status_code=404)
    return Response(data, media_type="image/png", headers={"Content-Disposition": f'attachment; filename="truepage-{page}.png"'})


async def source(request):
    from starlette.responses import Response
    with LOCK:
        try:
            ident = request.path_params["ident"]
            if not get_job(ident)["uploaded"]:
                raise ValueError("No photo.")
            data = (ROOT / ident / "source").read_bytes()
        except ValueError:
            return JSONResponse({"error": "Photo unavailable."}, status_code=404)
    return Response(data, media_type="image/png" if data.startswith(b"\x89PNG") else "image/jpeg")


@contextlib.asynccontextmanager
async def lifespan(app):
    async def sweeper():
        while True:
            await asyncio.sleep(30)
            await run_in_threadpool(cleanup)
    async with mcp.session_manager.run():
        task = asyncio.create_task(sweeper())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            shutil.rmtree(ROOT, ignore_errors=True)


inner = Starlette(routes=[Route("/", home), Route("/healthz", health),
    Route("/connect", oauth.connect, methods=["GET", "POST"]),
    Route("/api/jobs", new_session, methods=["POST"]),
    Route("/api/jobs/{ident}", job_api, methods=["GET", "PUT", "DELETE"]),
    Route("/api/jobs/{ident}/analyze", analyze, methods=["POST"]),
    Route("/api/jobs/{ident}/export", export, methods=["POST"]),
    Route("/api/jobs/{ident}/source", source),
    Route("/download/{ident}/{page}.png", download),
    Mount("/", mcp.streamable_http_app())], lifespan=lifespan)


class Guard:
    """Bound streaming bodies, require MCP authentication, and disable caching."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        # Do not allow cross-origin browser writes (capability URLs must stay private).
        origin = headers.get(b"origin")
        if origin and origin.decode() != BASE_URL:
            return await JSONResponse({"error": "Origin not allowed"}, status_code=403)(scope, receive, send)
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > MAX_BYTES:
                return await JSONResponse({"error": "Upload exceeds 12 MiB."}, status_code=413)(scope, receive, send)
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        delivered = False
        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()
        async def safe_send(message):
            if message["type"] == "http.response.start":
                message["headers"] += [(b"cache-control", b"no-store"), (b"referrer-policy", b"no-referrer"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"content-security-policy", b"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'")]
            await send(message)
        await self.app(scope, bounded_receive, safe_send)


app = Guard(inner)
