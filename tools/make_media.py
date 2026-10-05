#!/usr/bin/env python3
"""Render README media from the real widgets: assets/demo.gif, assets/states.png, assets/panel.png.

Run headless:  QT_QPA_PLATFORM=offscreen .venv/bin/python tools/make_media.py
The desktop, editor and timings are a mock-up; the overlay, glass and panel are the app's own code.
"""

import math
import os
import sys
import time

from PIL import Image
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPolygonF, \
    QRadialGradient
from PySide6.QtWidgets import QApplication

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
app = QApplication(sys.argv)

import whisperx11.panel as panel_mod  # noqa: E402
from whisperx11.config import Settings  # noqa: E402
from whisperx11.glass import qimage_to_rgb  # noqa: E402
from whisperx11.overlay import Overlay  # noqa: E402

ASSETS = os.path.join(ROOT, "assets")
os.makedirs(ASSETS, exist_ok=True)
S = 2                      # render at 2x, downscale for output
W, H = 900, 430
FPS = 25
FA = "سلام! جلسه فردا ساعت ده برگزار می‌شود."
EN = "Let's ship the new release on Friday."


def font(size, weight=QFont.Normal, families=("Inter", "Noto Sans Arabic", "DejaVu Sans")):
    f = QFont()
    f.setFamilies(list(families))
    f.setPointSizeF(size)
    f.setWeight(weight)
    return f


def new_image(w, h):
    img = QImage(w * S, h * S, QImage.Format_ARGB32_Premultiplied)
    img.setDevicePixelRatio(S)
    img.fill(Qt.transparent)
    return img


def to_pil(img, w, h):
    img = img.convertToFormat(QImage.Format_RGBA8888)
    pil = Image.frombuffer("RGBA", (img.width(), img.height()), bytes(img.constBits()), "raw", "RGBA", 0, 1)
    return pil.resize((w, h), Image.LANCZOS).convert("RGB")


def wallpaper(p, w, h, dark=False):
    g = QLinearGradient(0, 0, w, h)
    stops = (["#0f2027", "#2c5364", "#5b2a86"] if dark else ["#ffd6a5", "#fdafc8", "#a0c4ff"])
    for i, c in enumerate(stops):
        g.setColorAt(i / 2, QColor(c))
    p.fillRect(QRectF(0, 0, w, h), g)
    for (x, y, r, c) in ((120, 360, 150, "#ff375f"), (760, 60, 170, "#0a84ff"), (520, 400, 120, "#ffd60a"),
                         (860, 330, 110, "#30d158")):
        rg = QRadialGradient(QPointF(x, y), r)
        col = QColor(c)
        col.setAlpha(170)
        rg.setColorAt(0, col)
        col.setAlpha(0)
        rg.setColorAt(1, col)
        p.setPen(Qt.NoPen)
        p.setBrush(rg)
        p.drawEllipse(QPointF(x, y), r, r)


def editor(p, lines, caret_on):
    win = QRectF(40, 28, W - 80, H - 70)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(0, 0, 0, 50))
    p.drawRoundedRect(win.translated(0, 6), 14, 14)
    p.setBrush(QColor("#fbfbfd"))
    p.drawRoundedRect(win, 14, 14)
    bar = QPainterPath()
    bar.addRoundedRect(QRectF(win.left(), win.top(), win.width(), 40), 14, 14)
    p.setBrush(QColor("#ececf1"))
    p.drawPath(bar)
    p.fillRect(QRectF(win.left(), win.top() + 26, win.width(), 14), QColor("#ececf1"))
    for i, c in enumerate(["#ff5f57", "#febc2e", "#28c840"]):
        p.setBrush(QColor(c))
        p.drawEllipse(QPointF(win.left() + 22 + i * 20, win.top() + 20), 6, 6)
    p.setFont(font(9.5, QFont.Medium))
    p.setPen(QColor("#6e6e73"))
    p.drawText(QRectF(win.left(), win.top(), win.width(), 40), Qt.AlignCenter, "notes.txt")
    p.setFont(font(12.5))
    y = win.top() + 76
    caret = None
    for i, text in enumerate(lines):
        r = QRectF(win.left() + 34, y - 15, win.width() - 68, 26)
        rtl = any("؀" <= ch <= "ۿ" for ch in text)
        p.setPen(QColor("#5e5ce6") if i == 0 else QColor("#1d1d1f"))
        p.setLayoutDirection(Qt.RightToLeft if rtl else Qt.LeftToRight)
        p.drawText(r, Qt.AlignVCenter | Qt.AlignAbsolute | (Qt.AlignRight if rtl else Qt.AlignLeft), text)
        p.setLayoutDirection(Qt.LeftToRight)
        adv = p.fontMetrics().horizontalAdvance(text)
        caret = (r.right() - adv - 3, y) if rtl else (r.left() + adv + 2, y)
        y += 32
    if caret_on and caret:
        p.fillRect(QRectF(caret[0], caret[1] - 11, 1.6, 21), QColor("#0a84ff"))


def pointer(p, x, y):
    poly = QPolygonF([QPointF(x, y), QPointF(x, y + 18), QPointF(x + 4.5, y + 14), QPointF(x + 8, y + 21),
                      QPointF(x + 11, y + 19.5), QPointF(x + 7.5, y + 13), QPointF(x + 13, y + 13)])
    p.setPen(QPen(QColor("#111"), 1.2))
    p.setBrush(QColor("#fff"))
    p.drawPolygon(poly)


def make_overlay():
    o = Overlay()
    o._dpr = lambda: float(S)
    o.opacity = o.target_opacity = 1.0
    return o


def overlay_frame(o, scene, ox, oy, sim_t):
    """Render the overlay at window position (ox, oy) over `scene` (2x QImage of the full frame)."""
    crop = scene.copy(int(ox * S), int(oy * S), o.W * S, o.H * S)
    o.static_backdrop = qimage_to_rgb(crop)
    o._refresh_backdrop()
    if o.caption:
        o._update_caption_glass()
    o.t0 = time.monotonic() - sim_t
    img = new_image(o.W, o.H)
    o.render(img)
    return img


def speech(t):
    return min(1.0, max(0.0, math.sin(t * 9.0)) * (0.55 + 0.45 * math.sin(t * 2.3 + 1)) +
               0.25 * max(0.0, math.sin(t * 23)))


def grow(sentence, k):
    return " ".join(sentence.split()[:k])


def demo_gif():
    o = make_overlay()
    o.subtitle = "Toggle · EN / FA"
    px, py = 250, 168
    ox, oy = px + 18 - o.PX, py + 22 - o.PY
    fa_words, en_words = FA.split(), EN.split()
    # (time, words of FA typed, words of EN typed, caption)
    plan = [(1.2, 2, 0, grow(FA, 3)), (1.8, 4, 0, grow(FA, 5)), (2.4, 5, 0, FA), (2.75, len(FA.split()), 0, FA),
            (3.9, len(FA.split()), 2, grow(EN, 3)), (4.5, len(FA.split()), 5, grow(EN, 6)),
            (5.0, len(FA.split()), 6, EN)]
    frames, sim_t = [], 0.0
    phases = [("idle", 0.6), ("listening", 5.3), ("thinking", 0.35), ("done", 1.0), ("after", 1.2)]
    fa_n = en_n = 0
    for phase, dur in phases:
        for f in range(int(dur * FPS)):
            sim_t += 1 / FPS
            local = f / FPS
            if phase == "idle":
                o.opacity = 0.0
            elif phase == "listening":
                o.mode = "listening"
                o.opacity = min(1.0, local / 0.2)
                talking = local < 2.3 or 3.2 <= local < 5.0
                o.level = speech(local) if talking else 0.02
                k = 0.45 if o.level > o.shown_level else 0.10
                o.shown_level += (o.level - o.shown_level) * k
                if f % 2 == 0:
                    o.history.append(o.shown_level)
                for t_, fa_k, en_k, cap in plan:
                    if abs(local - t_) < 0.5 / FPS:
                        if fa_k > fa_n or en_k > en_n:
                            o.flash = 0.35
                        fa_n, en_n = fa_k, en_k
                        o.caption = cap
            elif phase == "thinking":
                o.mode = "thinking"
                o.shown_level *= 0.8
            elif phase == "done":
                if f == 0:
                    o.mode, o.flash, en_n = "done", 1.0, len(en_words)
                o.flash *= 0.9
                o.opacity = 1.0 if local < 0.6 else max(0.0, 1 - (local - 0.6) / 0.3)
            lines = ["Meeting notes", "", " ".join(fa_words[:fa_n]), " ".join(en_words[:en_n])]
            scene = new_image(W, H)
            p = QPainter(scene)
            p.setRenderHint(QPainter.Antialiasing)
            p.setRenderHint(QPainter.TextAntialiasing)
            wallpaper(p, W, H)
            editor(p, lines, int(sim_t * 2) % 2 == 0)
            pointer(p, px, py)
            p.end()
            if o.opacity > 0.01:
                ov = overlay_frame(o, scene, ox, oy, sim_t)
                p = QPainter(scene)
                p.drawImage(QPointF(ox, oy), ov)
                p.end()
            frames.append(to_pil(scene, W, H))
    pal = [fr.quantize(colors=255, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE) for fr in frames]
    out = os.path.join(ASSETS, "demo.gif")
    pal[0].save(out, save_all=True, append_images=pal[1:], duration=int(1000 / FPS), loop=0, optimize=True)
    print("wrote", out, f"{os.path.getsize(out) / 1e6:.1f} MB")


def states_png():
    states = [("listening", 0.75, 0.0, 1.3, "Let's ship the new release", False),
              ("thinking", 0.0, 0.0, 2.1, FA, True),
              ("done", 0.0, 0.7, 1.0, "", False)]
    cw, ch = 470, 168
    sheet = new_image(cw, ch * len(states))
    p = QPainter(sheet)
    for i, (mode, level, flash, t, caption, dark) in enumerate(states):
        o = make_overlay()
        o.subtitle = "Push to talk · EN / FA" if mode == "thinking" else "Toggle · EN / FA"
        o.mode, o.shown_level, o.flash, o.caption = mode, level, flash, caption
        o.history.clear()
        o.history.extend([abs(math.sin(j * 0.6)) * 0.9 * (0.4 + 0.6 * ((j * 7) % 5) / 5) for j in range(o.BARS)]
                         if mode != "thinking" else [0] * o.BARS)
        bg = new_image(o.W, o.H)
        q = QPainter(bg)
        wallpaper(q, o.W, o.H, dark)
        q.setFont(font(13))
        q.setPen(QColor("#f5f5f7") if dark else QColor("#1d1d1f"))
        for j, line in enumerate(["The quick brown fox jumps over the lazy dog", "سلام! این یک متن آزمایشی است",
                                  "Liquid glass bends what is behind it"]):
            q.drawText(QPointF(40, 50 + j * 34), line)
        q.end()
        ov = overlay_frame(o, bg, 0, 0, t)
        q = QPainter(bg)
        q.drawImage(QPointF(0, 0), ov)
        q.end()
        p.drawImage(QRectF(0, i * ch, cw, ch), bg, QRectF(10 * S, 10 * S, cw * S, ch * S))
    p.end()
    out = os.path.join(ASSETS, "states.png")
    sheet.save(out)
    print("wrote", out)


def panel_png():
    s = Settings()
    s.values.update({"mode": "ptt", "task": "transcribe", "languages": "en,fa", "insert": "type"})
    pn = panel_mod.SettingsPanel(s, "<ctrl>+<alt>+<space>", lambda cb: None, lambda: None,
                                 lambda: "turbo on AMD Radeon RX 6700 XT")
    pn.devicePixelRatioF = lambda: float(S)
    margin = 36
    bg = new_image(pn.W + 2 * margin, pn.H + 2 * margin)
    q = QPainter(bg)
    wallpaper(q, pn.W + 2 * margin, pn.H + 2 * margin, dark=True)
    q.end()
    crop = bg.copy(margin * S, margin * S, pn.W * S, pn.H * S)
    panel_mod.grab_backdrop = lambda *a: qimage_to_rgb(crop)
    pn.sync()
    pn._refresh_backdrop()
    img = new_image(pn.W, pn.H)
    pn.render(img)
    q = QPainter(bg)
    q.drawImage(QPointF(margin, margin), img)
    q.end()
    out = os.path.join(ASSETS, "panel.png")
    bg.save(out)
    print("wrote", out)


if __name__ == "__main__":
    panel_png()
    states_png()
    demo_gif()
