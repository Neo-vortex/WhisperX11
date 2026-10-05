"""Settings: a liquid-glass panel with Apple-style controls, plus the tray icon and its menu."""

import math
import time

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QAction, QActionGroup, QColor, QCursor, QFontMetricsF, QGuiApplication, QIcon,
                           QImage, QPainter, QPainterPath, QPen, QPixmap, QRadialGradient)
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QToolTip, QWidget

from .config import TRANSLATE_HINT, translate_available
from .glass import GlassStyle, grab_backdrop, paint_specular, render_glass, synthetic_backdrop
from .hotkeys import combo_label, key_label
from .overlay import SIRI, _font

LANG_OPTIONS = [("en,fa", "EN + FA"), ("en", "English"), ("fa", "Persian")]


def _ink(light, alpha=1.0):
    c = QColor(29, 29, 31) if light else QColor(245, 245, 247)
    c.setAlphaF(alpha)
    return c


class _Control(QWidget):
    def light(self):
        return getattr(self.window(), "light", False)


class Segmented(_Control):
    """Segmented control with a sliding glass thumb. Options: [(key, label)]."""
    changed = Signal(str)

    def __init__(self, options, value, parent=None):
        super().__init__(parent)
        self.options = options
        self.value = value
        self.disabled = {}  # key -> tooltip
        self.thumb_x = None
        self.setFixedHeight(32)
        self.setMouseTracking(True)
        self.anim = QTimer(self)
        self.anim.timeout.connect(self._step)

    def set_value(self, value):
        self.value = value
        self.anim.start(16)

    def set_disabled(self, key, tooltip=None):
        if tooltip:
            self.disabled[key] = tooltip
        else:
            self.disabled.pop(key, None)
        self.update()

    def _seg_rect(self, i):
        w = (self.width() - 4) / len(self.options)
        return QRectF(2 + i * w, 2, w, self.height() - 4)

    def _index(self, key):
        return next((i for i, (k, _) in enumerate(self.options) if k == key), 0)

    def _step(self):
        target = self._seg_rect(self._index(self.value)).x()
        self.thumb_x = target if self.thumb_x is None else self.thumb_x + (target - self.thumb_x) * 0.3
        if abs(self.thumb_x - target) < 0.3:
            self.thumb_x = target
            self.anim.stop()
        self.update()

    def mousePressEvent(self, e):
        i = min(len(self.options) - 1, int((e.position().x() - 2) / ((self.width() - 4) / len(self.options))))
        key = self.options[i][0]
        if key in self.disabled:
            QToolTip.showText(e.globalPosition().toPoint(), self.disabled[key], self)
            return
        if key != self.value:
            self.set_value(key)
            self.changed.emit(key)

    def event(self, e):
        if e.type() == QEvent.ToolTip:
            w = (self.width() - 4) / len(self.options)
            i = min(len(self.options) - 1, max(0, int((e.pos().x() - 2) / w)))
            tip = self.disabled.get(self.options[i][0])
            if tip:
                QToolTip.showText(e.globalPos(), tip, self)
            else:
                QToolTip.hideText()
            return True
        return super().event(e)

    def resizeEvent(self, e):
        self.thumb_x = self._seg_rect(self._index(self.value)).x()

    def paintEvent(self, _):
        light = self.light()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        track = QRectF(self.rect())
        p.setPen(Qt.NoPen)
        p.setBrush(_ink(light, 0.07 if light else 0.10))
        p.drawRoundedRect(track, track.height() / 2, track.height() / 2)
        if self.thumb_x is None:
            self.thumb_x = self._seg_rect(self._index(self.value)).x()
        seg = self._seg_rect(0)
        thumb = QRectF(self.thumb_x, seg.y(), seg.width(), seg.height())
        p.setBrush(QColor(255, 255, 255, 235 if light else 62))
        p.drawRoundedRect(thumb, thumb.height() / 2, thumb.height() / 2)
        p.setPen(QPen(QColor(255, 255, 255, 200 if light else 90), 0.8))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(thumb.adjusted(0.4, 0.4, -0.4, -0.4), thumb.height() / 2, thumb.height() / 2)
        p.setFont(_font(9.6, 600))
        for i, (key, label) in enumerate(self.options):
            alpha = 0.3 if key in self.disabled else (1.0 if key == self.value else 0.72)
            p.setPen(_ink(light, alpha))
            p.drawText(self._seg_rect(i), Qt.AlignCenter, label)


class Switch(_Control):
    """iOS-style toggle."""
    toggled = Signal(bool)

    def __init__(self, checked, parent=None):
        super().__init__(parent)
        self.checked = checked
        self.pos_ = 1.0 if checked else 0.0
        self.setFixedSize(46, 28)
        self.anim = QTimer(self)
        self.anim.timeout.connect(self._step)

    def set_checked(self, checked):
        self.checked = checked
        self.anim.start(16)

    def _step(self):
        target = 1.0 if self.checked else 0.0
        self.pos_ += (target - self.pos_) * 0.3
        if abs(self.pos_ - target) < 0.01:
            self.pos_ = target
            self.anim.stop()
        self.update()

    def mousePressEvent(self, _):
        self.set_checked(not self.checked)
        self.toggled.emit(self.checked)

    def paintEvent(self, _):
        light = self.light()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect())
        off = _ink(light, 0.14 if light else 0.18)
        on = QColor("#34C759")
        col = QColor(int(off.red() + (on.red() - off.red()) * self.pos_),
                     int(off.green() + (on.green() - off.green()) * self.pos_),
                     int(off.blue() + (on.blue() - off.blue()) * self.pos_),
                     int(off.alpha() + (255 - off.alpha()) * self.pos_))
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        d = r.height() - 4
        x = 2 + (r.width() - d - 4) * self.pos_
        p.setBrush(QColor(0, 0, 0, 40))
        p.drawEllipse(QRectF(x, 3, d, d))
        p.setBrush(QColor(255, 255, 255))
        p.drawEllipse(QRectF(x, 2, d, d))


class PillButton(_Control):
    clicked = Signal()

    def __init__(self, text, parent=None, accent=False):
        super().__init__(parent)
        self.text = text
        self.accent = accent
        self.hover = False
        self.setFixedHeight(30)
        self.setCursor(Qt.PointingHandCursor)

    def set_text(self, text):
        self.text = text
        self.update()

    def enterEvent(self, _):
        self.hover = True
        self.update()

    def leaveEvent(self, _):
        self.hover = False
        self.update()

    def mousePressEvent(self, _):
        self.clicked.emit()

    def paintEvent(self, _):
        light = self.light()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if self.accent:
            p.setBrush(QColor(10, 132, 255, 235 if self.hover else 210))
            p.setPen(Qt.NoPen)
        else:
            p.setBrush(QColor(255, 255, 255, (250 if self.hover else 225) if light else (80 if self.hover else 52)))
            p.setPen(QPen(QColor(255, 255, 255, 190 if light else 80), 0.8))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        p.setFont(_font(9.6, 600))
        p.setPen(QColor("white") if self.accent else _ink(light))
        p.drawText(r, Qt.AlignCenter, self.text)


class SettingsPanel(QWidget):
    """Glass settings window. Every change is applied live through settings.set()."""

    W = 404
    RADIUS = 28
    ROW = 46

    def __init__(self, settings, hotkey, on_capture_key, on_edit_vocab, status):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle("WhisperX11")
        self.settings = settings
        self.hotkey = hotkey
        self.on_capture_key = on_capture_key
        self.status = status
        self.light = False
        self.glass = None
        self.backdrop = None
        self.capturing = False
        self.drag = None
        self.t0 = time.monotonic()

        self.rows = []
        self.mode = self._row("Mode", Segmented([("toggle", "Toggle"), ("ptt", "Push to talk")], settings["mode"]))
        self.task = self._row("Task", Segmented([("transcribe", "Transcribe"), ("translate", "Translate → EN")],
                                                settings["task"]))
        self.lang = self._row("Language", Segmented(LANG_OPTIONS, settings["languages"]))
        self.insert = self._row("Insert by", Segmented([("type", "Typing"), ("paste", "Pasting")], settings["insert"]))
        self.ptt = self._row("Push-to-talk key", PillButton(key_label(settings["ptt_key"])), width=150)
        self.sounds = self._row("Sounds", Switch(settings["sounds"]), width=46)
        self.preview = self._row("Live caption", Switch(settings["preview"]), width=46)
        self.early = self._row("Type stable words early", Switch(settings["early_commit"]), width=46)
        self.vocab = PillButton("Edit vocabulary…", self)
        self.close_btn = PillButton("Done", self, accent=True)

        self.mode.changed.connect(lambda v: settings.set("mode", v))
        self.task.changed.connect(lambda v: settings.set("task", v))
        self.lang.changed.connect(lambda v: settings.set("languages", v))
        self.insert.changed.connect(lambda v: settings.set("insert", v))
        self.sounds.toggled.connect(lambda v: settings.set("sounds", v))
        self.preview.toggled.connect(lambda v: settings.set("preview", v))
        self.early.toggled.connect(lambda v: settings.set("early_commit", v))
        self.ptt.clicked.connect(self._capture)
        self.vocab.clicked.connect(on_edit_vocab)
        self.close_btn.clicked.connect(self.hide)
        settings.listeners.append(self._on_setting)

        self.H = 92 + self.ROW * len(self.rows) + 66
        self.resize(self.W, self.H)
        self._layout()
        self.refresh = QTimer(self)
        self.refresh.timeout.connect(self._refresh_backdrop)
        self.anim = QTimer(self)
        self.anim.timeout.connect(self.update)

    def _row(self, title, control, width=None):
        control.setParent(self)
        self.rows.append((title, control, width))
        return control

    def _layout(self):
        y = 84
        for title, control, width in self.rows:
            w = width or 214
            control.setGeometry(int(self.W - 24 - w), int(y + (self.ROW - control.height()) / 2), w, control.height())
            y += self.ROW
        self.vocab.setGeometry(24, self.H - 52, 150, 30)
        self.close_btn.setGeometry(self.W - 24 - 96, self.H - 52, 96, 30)

    def _on_setting(self, key, value):
        widget = {"mode": self.mode, "task": self.task, "languages": self.lang, "insert": self.insert}.get(key)
        if widget and widget.value != value:
            widget.set_value(value)
        switch = {"sounds": self.sounds, "preview": self.preview, "early_commit": self.early}.get(key)
        if switch and switch.checked != value:
            switch.set_checked(value)
        if key == "ptt_key":
            self.ptt.set_text(key_label(value))
        self.update()

    def sync(self):
        """Re-check things that can change outside the UI (e.g. the translation model appearing)."""
        if translate_available():
            self.task.set_disabled("translate")
        else:
            self.task.set_disabled("translate", TRANSLATE_HINT)
        self.ptt.set_text("Press a key…" if self.capturing else key_label(self.settings["ptt_key"]))

    def _capture(self):
        self.capturing = True
        self.sync()
        self.on_capture_key(self._captured)

    def _captured(self, name):
        self.capturing = False
        if name and name != "esc":
            self.settings.set("ptt_key", name)
        self.sync()

    # --- showing ------------------------------------------------------------------
    def open(self):
        self.sync()
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        g = screen.availableGeometry()
        self.move(g.right() - self.W - 16, g.top() + 16)
        self.backdrop = None
        self._refresh_backdrop()
        self.show()
        self.raise_()
        self.activateWindow()
        self.refresh.start(500)
        self.anim.start(33)

    def hideEvent(self, e):
        self.refresh.stop()
        self.anim.stop()
        super().hideEvent(e)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape and not self.capturing:
            self.hide()

    def _refresh_backdrop(self):
        dpr = self.devicePixelRatioF()
        bd = grab_backdrop(self.x(), self.y(), self.W, self.H, dpr)
        if bd is None:
            bd = synthetic_backdrop(round(self.H * dpr), round(self.W * dpr))
        if self.backdrop is not None and self.backdrop.shape == bd.shape and \
                abs(self.backdrop[::6, ::6] - bd[::6, ::6]).mean() < 0.003:
            return
        self.backdrop = bd
        m = 0  # the panel fills its window; the shadow is clipped by the window edge
        # panels carry lots of text: heavier blur and frost than the small overlay (like Control Center)
        style = GlassStyle(bevel=22, refraction=1.1, blur=16.0, frost=0.42, saturation=1.8, shadow=0.0)
        self.glass = render_glass(bd, (m, m, self.W * dpr, self.H * dpr), self.RADIUS * dpr, style, dpr)
        self.light = self.glass[2] > 0.55
        for _, control, _ in self.rows:
            control.update()
        self.update()

    # --- dragging -------------------------------------------------------------------
    def mousePressEvent(self, e):
        self.drag = e.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, e):
        if self.drag is not None:
            self.move(e.globalPosition().toPoint() - self.drag)

    def mouseReleaseEvent(self, e):
        self.drag = None
        self.backdrop = None
        self._refresh_backdrop()

    # --- painting -------------------------------------------------------------------
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        dpr = self.devicePixelRatioF()
        r = QRectF(0, 0, self.W, self.H)
        if self.glass:
            img, (x, y), _ = self.glass
            p.drawImage(QRectF(x / dpr, y / dpr, img.width() / dpr, img.height() / dpr), img)
        t = time.monotonic() - self.t0
        paint_specular(p, r.adjusted(0.5, 0.5, -0.5, -0.5), self.RADIUS, 125 + 20 * math.sin(t * 0.5), self.light, 0.9)

        # header: orb + title + status
        c = QPointF(38, 44)
        for i, col in enumerate(SIRI[:5]):
            a = t * 0.9 + i * 2 * math.pi / 5
            g = QRadialGradient(QPointF(c.x() + math.cos(a) * 5, c.y() + math.sin(a) * 5), 13)
            q = QColor(col)
            q.setAlpha(200)
            g.setColorAt(0, q)
            q.setAlpha(0)
            g.setColorAt(1, q)
            p.setPen(Qt.NoPen)
            p.setBrush(g)
            p.drawEllipse(c, 14, 14)
        p.setFont(_font(14.5, 700))
        p.setPen(_ink(self.light))
        p.drawText(QRectF(62, 24, 300, 24), Qt.AlignLeft | Qt.AlignVCenter, "WhisperX11")
        p.setFont(_font(9.2, 500))
        p.setPen(_ink(self.light, 0.6))
        p.drawText(QRectF(62, 46, 320, 18), Qt.AlignLeft | Qt.AlignVCenter, self.status())

        # row titles and separators
        y = 84
        p.setFont(_font(10.4, 500))
        for i, (title, control, _) in enumerate(self.rows):
            p.setPen(_ink(self.light, 0.92))
            p.drawText(QRectF(24, y, 170, self.ROW), Qt.AlignLeft | Qt.AlignVCenter, title)
            if i:
                p.setPen(QPen(_ink(self.light, 0.08), 1))
                p.drawLine(QPointF(24, y), QPointF(self.W - 24, y))
            y += self.ROW
        p.setFont(_font(8.8, 500))
        p.setPen(_ink(self.light, 0.5))
        hint = (f"Hold {key_label(self.settings['ptt_key'])} to talk" if self.settings["mode"] == "ptt"
                else f"{combo_label(self.hotkey)} to start / stop")
        p.drawText(QRectF(24, self.H - 82, self.W - 48, 20), Qt.AlignLeft | Qt.AlignVCenter, hint + " · Esc cancels")


def tray_icon_pixmap(size=64, active=False):
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    c = QPointF(size / 2, size / 2)
    r = size * 0.42
    path = QPainterPath()
    path.addEllipse(c, r, r)
    p.setClipPath(path)
    p.fillPath(path, QColor("#20203a"))
    for i, col in enumerate(SIRI[:5]):
        a = i * 2 * math.pi / 5
        g = QRadialGradient(QPointF(c.x() + math.cos(a) * r * 0.45, c.y() + math.sin(a) * r * 0.45), r)
        q = QColor(col)
        q.setAlpha(230 if active else 200)
        g.setColorAt(0, q)
        q.setAlpha(0)
        g.setColorAt(1, q)
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawEllipse(c, r, r)
    p.setClipping(False)
    # microphone glyph
    p.setPen(QPen(QColor(255, 255, 255, 240), size * 0.06, Qt.SolidLine, Qt.RoundCap))
    p.setBrush(Qt.NoBrush)
    mw, mh = size * 0.16, size * 0.26
    p.drawRoundedRect(QRectF(c.x() - mw / 2, c.y() - mh * 0.75, mw, mh), mw / 2, mw / 2)
    p.drawArc(QRectF(c.x() - mw * 0.95, c.y() - mh * 0.55, mw * 1.9, mh * 1.05), 200 * 16, 140 * 16)
    p.drawLine(QPointF(c.x(), c.y() + mh * 0.5), QPointF(c.x(), c.y() + mh * 0.75))
    p.end()
    return pm


class Tray:
    """System tray icon: quick mode switches in the menu, left click opens the panel."""

    def __init__(self, settings, panel, on_quit, on_edit_vocab):
        self.settings = settings
        self.panel = panel
        self.icon = QSystemTrayIcon(QIcon(tray_icon_pixmap()))
        self.icon.setToolTip("WhisperX11")
        self.menu = QMenu()
        self.menu.setToolTipsVisible(True)
        self.actions = {}
        self._group("Mode", "mode", [("toggle", "Toggle (start / stop)"), ("ptt", "Push to talk")])
        self._group("Task", "task", [("transcribe", "Transcribe"), ("translate", "Translate to English")])
        self._group("Language", "languages", LANG_OPTIONS)
        self._group("Insert by", "insert", [("type", "Typing (clipboard untouched)"), ("paste", "Pasting")])
        self.menu.addSeparator()
        for key, label in (("sounds", "Sounds"), ("preview", "Live caption"), ("early_commit", "Type stable words early")):
            a = QAction(label, self.menu, checkable=True)
            a.setChecked(settings[key])
            a.toggled.connect(lambda v, k=key: settings.set(k, v))
            self.menu.addAction(a)
            self.actions[(key, None)] = a
        self.menu.addSeparator()
        self.menu.addAction("Settings…", panel.open)
        self.menu.addAction("Edit vocabulary…", on_edit_vocab)
        self.menu.addSeparator()
        self.menu.addAction("Quit WhisperX11", on_quit)
        self.menu.aboutToShow.connect(self.sync)
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._activated)
        settings.listeners.append(self._on_setting)
        self.sync()
        self.icon.show()

    def _group(self, title, key, options):
        self.menu.addSection(title)
        group = QActionGroup(self.menu)
        for value, label in options:
            a = QAction(label, self.menu, checkable=True)
            a.setChecked(self.settings[key] == value)
            a.triggered.connect(lambda _=False, v=value: self.settings.set(key, v))
            group.addAction(a)
            self.menu.addAction(a)
            self.actions[(key, value)] = a

    def sync(self):
        a = self.actions[("task", "translate")]
        ok = translate_available()
        a.setEnabled(ok)
        a.setToolTip("" if ok else TRANSLATE_HINT)
        a.setText("Translate to English" if ok else "Translate to English (needs medium model)")

    def _on_setting(self, key, value):
        for (k, v), a in self.actions.items():
            if k == key:
                a.blockSignals(True)
                a.setChecked(value if v is None else v == value)
                a.blockSignals(False)

    def _activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            if self.panel.isVisible():
                self.panel.hide()
            else:
                self.panel.open()

    def set_active(self, active):
        self.icon.setIcon(QIcon(tray_icon_pixmap(active=active)))
