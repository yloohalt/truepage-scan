"""Pixel-preserving book scan geometry. No text generation and no OCR.

Limitations: a photographed, curved page cannot be perfectly flattened by a
single homography. Unknown or low-confidence corners require manual override.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import cv2
import numpy as np

Page = Literal['left', 'right', 'single']
Rotation = Literal['auto', 'none', 'cw', 'ccw', '180']


@dataclass
class ScanResult:
    image: np.ndarray
    page: str
    two_pages: bool
    rotation: str
    geometry: str
    warnings: list[str]


def _order_quad(points: np.ndarray) -> np.ndarray:
    pts = np.asarray(points, dtype=np.float32).reshape(4, 2)
    sums = pts.sum(axis=1)
    diffs = np.diff(pts, axis=1).reshape(-1)
    return np.asarray([pts[np.argmin(sums)], pts[np.argmin(diffs)],
                       pts[np.argmax(sums)], pts[np.argmax(diffs)]], dtype=np.float32)


def _line_score(image: np.ndarray) -> float:
    """Rough horizontal text-line score, for detecting 90-degree rotation."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    h, w = gray.shape
    scale = 900 / max(h, w)
    gray = cv2.resize(gray, (max(1, int(w*scale)), max(1, int(h*scale))))
    binary = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                   cv2.THRESH_BINARY_INV, 31, 15)
    # Wide text runs are substantially more common in upright documents.
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (19, 2))
    merged = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(merged, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    score = 0.0
    for c in contours:
        x, y, cw, ch = cv2.boundingRect(c)
        if 25 <= cw <= gray.shape[1]*0.75 and 2 <= ch <= 31 and cw/ch >= 3.3:
            score += min(cw, 140) / 140
    return score


def orient(image: np.ndarray, rotate: Rotation = 'auto') -> tuple[np.ndarray, str]:
    options = {'cw': cv2.ROTATE_90_CLOCKWISE,
               'ccw': cv2.ROTATE_90_COUNTERCLOCKWISE,
               '180': cv2.ROTATE_180}
    if rotate == 'none':
        return image, 'none'
    if rotate in options:
        return cv2.rotate(image, options[rotate]), rotate
    if rotate != 'auto':
        raise ValueError('rotation must be auto, none, cw, ccw, or 180')
    if _line_score(cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)) > _line_score(image)*1.3:
        # Most sideways book photos are turned clockwise by the camera.
        # Other orientations can be explicitly selected in the upload UI.
        return cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE), 'ccw'
    return image, 'none'


def estimate_single_quad(image: np.ndarray) -> tuple[np.ndarray, str]:
    h, w = image.shape[:2]
    scaled = cv2.resize(image, (int(w * min(1.0, 1000/max(w,h))),
                                int(h * min(1.0, 1000/max(w,h)))))
    sh, sw = scaled.shape[:2]
    gray = cv2.cvtColor(scaled, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5,5), 0)
    edges = cv2.Canny(blurred, 35, 105)
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((7,7), np.uint8))
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    for cnt in sorted(contours, key=cv2.contourArea, reverse=True)[:30]:
        area = cv2.contourArea(cnt) / (sw*sh)
        if not (0.20 < area < 0.985):
            continue
        approx = cv2.approxPolyDP(cnt, 0.026 * cv2.arcLength(cnt, True), True)
        if len(approx) != 4 or not cv2.isContourConvex(approx):
            continue
        pts = _order_quad(approx[:,0,:])
        # Avoid choosing a table/panel inside a textbook page.
        if area < 0.45:
            continue
        return pts * np.array([w/sw, h/sh], dtype=np.float32), 'contour'
    # Do not over-crop if detection failed: users can supply exact corners.
    return np.array([[0,0], [w-1,0], [w-1,h-1],[0,h-1]], np.float32), 'full-frame-fallback'


def _spine_fraction(image: np.ndarray) -> float:
    """Conservative seam estimate near center, bias toward the midpoint."""
    h, w = image.shape[:2]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    # Center fold often has a long, dark vertical channel across many rows.
    top, bottom = int(h*.17), int(h*.85)
    if bottom <= top:
        return 0.5
    strip = gray[top:bottom, :].astype(np.float32)
    ink_reduced = cv2.GaussianBlur(strip, (41, 41), 0)
    bands = np.percentile(ink_reduced, 50, axis=0)
    lo, hi = int(w*.43), int(w*.57)
    if hi <= lo:
        return 0.5
    index = np.argmin(bands[lo:hi])+lo
    # Regularize toward center to avoid following dark illustrations.
    return float(np.clip(.5 + .30*(index/w-.5), .47, .53))


def estimate_spread_quad(image: np.ndarray, page: Page) -> np.ndarray:
    """Conservative 4-corner book model; retains page edges and white margins.

    Top edges of book spreads frequently arch towards the spine.
    Exact manual corners are preferred for very curved or asymmetrical books.
    """
    h, w = image.shape[:2]
    spine = _spine_fraction(image)
    # Wide margins deliberately left to protect headers/page numbers.
    left_tl = (.03*w, .005*h)
    left_bl = (.005*w, (h-1)*.998)
    fold_top = (min(.55, spine+.008)*w, .095*h)
    fold_bottom = (min(.56, spine+.020)*w, (h-1)*.995)
    right_tr = (.975*w, .005*h)
    right_br = ((w-1)*.998, (h-1)*.998)
    pts = ([left_tl, fold_top, fold_bottom, left_bl] if page == 'left'
           else [fold_top, right_tr, right_br, fold_bottom])
    return np.asarray(pts, np.float32)


def _warp(image: np.ndarray, points: np.ndarray, output_width: int = 0) -> np.ndarray:
    pts = _order_quad(points)
    tl, tr, br, bl = pts
    width = max(np.linalg.norm(tr-tl), np.linalg.norm(br-bl))
    height = max(np.linalg.norm(bl-tl), np.linalg.norm(br-tr))
    if width < 100 or height < 100:
        raise ValueError('Selected page is too small')
    if output_width:
        out_w = int(np.clip(output_width, 400, 3000))
    else:
        out_w = int(min(2400, max(1400, round(width))))
    out_h = int(np.clip(round(out_w*(height/width)), 400, 4200))
    dst = np.asarray([[0,0], [out_w-1,0], [out_w-1,out_h-1], [0,out_h-1]], np.float32)
    return cv2.warpPerspective(image, cv2.getPerspectiveTransform(pts,dst),
                               (out_w,out_h), flags=cv2.INTER_CUBIC,
                               borderMode=cv2.BORDER_REPLICATE)


def scan_page(image: np.ndarray, page: Page = 'left',
              rotation: Rotation = 'auto', two_pages: bool | None = None,
              corners: list[list[float]] | None = None,
              output_width: int = 0, light_enhance: bool = True) -> ScanResult:
    """Create a rectified raster of the original page.

    `corners` are TL/TR/BR/BL normalized [0..1], measured AFTER rotation.
    This is the reliable fallback when automated detection is uncertain.
    """
    if image is None or image.ndim != 3 or image.shape[2] != 3:
        raise ValueError('Image must be a 3-channel color image')
    if page not in ('left', 'right', 'single'):
        raise ValueError('page must be left, right, or single')
    # In this workflow, selecting left/right means a two-page spread.
    # A portrait-shaped photo of a spread is typically captured sideways.
    if rotation == 'auto' and page in ('left','right') and image.shape[0] > image.shape[1] * 1.12:
        work, actual_rot = orient(image, 'ccw')
    else:
        work, actual_rot = orient(image, rotation)
    h, w = work.shape[:2]
    if two_pages is None:
        # Spread photos have comparable width and height or are landscape.
        two_pages = bool(w/h >= 1.18)
    if page == 'single' and two_pages:
        raise ValueError('Two-page image detected; choose left or right page')
    if two_pages and page == 'single':
        raise ValueError('Choose left or right')
    if (not two_pages) and page == 'right':
        raise ValueError('Right page requested but two-page mode is disabled')
    warnings = []
    if corners is not None:
        pts = np.asarray(corners, dtype=np.float32)
        if pts.shape != (4,2) or not np.isfinite(pts).all() or np.min(pts) < 0 or np.max(pts) > 1:
            raise ValueError('corners must contain four normalized [x,y] points in [0,1]')
        pts *= np.array([w-1,h-1],np.float32)
        geometry = 'manual-corners'
    elif two_pages:
        pts = estimate_spread_quad(work, page)
        geometry = 'estimated-book-geometry'
        warnings.append('Page corners are estimated; inspect margins and adjust corners if needed.')
        warnings.append('A 4-corner perspective transform cannot fully flatten a curved page.')
    else:
        pts, geometry = estimate_single_quad(work)
        if geometry == 'full-frame-fallback':
            warnings.append('Page contour not confidently found; full frame retained to avoid cropping.')
    output = _warp(work, pts, output_width)
    if light_enhance:
        # Restrained, non-generative contrast/sharpening. Keep original colors.
        lab = cv2.cvtColor(output,cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        l2 = cv2.createCLAHE(clipLimit=1.3, tileGridSize=(12,12)).apply(l)
        l = cv2.addWeighted(l, .83, l2, .17, 0)
        output = cv2.cvtColor(cv2.merge((l,a,b)),cv2.COLOR_LAB2BGR)
        output = cv2.addWeighted(output,1.12,cv2.GaussianBlur(output,(0,0),0.7),-.12,0)
    return ScanResult(output, page, bool(two_pages),actual_rot,geometry,warnings)
