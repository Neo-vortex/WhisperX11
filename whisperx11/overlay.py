"""The dictation overlay: a liquid-glass pill at the pointer with a live caption capsule below it."""

import math
import time
from collections import deque

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (QBrush, QColor, QConicalGradient, QCursor, QFont, QFontMetricsF, QGuiApplication,
                           QLinearGradient,
                           QPainter, QPainterPath, QPen, QRadialGradient)
from PySide6.QtWidgets import QWidget

from .glass import GlassStyle, grab_backdrop, paint_specular, render_glass, synthetic_backdrop

# Apple Intelligence glow colours
SIRI = ["#FF9F0A", "#FF375F", "#BF5AF2", "#5E5CE6", "#0A84FF", "#64D2FF", "#FF9F0A"]


def _font(size, weight=QFont.Normal, families=("Inter", "SF Pro Text", "Vazirmatn", "Noto Sans Arabic")):
    f = QFont()
    f.setFamilies(list(families))
    f.setStyleHint(QFont.SansSerif)
    f.setPointSizeF(size)
    f.setWeight(QFont.Weight(weight) if isinstance(weight, int) else weight)
    return f


def _is_rtl(text):
    strong = next((c for c in text if c.isalpha()), "a")
    return "֐" <= strong <= "ࣿ"


class Overlay(QWidget):
    W, H = 640, 200            # window: room for glow, shadow and the caption
    PX, PY = 44, 36            # pill position inside the window
    PW, PH = 330, 60           # pill size
    CAPTION_MAX = 560
    BARS = 22
    LABELS = {"listening": "Listening", "thinking": "Thinking", "done": "Inserted", "empty": "No speech"}

    def __init__(self, style=GlassStyle()):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.X11BypassWindowManagerHint | Qt.WindowDoesNotAcceptFocus
                         | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.resize(self.W, self.H)
        self.style = style
        self.mode = "listening"
        self.subtitle = ""
        self.caption = ""
        self.level = self.shown_level = 0.0
        self.history = deque([0.0] * self.BARS, maxlen=self.BARS)
        self.opacity = self.target_opacity = 0.0
        self.flash = 0.0
        self.t0 = time.monotonic()
        self.frame = 0
        self.static_backdrop = None   # set for offscreen rendering (README media)
        self.backdrop = None
        self.pill_glass = self.caption_glass = None   # (QImage, (x, y), luminance)
        self.caption_box = None
        self.label_font = _font(11.0, QFont.DemiBold)
        self.sub_font = _font(8.6, QFont.Medium)
        self.caption_font = _font(11.0)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._animate)
        self.timer.start(16)
        self.refresh = QTimer(self)
        self.refresh.timeout.connect(self._refresh_backdrop)

    # --- public state ------------------------------------------------------------
    def popup(self, at_cursor=True):
        g = QGuiApplication.primaryScreen().geometry()
        if at_cursor:
            pos = QCursor.pos()
            g = (QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()).geometry()
            px = min(max(pos.x() + 18, g.left() + 8), g.right() - self.PW - 8)
            py = min(max(pos.y() + 22, g.top() + 8), g.bottom() - self.PH - 70)
        else:  # Wayland: the pointer position isn't available, use the bottom centre
            px, py = g.center().x() - self.PW // 2, g.bottom() - self.PH - 120
        self.move(px - self.PX, py - self.PY)
        self.history = deque([0.0] * self.BARS, maxlen=self.BARS)
        self.level = self.shown_level = self.flash = 0.0
        self.caption = ""
        self.caption_glass = self.caption_box = None
        self.mode = "listening"
        self.target_opacity = 1.0
        self.backdrop = None
        self._refresh_backdrop()   # grab before showing, so the first frame already has glass
        self.show()
        self.raise_()
        self.refresh.start(220)

    def set_mode(self, mode):
        self.mode = mode
        if mode in ("done", "empty"):
            self.flash = 1.0 if mode == "done" else 0.0
            QTimer.singleShot(650 if mode == "done" else 450, self.dismiss)

    def set_caption(self, text):
        self.caption = text
        self._update_caption_glass()

    def dismiss(self):
        self.target_opacity = 0.0

    # --- glass --------------------------------------------------------------------
    def _dpr(self):
        return self.devicePixelRatioF() if self.isVisible() else (
            QGuiApplication.primaryScreen().devicePixelRatio() if QGuiApplication.primaryScreen() else 1.0)

    def _refresh_backdrop(self):
        dpr = self._dpr()
        if self.static_backdrop is not None:
            bd = self.static_backdrop
        else:
            bd = grab_backdrop(self.x(), self.y(), self.W, self.H, dpr)
            if bd is None:
                bd = synthetic_backdrop(round(self.H * dpr), round(self.W * dpr))
        if self.backdrop is not None and self.backdrop.shape == bd.shape and \
                np.abs(self.backdrop[::4, ::4] - bd[::4, ::4]).mean() < 0.002:
            return  # nothing behind us changed
        self.backdrop = bd
        self.pill_glass = render_glass(bd, (self.PX * dpr, self.PY * dpr, self.PW * dpr, self.PH * dpr),
                                       self.PH / 2 * dpr, self.style, dpr)
        self.caption_box = None
        self._update_caption_glass()

    def _caption_rect(self):
        if not self.caption:
            return None
        fm = QFontMetricsF(self.caption_font)
        text = fm.elidedText(self.caption, Qt.ElideLeft, self.CAPTION_MAX - 36)
        w = min(self.CAPTION_MAX, fm.horizontalAdvance(text) + 36)
        w = max(80.0, math.ceil(w / 24) * 24)  # quantize, so the glass isn't re-rendered for every letter
        return QRectF(self.PX, self.PY + self.PH + 12, w, fm.height() + 20), text

    def _update_caption_glass(self):
        r = self._caption_rect()
        if r is None:
            self.caption_glass = self.caption_box = None
            return
        rect, _ = r
        if self.caption_box == rect and self.caption_glass is not None:
            return
        self.caption_box = rect
        if self.backdrop is None:
            return
        dpr = self._dpr()
        self.caption_glass = render_glass(self.backdrop, (rect.x() * dpr, rect.y() * dpr, rect.width() * dpr,
                                                          rect.height() * dpr), rect.height() / 2 * dpr,
                                          self.style, dpr)

    # --- animation ----------------------------------------------------------------
    def _animate(self):
        if not self.isVisible():
            return
        self.frame += 1
        self.opacity += (self.target_opacity - self.opacity) * (0.2 if self.target_opacity else 0.15)
        if self.target_opacity == 0 and self.opacity < 0.02:
            self.opacity = 0.0
            self.refresh.stop()
            self.hide()
            return
        k = 0.45 if self.level > self.shown_level else 0.10  # fast attack, slow release
        self.shown_level += (self.level - self.shown_level) * k
        self.flash *= 0.93
        if self.frame % 3 == 0 and self.mode == "listening":
            self.history.append(self.shown_level)
        self.update()

    # --- painting -----------------------------------------------------------------
    def paintEvent(self, _):
        t = time.monotonic() - self.t0
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        pill = QRectF(self.PX, self.PY, self.PW, self.PH)
        s = 0.9 + 0.1 * self.opacity  # gentle grow-in
        p.translate(pill.center())
        p.scale(s, s)
        p.translate(-pill.center())
        p.setOpacity(self.opacity)
        dpr = self._dpr()
        thinking = self.mode == "thinking"
        light = self.pill_glass[2] > 0.55 if self.pill_glass else False

        self._paint_glow(p, pill, t, thinking)
        if self.pill_glass:
            img, (x, y), _ = self.pill_glass
            p.drawImage(QRectF(x / dpr, y / dpr, img.width() / dpr, img.height() / dpr), img)
        angle = 120 + 25 * math.sin(t * 0.7) + 40 * self.shown_level
        paint_specular(p, pill, self.PH / 2, angle, light, 0.8 + 0.4 * self.flash)

        ink = QColor(29, 29, 31) if light else QColor(245, 245, 247)
        self._paint_orb(p, QPointF(pill.left() + 32, pill.center().y()), t, thinking)
        self._paint_wave(p, QRectF(pill.left() + 60, pill.top() + 14, 132, pill.height() - 28), t, thinking, ink)
        self._paint_label(p, QRectF(pill.left() + 206, pill.top() + 10, pill.width() - 220, pill.height() - 20), ink, t)

        if self.caption and self.caption_box is not None:
            self._paint_caption(p, t, dpr)
        p.end()

    def _paint_glow(self, p, pill, t, thinking):
        """Apple Intelligence style glow around the glass, breathing with the voice."""
        if self.mode in ("listening", "thinking"):
            glow = (0.55 + 0.2 * math.sin(t * 5)) if thinking else (0.25 + 0.85 * self.shown_level)
        else:
            glow = 0.0
        glow = min(1.3, glow + self.flash)
        if glow <= 0.01:
            return
        g = QConicalGradient(pill.center(), -(t * (160 if thinking else 45)) % 360)
        for i, c in enumerate(SIRI):
            g.setColorAt(i / (len(SIRI) - 1), QColor(c))
        base = p.opacity()
        p.setBrush(Qt.NoBrush)
        for i in range(16, 0, -1):
            p.setOpacity(base * min(1.0, glow * 0.06 * (1.1 - i / 16)))
            p.setPen(QPen(QBrush(g), i * 2.6))
            p.drawRoundedRect(pill, self.PH / 2, self.PH / 2)
        p.setOpacity(base)

    def _paint_orb(self, p, c, t, thinking):
        r = 12.5 + 3.0 * self.shown_level + 0.8 * math.sin(t * 2.4)
        path = QPainterPath()
        path.addEllipse(c, r, r)
        p.save()
        p.setClipPath(path)
        p.fillPath(path, QColor("#1C1C3A"))
        spin = t * (3.2 if thinking else 1.1)
        for i, col in enumerate(["#FF375F", "#BF5AF2", "#0A84FF", "#64D2FF", "#FF9F0A"]):
            a = spin + i * 2 * math.pi / 5
            bc = QPointF(c.x() + math.cos(a) * r * 0.45, c.y() + math.sin(a * 1.3) * r * 0.45)
            g = QRadialGradient(bc, r * 0.95)
            col = QColor(col)
            col.setAlpha(210)
            g.setColorAt(0, col)
            col.setAlpha(0)
            g.setColorAt(1, col)
            p.setPen(Qt.NoPen)
            p.setBrush(g)
            p.drawEllipse(bc, r * 0.95, r * 0.95)
        shine = QRadialGradient(QPointF(c.x() - r * 0.35, c.y() - r * 0.45), r * 0.7)
        shine.setColorAt(0, QColor(255, 255, 255, 200))
        shine.setColorAt(1, QColor(255, 255, 255, 0))
        p.setBrush(shine)
        p.drawEllipse(c, r, r)
        p.restore()
        p.setPen(QPen(QColor(255, 255, 255, 110), 0.8))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(c, r, r)

    def _paint_wave(self, p, area, t, thinking, ink):
        n, gap = self.BARS, 2.4
        w = (area.width() - gap * (n - 1)) / n
        mid = area.center().y()
        p.setPen(Qt.NoPen)
        for i in range(n):
            if thinking:
                h = 3 + 12 * (0.5 + 0.5 * math.sin(t * 7 - i * 0.45)) ** 2
            else:
                center = 0.5 + 0.5 * math.sin(math.pi * (i + 0.5) / n)
                h = 3 + self.history[i] * area.height() * center + 1.0 * (1 + math.sin(t * 4 + i * 0.7))
            h = min(h, area.height())
            col = QColor(ink)
            col.setAlphaF(0.55 + 0.4 * min(1.0, h / area.height() * 1.6))
            p.setBrush(col)
            p.drawRoundedRect(QRectF(area.left() + i * (w + gap), mid - h / 2, w, h), w / 2, w / 2)

    def _paint_label(self, p, area, ink, t):
        label = self.LABELS.get(self.mode, "")
        p.setFont(self.label_font)
        shimmer = QLinearGradient(QPointF(area.left() + ((t * 0.8) % 1.8 - 0.4) * area.width() - 30, 0),
                                  QPointF(area.left() + ((t * 0.8) % 1.8 - 0.4) * area.width() + 30, 0))
        dim = QColor(ink)
        dim.setAlphaF(0.78)
        shimmer.setColorAt(0, dim)
        shimmer.setColorAt(0.5, ink)
        shimmer.setColorAt(1, dim)
        p.setPen(QPen(QBrush(shimmer), 1))
        top = QRectF(area.left(), area.top(), area.width(), area.height() * 0.58)
        p.drawText(top, Qt.AlignLeft | Qt.AlignBottom, label)
        if self.subtitle:
            sub = QColor(ink)
            sub.setAlphaF(0.6)
            p.setPen(sub)
            p.setFont(self.sub_font)
            bottom = QRectF(area.left(), area.top() + area.height() * 0.6, area.width(), area.height() * 0.4)
            p.drawText(bottom, Qt.AlignLeft | Qt.AlignTop, self.subtitle)

    def _paint_caption(self, p, t, dpr):
        rect, text = self._caption_rect()
        box = self.caption_box
        light = self.caption_glass[2] > 0.55 if self.caption_glass else False
        if self.caption_glass:
            img, (x, y), _ = self.caption_glass
            p.drawImage(QRectF(x / dpr, y / dpr, img.width() / dpr, img.height() / dpr), img)
        paint_specular(p, box, box.height() / 2, 120 + 25 * math.sin(t * 0.7), light, 0.6)
        p.setFont(self.caption_font)
        p.setPen(QColor(29, 29, 31) if light else QColor(245, 245, 247))
        p.setLayoutDirection(Qt.RightToLeft if _is_rtl(self.caption) else Qt.LeftToRight)
        p.drawText(box.adjusted(18, 0, -18, 0), Qt.AlignVCenter | Qt.AlignAbsolute | Qt.AlignLeft, text)
        p.setLayoutDirection(Qt.LeftToRight)
