#!/usr/bin/env python3
"""Render README media (assets/demo.gif, assets/states.png) from the real Overlay widget.

Run headless:  QT_QPA_PLATFORM=offscreen .venv/bin/python tools/make_media.py
"""

import math
import os
import sys
import time

from PIL import Image
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QApplication

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
app = QApplication(sys.argv)
import dictate  # noqa: E402

ASSETS = os.path.join(ROOT, "assets")
os.makedirs(ASSETS, exist_ok=True)

SCALE = 2
W, H = 760, 330
FPS = 25
TYPED = ["Meeting notes", "", "سلام! جلسه فردا ساعت ده برگزار می‌شود.", "Let's ship the new release on Friday."]


def qimage_to_pil(img):
    img = img.convertToFormat(QImage.Format_RGBA8888)
    return Image.frombuffer("RGBA", (img.width(), img.height()), bytes(img.constBits()), "raw", "RGBA", 0, 1)


def overlay_image(o, sim_t):
    o.t0 = time.monotonic() - sim_t
    img = QImage(o.W * SCALE, o.H * SCALE, QImage.Format_ARGB32_Premultiplied)
    img.setDevicePixelRatio(SCALE)
    img.fill(Qt.transparent)
    o.render(img)
    return img


def draw_editor(p, lines, caret_on, line_count):
    p.fillRect(QRectF(0, 0, W, H), QColor("#0f1117"))
    win = QRectF(28, 24, W - 56, H - 48)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#171a22"))
    p.drawRoundedRect(win, 12, 12)
    p.setBrush(QColor("#1d2029"))
    path = QPainterPath()
    path.addRoundedRect(QRectF(win.left(), win.top(), win.width(), 38), 12, 12)
    p.drawPath(path)
    p.fillRect(QRectF(win.left(), win.top() + 26, win.width(), 12), QColor("#1d2029"))
    for i, c in enumerate(["#ff5f57", "#febc2e", "#28c840"]):
        p.setBrush(QColor(c))
        p.drawEllipse(QPointF(win.left() + 22 + i * 20, win.top() + 19), 6, 6)
    font = QFont("Inter")
    font.setStyleHint(QFont.SansSerif)
    font.setPointSizeF(10)
    p.setFont(font)
    p.setPen(QColor("#8b90a0"))
    p.drawText(QRectF(win.left(), win.top(), win.width(), 38), Qt.AlignCenter, "notes.txt — Mousepad")

    body = QFont("Inter")
    body.setFamilies(["Inter", "Noto Sans Arabic", "DejaVu Sans"])
    body.setPointSizeF(12.5)
    p.setFont(body)
    y = win.top() + 64
    caret = None
    for i in range(line_count):
        text = lines[i] if i < len(lines) else ""
        p.setPen(QColor("#4b5163"))
        p.drawText(QRectF(win.left() + 14, y - 16, 24, 24), Qt.AlignRight | Qt.AlignVCenter, str(i + 1))
        p.setPen(QColor("#e6e8ef") if i else QColor("#c4b5fd"))
        r = QRectF(win.left() + 52, y - 16, win.width() - 80, 24)
        rtl = any("\u0600" <= ch <= "\u06ff" for ch in text)
        p.drawText(r, (Qt.AlignRight if rtl else Qt.AlignLeft) | Qt.AlignVCenter, text)
        caret = (r.left() + p.fontMetrics().horizontalAdvance(text) + 2, y)
        y += 30
    if caret_on and caret:
        p.fillRect(QRectF(caret[0], caret[1] - 12, 2, 20), QColor("#e6e8ef"))
    return caret


def draw_pointer(p, x, y):
    poly = QPolygonF([QPointF(x, y), QPointF(x, y + 18), QPointF(x + 4.5, y + 14), QPointF(x + 8, y + 21),
                      QPointF(x + 11, y + 19.5), QPointF(x + 7.5, y + 13), QPointF(x + 13, y + 13)])
    p.setPen(QPen(QColor("#111"), 1.2))
    p.setBrush(QColor("#fff"))
    p.drawPolygon(poly)


def speech_envelope(t):
    """Fake syllable-like loudness for the demo."""
    if t < 0.25:
        return 0.05
    s = max(0.0, math.sin(t * 9.0)) * (0.55 + 0.45 * math.sin(t * 2.3 + 1)) + 0.25 * max(0.0, math.sin(t * 23))
    return min(1.0, s)


def demo_gif():
    o = dictate.Overlay()
    frames = []
    pointer = (300, 196)
    timeline = [("idle", 0.8), ("listening", 2.6), ("thinking", 0.9), ("done", 1.0), ("after", 1.4)]
    sim_t = 0.0
    lines = TYPED[:2]
    for phase, dur in timeline:
        n = int(dur * FPS)
        for f in range(n):
            sim_t += 1 / FPS
            local = f / FPS
            if phase == "idle":
                o.opacity = 0.0
            elif phase == "listening":
                o.mode = "listening"
                o.opacity = min(1.0, local / 0.18)
                o.level = speech_envelope(local) if local < 2.1 else 0.03
                k = 0.45 if o.level > o.shown_level else 0.12
                o.shown_level += (o.level - o.shown_level) * k
                if f % 2 == 0:
                    o.history.append(o.shown_level)
            elif phase == "thinking":
                o.mode = "thinking"
                o.shown_level *= 0.8
            elif phase == "done":
                if f == 0:
                    o.mode, o.flash = "done", 1.0
                    lines = TYPED
                o.flash *= 0.9
                o.opacity = 1.0 if local < 0.55 else max(0.0, 1 - (local - 0.55) / 0.3)

            img = QImage(W * SCALE, H * SCALE, QImage.Format_ARGB32_Premultiplied)
            img.setDevicePixelRatio(SCALE)
            p = QPainter(img)
            p.setRenderHint(QPainter.Antialiasing)
            p.setRenderHint(QPainter.TextAntialiasing)
            draw_editor(p, lines, int(sim_t * 2) % 2 == 0, 4)
            draw_pointer(p, *pointer)
            if o.opacity > 0.01:
                ov = overlay_image(o, sim_t)
                px = pointer[0] + 18 - (o.W - o.PW) // 2
                py = pointer[1] + 22 - (o.H - o.PH) // 2
                p.drawImage(QPointF(px, py), ov)
            p.end()
            frames.append(qimage_to_pil(img).resize((W, H), Image.LANCZOS).convert("RGB"))

    pal = [f.quantize(colors=255, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE) for f in frames]
    out = os.path.join(ASSETS, "demo.gif")
    pal[0].save(out, save_all=True, append_images=pal[1:], duration=int(1000 / FPS), loop=0, optimize=True)
    print("wrote", out, f"{os.path.getsize(out) / 1e6:.1f} MB")


def states_png():
    o = dictate.Overlay()
    o.opacity = 1.0
    hist = [abs(math.sin(i * 0.6)) * 0.9 * (0.4 + 0.6 * ((i * 7) % 5) / 5) for i in range(o.BARS)]
    cell_w, cell_h = o.W, o.H
    sheet = QImage(cell_w * 3 * SCALE, cell_h * SCALE, QImage.Format_ARGB32_Premultiplied)
    sheet.setDevicePixelRatio(SCALE)
    sheet.fill(QColor("#0f1117"))
    p = QPainter(sheet)
    for i, (mode, level, flash, t) in enumerate([("listening", 0.7, 0, 1.3), ("thinking", 0, 0, 2.1),
                                                 ("done", 0, 0.7, 1.0)]):
        o.mode, o.shown_level, o.flash = mode, level, flash
        o.history.clear()
        o.history.extend(hist if mode != "thinking" else [0] * o.BARS)
        p.drawImage(QPointF(i * cell_w, 0), overlay_image(o, t))
    p.end()
    out = os.path.join(ASSETS, "states.png")
    sheet.save(out)
    print("wrote", out)


if __name__ == "__main__":
    states_png()
    demo_gif()
