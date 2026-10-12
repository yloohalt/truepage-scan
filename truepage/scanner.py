from io import BytesIO
import warnings

import cv2
import numpy as np
from PIL import Image, ImageOps

MAX_BYTES = 12 * 1024 * 1024
MAX_PIXELS = 12_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS
cv2.setNumThreads(1)


def decode(data: bytes) -> np.ndarray:
    if not data or len(data) > MAX_BYTES:
        raise ValueError("Upload a JPEG or PNG smaller than 12 MiB.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as im:
                if im.format not in {"JPEG", "PNG"} or im.width * im.height > MAX_PIXELS:
                    raise ValueError("Use JPEG/PNG with at most 12 megapixels.")
                im = ImageOps.exif_transpose(im)
                if min(im.size) < 64:
                    raise ValueError("Image is too small to scan.")
                # Composite transparency onto paper white.
                rgba = im.convert("RGBA")
                rgb = Image.new("RGBA", rgba.size, "white")
                rgb.alpha_composite(rgba)
                return np.asarray(rgb.convert("RGB")).copy()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("Image exceeds the safe pixel limit.") from exc
    except (OSError, SyntaxError) as exc:
        raise ValueError("The file is not a readable JPEG or PNG.") from exc


def rotate(rgb: np.ndarray, clockwise: int) -> np.ndarray:
    if clockwise not in (0, 90, 180, 270):
        raise ValueError("Rotation must be 0, 90, 180, or 270 degrees.")
    return np.rot90(rgb, -(clockwise // 90)).copy()


def validate_corners(points, width, height):
    p = np.asarray(points, dtype=np.float32)
    if p.shape != (4, 2) or not np.isfinite(p).all():
        raise ValueError("Supply four finite corners: top-left, top-right, bottom-right, bottom-left.")
    if (p < 0).any() or (p[:, 0] > width - 1).any() or (p[:, 1] > height - 1).any():
        raise ValueError("All corners must be inside the image.")
    if not cv2.isContourConvex(p.reshape(-1, 1, 2)) or cv2.contourArea(p, oriented=True) < 1024:
        raise ValueError("Corners must form a clockwise, non-crossing page of at least 1024 pixels.")
    if np.min(np.linalg.norm(p - np.roll(p, 1, axis=0), axis=1)) < 16:
        raise ValueError("Page edges must be at least 16 pixels long.")
    return p


def rectify(rgb, corners, enhance=False):
    h, w = rgb.shape[:2]
    p = validate_corners(corners, w, h)
    width = round(max(np.linalg.norm(p[1]-p[0]), np.linalg.norm(p[2]-p[3]))) + 1
    height = round(max(np.linalg.norm(p[3]-p[0]), np.linalg.norm(p[2]-p[1]))) + 1
    if width * height > MAX_PIXELS:
        raise ValueError("Output exceeds the 12 megapixel limit.")
    dst = np.float32([[0, 0], [width-1, 0], [width-1, height-1], [0, height-1]])
    matrix = cv2.getPerspectiveTransform(p, dst)
    out = cv2.warpPerspective(rgb, matrix, (width, height), flags=cv2.INTER_LINEAR)
    if enhance:
        # Mild sharpening only; no thresholding, content synthesis, or color replacement.
        blur = cv2.GaussianBlur(out, (0, 0), 0.8)
        out = cv2.addWeighted(out, 1.15, blur, -0.15, 0)
    return out


def png(rgb):
    stream = BytesIO()
    Image.fromarray(rgb).save(stream, format="PNG")
    return stream.getvalue()


def proposals(rgb):
    """Heuristic candidates only. Always require human review before cropping."""
    h, w = rgb.shape[:2]
    scale = min(1.0, 1000 / max(h, w))
    small = cv2.resize(rgb, None, fx=scale, fy=scale)
    gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(cv2.GaussianBlur(gray, (5, 5), 0), 40, 120)
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    found = []
    for c in sorted(contours, key=cv2.contourArea, reverse=True)[:12]:
        poly = cv2.approxPolyDP(c, 0.025 * cv2.arcLength(c, True), True)
        if len(poly) != 4 or cv2.contourArea(poly) < small.shape[0] * small.shape[1] * .12:
            continue
        p = poly.reshape(4, 2).astype(np.float32) / scale
        center = p.mean(axis=0)
        p = p[np.argsort(np.arctan2(p[:, 1]-center[1], p[:, 0]-center[0]))]
        p = np.roll(p, -np.argmin(p.sum(axis=1)), axis=0)
        p[:, 0] = p[:, 0].clip(0, w-1)
        p[:, 1] = p[:, 1].clip(0, h-1)
        try:
            validate_corners(p, w, h)
        except ValueError:
            continue
        found.append(p.tolist())
    found = sorted(found[:2], key=lambda q: np.mean(q, axis=0)[0])
    return {"width": w, "height": h, "candidates": found,
            "suggested_layout": "spread" if len(found) == 2 else "single",
            "review_required": True,
            "warning": "Page detection is a suggestion. Confirm orientation, layout, and all four corners. Curved pages are not flattened."}
