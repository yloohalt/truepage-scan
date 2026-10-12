---
name: scan-page
description: Use when the user wants to scan a photographed textbook or document with TruePage Scan and export a faithful PNG.
---

Use the connected TruePage Scan tools. Never use image generation, rewrite text, translate it, redraw illustrations, or invent missing details.

1. Call create_upload_session and give the user its private upload link. Explain that attached chat images do not transfer to the backend automatically: upload the original JPEG/PNG through that link.
2. Have the user confirm upright reading orientation, one page versus a spread, and the four page corners in the browser. Detection is only a proposal. Do not set reviewed=true without actual user review. Corners use full-resolution coordinates after the requested rotation, ordered TL, TR, BR, BL.
3. For a spread, scan the left page first. Return its download link, then ask "Scan the right page or pass?" Do not export the right page until requested.
4. Read get_scan_status for available PNG links. The browser also performs exports. Do not claim a scan exists until the tool returns one.
5. Keep enhancement off unless requested. Explain relevant limitations: perspective resampling changes pixels, severe blur/glare/hidden print cannot be restored, and curved pages are not flattened.
6. Session IDs and links grant private access. Do not share them outside the user's conversation. Download within 30 minutes of session creation; delete_session removes the photo and scans immediately.

This template is not connected until a verified live MCP endpoint is added and its authentication configured through the host's supported secret flow. Never place the server access key in this skill, a public repository, or chat.
