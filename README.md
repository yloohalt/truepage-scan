# TruePage Scan

A private OpenCV page scanner with a browser review screen and an OAuth-protected MCP server.

## What works

- Upload JPEG/PNG photographs, up to 12 MiB and 12 megapixels.
- Apply EXIF orientation; manually rotate into reading orientation.
- Suggest page boundaries, then review or replace the four corners.
- Correct planar perspective using the original photograph and export a lossless PNG.
- For spreads, export the left page first; choose **Scan right page** or **Pass** afterward.
- Optional mild sharpening; no image generation, OCR replacement, or invented text.
- Private, unguessable upload sessions; deletion on demand and expiration after 30 minutes.
- MCP tools: `create_upload_session`, `get_scan_status`, `scan_uploaded_page`, `delete_session`.
- OAuth discovery, dynamic client registration, owner approval, PKCE verification, and single-use authorization codes, using the official MCP Python SDK.

## Limits you should know

This is a single-owner prototype. Page detection and layout classification are heuristics and **always require review**. Reading direction and language are not recognized automatically. Flat perspective correction does not flatten book curvature or recover hidden, blurred, or glared-out print. Resampling changes pixel values; it does not reconstruct missing information. Export resolution follows the source, without artificial upscaling.

Chat attachments are not automatically accessible to this server. The MCP tool supplies a browser link where you upload the original photograph and review its corners. All spreads use the left-first workflow, regardless of language.

Session IDs and download URLs are bearer capabilities: anyone with a link can access that session. Never post them publicly. Data is temporary, unencrypted on the server's ephemeral filesystem, and is deleted at expiry by a 30-second cleanup loop, on explicit deletion, or on normal shutdown. Abrupt process termination may leave temporary files until the hosting instance's filesystem is discarded. Keep one process/replica, with access logs disabled. Do not use this prototype as a multi-user service or for sensitive documents without a proper identity/storage review.

OAuth clients and tokens are held in memory. Access lasts one hour and is lost on restart; reconnect when needed. No refresh tokens are issued. Public dynamic registration is bounded to 256 clients per process. Restart to clear obsolete client registrations if that limit is reached. For long-term/shared use, replace this owner-key OAuth provider with a managed identity provider and durable token/client storage.

## Run locally (Python 3.12)

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
$env:TRUEPAGE_API_KEY = (& .\.venv\Scripts\python -c "import secrets; print(secrets.token_urlsafe(32))")
$env:PUBLIC_BASE_URL = 'http://127.0.0.1:8000'
.\.venv\Scripts\python -m uvicorn truepage.server:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

Open `http://127.0.0.1:8000`. Enter the server key locally in the browser. Do not commit it or send it in chat. The key is in the current terminal environment; use your own password manager for storage.

## Test

```sh
python -m pytest -q
```

Tests cover exact identity/crop pixel preservation, invalid geometry, EXIF rotation, image validation, API authentication, expiration/deletion, left-first enforcement, MCP initialization/discovery/tool calls, OAuth approval, PKCE mismatch, and authorization-code replay. These synthetic tests do not establish accuracy on real textbook photographs.

## Deploy to Render

The repository includes a non-root Docker image and `render.yaml` for one free web service. The Blueprint generates `TRUEPAGE_API_KEY` securely. For direct service creation, set an equivalent random key of at least 32 characters. Never commit a real key.

The server uses Render's `RENDER_EXTERNAL_URL` for its public origin and OAuth audience. For a custom domain, set `PUBLIC_BASE_URL` to that exact HTTPS origin without a trailing slash. Deploy one worker/replica. `/healthz` reports liveness; `/mcp` is the Streamable HTTP endpoint. Free-service restarts/suspension invalidate temporary scans and OAuth state.

Find the access key under the service's **Environment** settings. It is used on the scan page and OAuth approval page, never inside the plugin package.

## Connect the plugin

`plugin-template/truepage-scan` contains the manifest and workflow skill. It deliberately has no fictional server URL. Once the live endpoint is verified, generate `mcp.json` with:

```sh
python scripts/package_plugin.py https://YOUR-ACTUAL-SERVICE.onrender.com/mcp
```

The resulting private plugin ZIP contains the verified URL only, never credentials. Import/save it with Plugin Creator, then connect through the host's OAuth flow. On the TruePage approval page, enter the server access key retrieved from Render. Never enter your GitHub password or Render API key there. A successful server test or plugin upload alone does not prove the host connection is complete.

## References

- [Official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x)
- [Render Blueprint reference](https://render.com/docs/blueprint-spec)

The app uses a pinned MCP 1.x release; migration to MCP 2.x requires updating the server API.
