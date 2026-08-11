"""桌宠主窗口：Qt 透明置顶窗口 + 像素动画 + 气泡 + 右键菜单。"""

import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QPainter,
    QPainterPath,
)
from PySide6.QtWidgets import QMenu, QWidget

import skins

PIXEL = 5
W, H = 250, 215
PET_X = (W - 20 * PIXEL) // 2
PET_Y = H - 20 * PIXEL - 4

ANIM_DELAYS = {
    "idle": 700,
    "talk": 380,
    "happy": 260,
    "think": 650,
    "sleep": 950,
    "wave": 320,
}


def draw_grid(painter, grid, palette, ox, oy, pixel):
    for row_idx, row in enumerate(grid):
        for col_idx, ch in enumerate(row):
            color = palette.get(ch)
            if not color:
                continue
            painter.fillRect(
                ox + col_idx * pixel,
                oy + row_idx * pixel,
                pixel,
                pixel,
                QColor(color),
            )


class PetWindow(QWidget):
    open_chat_requested = Signal()
    tick_requested = Signal()
    say_requested = Signal()
    settings_requested = Signal()
    search_requested = Signal()
    quit_requested = Signal()

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        # macOS：应用失活（切到其他程序）时 Qt 会自动隐藏 Tool 窗口，
        # 加此属性强制保持可见，否则桌宠一切换程序就消失、无法找回
        self.setAttribute(Qt.WA_MacAlwaysShowToolWindow)
        self.setFixedSize(W, H)

        self._mode = "idle"
        self._frame = 0
        self._current_grid = None
        self._bubble_text = ""
        self._bubble_until = 0.0
        self._status_text = "启动中…"
        self._drag_pos = None
        self._dragged = False

        self._font = QFont("Microsoft YaHei UI", 10)
        self._small_font = QFont("Microsoft YaHei UI", 8)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._animate)
        self._timer.start(ANIM_DELAYS["idle"])

        self.apply_skin(cfg.get("skin", skins.DEFAULT_SKIN))

    def _clamp_to_screen(self):
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        x = max(geo.left(), min(self.x(), geo.right() - self.width() + 1))
        y = max(geo.top(), min(self.y(), geo.bottom() - self.height() + 1))
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)

    def showEvent(self, event):
        self._clamp_to_screen()
        super().showEvent(event)

    # ---------- 皮肤与动作 ----------

    def apply_skin(self, name):
        self.skin = skins.get_skin(name)
        self.frames = skins.build_frames(self.skin)
        self._current_grid = self.frames["idle"][0]
        self.update()

    def play(self, animation, duration_ms=None):
        if animation not in self.frames:
            return
        self._mode = animation
        self._frame = 0
        if duration_ms:
            QTimer.singleShot(duration_ms, self._return_to_rest)

    def _return_to_rest(self):
        if time.time() >= self._bubble_until:
            self._mode = self._rest_animation()

    def _rest_animation(self):
        return "sleep" if self._quiet_now() else "idle"

    def _quiet_now(self):
        try:
            start = int(self.cfg.get("quiet_start", 23))
            end = int(self.cfg.get("quiet_end", 7))
        except (TypeError, ValueError):
            return False
        if start == end:
            return False
        hour = int(time.strftime("%H"))
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    def show_bubble(self, text, seconds=8, animation="talk"):
        self._bubble_text = text
        self._bubble_until = time.time() + seconds
        self.play(animation)
        QTimer.singleShot(seconds * 1000, self._hide_bubble_if_expired)
        self.update()

    def _hide_bubble_if_expired(self):
        if time.time() >= self._bubble_until:
            self._bubble_text = ""
            self._mode = self._rest_animation()
            self.update()

    def set_status(self, text):
        self._status_text = text
        self.update()

    def _animate(self):
        frames = self.frames.get(self._mode) or self.frames["idle"]
        self._frame = (self._frame + 1) % len(frames)
        self._current_grid = frames[self._frame]
        self.update()

    # ---------- 绘制 ----------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        if time.time() < self._bubble_until and self._bubble_text:
            self._draw_bubble(painter)
        draw_grid(
            painter,
            self._current_grid,
            self.skin["palette"],
            PET_X,
            PET_Y,
            PIXEL,
        )
        painter.setFont(self._small_font)
        painter.setPen(QColor("#999999"))
        painter.drawText(4, H - 5, self._status_text)

    def _draw_bubble(self, painter):
        metrics = QFontMetrics(self._font)
        lines = self._wrap_text(self._bubble_text, 170, metrics)
        line_height = metrics.height() + 4
        width = min(
            max(max(metrics.horizontalAdvance(line) for line in lines) + 24, 64),
            W - 20,
        )
        height = len(lines) * line_height + 16
        pet_cx = PET_X + self.skin["width"] * PIXEL // 2
        x0 = max(4, min(pet_cx - width // 2, W - width - 4))
        x1 = x0 + width
        y1 = PET_Y - 6
        y0 = y1 - height

        painter.setPen(QColor("#555555"))
        painter.setBrush(QColor("white"))
        path = QPainterPath()
        path.addRoundedRect(x0, y0, width, height, 10, 10)
        painter.drawPath(path)

        tail = QPainterPath()
        tail.moveTo(pet_cx - 7, y1 - 2)
        tail.lineTo(pet_cx + 7, y1 - 2)
        tail.lineTo(pet_cx, y1 + 7)
        tail.closeSubpath()
        painter.setPen(Qt.NoPen)
        painter.drawPath(tail)

        painter.setPen(QColor("#333333"))
        painter.setFont(self._font)
        y = y0 + 8
        for line in lines:
            painter.drawText(x0 + 12, y + metrics.ascent(), line)
            y += line_height

    def _wrap_text(self, text, max_width, metrics):
        lines = []
        for raw in text.splitlines() or [""]:
            line = ""
            for ch in raw:
                if metrics.horizontalAdvance(line + ch) <= max_width:
                    line += ch
                else:
                    lines.append(line)
                    line = ch
            lines.append(line)
        return lines

    # ---------- 交互 ----------

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint()
            self._dragged = False

    def mouseMoveEvent(self, event):
        if self._drag_pos is None:
            return
        delta = event.globalPosition().toPoint() - self._drag_pos
        if abs(delta.x()) + abs(delta.y()) > 3:
            self._dragged = True
        if self._dragged:
            self.move(self.pos() + delta)
            self._clamp_to_screen()
            self._drag_pos = event.globalPosition().toPoint()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._drag_pos is not None:
            if not self._dragged:
                self.open_chat_requested.emit()
            self._drag_pos = None

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.addAction("立即巡视", self.tick_requested.emit)
        menu.addAction("跟我说句话", self.say_requested.emit)
        menu.addAction("设置…", self.settings_requested.emit)
        menu.addAction("搜索…", self.search_requested.emit)
        menu.addSeparator()
        menu.addAction("退出", self.quit_requested.emit)
        menu.exec(event.globalPos())
