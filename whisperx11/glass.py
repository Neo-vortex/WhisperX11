"""Liquid-glass rendering: refraction, dispersion, frost and rim light from the real backdrop.

`render_glass` turns the pixels behind a rounded shape into the glass body:

  1. blur + saturation boost of the backdrop (frosted vibrancy)
  2. a rounded-rect signed distance field gives every pixel its depth inside the shape and the
     outward surface normal
  3. within the bevel the surface curves like a convex lens: pixels sample the backdrop further
     outward along the normal, strongest at the rim, so content beyond the edge bends into view
  4. red / green / blue refract by slightly different amounts (chromatic dispersion)
  5. Fresnel brightening toward the rim, an adaptive frost tint, soft drop shadow

Specular highlights are not baked in; they're painted live by the widgets so the light can move.
"""

from dataclasses import dataclass

import numpy as np
from scipy.ndimage import gaussian_filter, map_coordinates
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QConicalGradient, QImage, QPainterPath, QPen, QBrush, QGuiApplication


@dataclass
class GlassStyle:
    blur: float = 3.0          # backdrop blur sigma (px); liquid glass stays fairly clear
    bevel: float = 18.0        # width of the curved rim (px)
    refraction: float = 1.25   # max displacement at the rim, as a fraction of the bevel
    dispersion: float = 0.16   # extra/less displacement for red/blue
    saturation: float = 1.55
    frost: float = 0.16        # how much the glass is tinted toward white (light) / smoke (dark)
    fresnel: float = 0.22
    shadow: float = 0.30
    shadow_blur: float = 9.0
    shadow_offset: float = 5.0


def qimage_to_rgb(img):
    """QImage -> float32 HxWx3 in 0..1."""
    img = img.convertToFormat(QImage.Format_RGBA8888)
    w, h = img.width(), img.height()
    arr = np.frombuffer(img.constBits(), np.uint8, count=img.bytesPerLine() * h)
    arr = arr.reshape(h, img.bytesPerLine())[:, :w * 4].reshape(h, w, 4)
    return arr[..., :3].astype(np.float32) / 255.0


def rgba_to_qimage(rgba):
    """Premultiplied float HxWx4 -> QImage (owns its memory)."""
    u8 = np.ascontiguousarray((np.clip(rgba, 0, 1) * 255 + 0.5).astype(np.uint8))
    h, w = u8.shape[:2]
    return QImage(u8.data, w, h, 4 * w, QImage.Format_RGBA8888_Premultiplied).copy()


def rounded_rect_sdf(h, w, rect, radius):
    """Signed distance (px) to a rounded rect; negative inside. rect = (x, y, w, h)."""
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32) + 0.5
    rx, ry, rw, rh = rect
    r = min(radius, rw / 2, rh / 2)
    qx = np.abs(xs - (rx + rw / 2)) - (rw / 2 - r)
    qy = np.abs(ys - (ry + rh / 2)) - (rh / 2 - r)
    outside = np.hypot(np.maximum(qx, 0), np.maximum(qy, 0))
    inside = np.minimum(np.maximum(qx, qy), 0)
    return outside + inside - r, xs, ys


def render_glass(backdrop, rect, radius, style=GlassStyle(), scale=1.0):
    """Render a glass shape over `backdrop` (float HxWx3, device pixels).

    rect/radius are in device pixels. Returns (QImage, (x0, y0), luminance) where the image covers
    the shape plus room for its shadow, placed at (x0, y0) in backdrop coordinates, and luminance is
    the mean brightness of the glass (to choose dark or light content on top).
    """
    s = scale
    bevel, blur = style.bevel * s, style.blur * s
    pad = int(bevel * style.refraction * (1 + style.dispersion) + style.shadow_blur * 3 * s
              + style.shadow_offset * s + 3 * blur + 4)
    H, W = backdrop.shape[:2]
    rx, ry, rw, rh = rect
    x0, y0 = max(0, int(rx) - pad), max(0, int(ry) - pad)
    x1, y1 = min(W, int(rx + rw) + pad + 1), min(H, int(ry + rh) + pad + 1)
    crop = backdrop[y0:y1, x0:x1]
    h, w = crop.shape[:2]

    # 1. frosted, vivid backdrop
    soft = gaussian_filter(crop, (blur, blur, 0), mode="nearest")
    lum = soft @ np.array([0.2126, 0.7152, 0.0722], np.float32)
    soft = np.clip(lum[..., None] + (soft - lum[..., None]) * style.saturation, 0, 1)

    # 2. shape geometry
    d, xs, ys = rounded_rect_sdf(h, w, (rx - x0, ry - y0, rw, rh), radius)
    gy, gx = np.gradient(d)
    norm = np.hypot(gx, gy) + 1e-6
    nx, ny = gx / norm, gy / norm
    t = np.clip(-d / bevel, 0, 1)  # 0 at the rim, 1 where the surface is flat

    # 3 + 4. lens refraction with dispersion
    disp = bevel * style.refraction * (1 - t) ** 2.4
    out = np.empty_like(soft)
    for c, k in enumerate((1 + style.dispersion, 1.0, 1 - style.dispersion)):
        coords = [ys - 0.5 + ny * disp * k, xs - 0.5 + nx * disp * k]
        out[..., c] = map_coordinates(soft[..., c], coords, order=1, mode="nearest")

    inside = d < 0
    mean_lum = float((out[inside] @ np.array([0.2126, 0.7152, 0.0722], np.float32)).mean()) if inside.any() else 0.5
    light = mean_lum > 0.55

    # 5. frost tint (adapts to what's behind), fresnel rim, faint top light
    f = style.frost
    if light:
        out = out * (1 - f) + f
    else:
        dark_f = min(0.9, f * 1.5)
        out = out * (1 - dark_f) + np.array([0.05, 0.05, 0.08], np.float32) * dark_f + 0.03
    out += (style.fresnel * (1 - t) ** 3)[..., None]
    top = np.clip(1 - (ys - (ry - y0)) / (rh * 0.55), 0, 1) * (0.05 if light else 0.06)
    out = np.clip(out + top[..., None], 0, 1)

    alpha = np.clip(0.5 - d, 0, 1)
    shadow = gaussian_filter(np.roll(alpha, int(style.shadow_offset * s), axis=0), style.shadow_blur * s)
    shadow *= style.shadow * (0.6 if light else 1.0)
    a = alpha + shadow * (1 - alpha)
    rgba = np.dstack([out * alpha[..., None], a])
    lum_after = float((out[inside] @ np.array([0.2126, 0.7152, 0.0722], np.float32)).mean()) if inside.any() else 0.5
    return rgba_to_qimage(rgba), (x0, y0), lum_after


def synthetic_backdrop(h, w, dark=True):
    """Stand-in when the screen can't be read (Wayland): a soft diagonal gradient."""
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    g = (xs / max(w, 1) * 0.6 + ys / max(h, 1) * 0.4)[..., None]
    a, b = (np.array([0.10, 0.11, 0.16]), np.array([0.20, 0.16, 0.30])) if dark else \
           (np.array([0.86, 0.88, 0.94]), np.array([0.96, 0.92, 0.98]))
    return (a * (1 - g) + b * g).astype(np.float32)


def grab_backdrop(x, y, w, h, dpr=1.0):
    """Screen pixels in a global logical rect as float HxWx3 (device px), or None if unavailable.

    On X11 with a compositor the grab doesn't include our own (override-redirect) overlay, so the
    glass can be refreshed live while it's showing."""
    screen = QGuiApplication.screenAt(QPointF(x + w / 2, y + h / 2).toPoint()) or QGuiApplication.primaryScreen()
    if screen is None or QGuiApplication.platformName() not in ("xcb",):
        return None
    geo = screen.geometry()
    pix = screen.grabWindow(0, x - geo.x(), y - geo.y(), w, h)
    if pix.isNull():
        return None
    img = pix.toImage()
    if img.width() != round(w * dpr) or img.height() != round(h * dpr):
        img = img.scaled(round(w * dpr), round(h * dpr), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    return qimage_to_rgb(img)


def paint_specular(p, rect, radius, angle, light, strength=1.0):
    """Live rim light: two opposed highlights (light source + its reflection) along the edge."""
    hi = 255 if not light else 255
    g = QConicalGradient(rect.center(), angle)
    peak = [(0.00, 1.0), (0.07, 0.35), (0.16, 0.0), (0.38, 0.0), (0.50, 0.55), (0.57, 0.18), (0.66, 0.0),
            (0.88, 0.0), (0.94, 0.35), (1.00, 1.0)]
    for pos, v in peak:
        g.setColorAt(pos, QColor(hi, hi, hi, int(255 * min(1.0, v * strength))))
    path = QPainterPath()
    path.addRoundedRect(rect.adjusted(0.6, 0.6, -0.6, -0.6), radius - 0.6, radius - 0.6)
    p.save()
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QBrush(g), 1.3))
    p.drawPath(path)
    soft = QPen(QBrush(g), 3.2)
    p.setOpacity(p.opacity() * 0.28)
    p.setPen(soft)
    inner = QPainterPath()
    inner.addRoundedRect(rect.adjusted(2.0, 2.0, -2.0, -2.0), radius - 2, radius - 2)
    p.drawPath(inner)
    p.restore()
    # constant hairline so the edge reads even where the highlight is off
    p.save()
    p.setPen(QPen(QColor(255, 255, 255, 70 if not light else 150), 0.8))
    p.setBrush(Qt.NoBrush)
    p.drawPath(path)
    p.restore()
