"""HeartBeat Qt 主题：浅色/深色 + 跨平台字体。"""

from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication

ACCENT = "#2f6fed"
BG = "#f5f6f8"
CARD = "#ffffff"
TEXT = "#1f2329"
SECONDARY = "#6b7280"
BORDER = "#e4e7ec"
DANGER = "#e5484d"

DARK_ACCENT = "#5b8cff"
DARK_BG = "#1b1d23"
DARK_CARD = "#262a33"
DARK_TEXT = "#e8eaf0"
DARK_SECONDARY = "#9aa3b2"
DARK_BORDER = "#363b47"
DARK_DANGER = "#ff6b70"

_dark = False

_FONT_CANDIDATES = (
    "Microsoft YaHei UI",
    "PingFang SC",
    "Noto Sans CJK SC",
    "Segoe UI",
    "Helvetica Neue",
    "Arial",
)


def set_dark(enabled):
    """切换全局深色模式（影响后续 build_stylesheet/bubble_style）。"""
    global _dark
    _dark = bool(enabled)


def is_dark():
    return _dark


def _system_ui_font():
    """按平台选第一个存在的 UI 字体，避免 macOS 上 Microsoft YaHei 失效。"""
    if QApplication.instance() is None:
        return _FONT_CANDIDATES[0]
    try:
        families = set(QFontDatabase.families())
        for name in _FONT_CANDIDATES:
            if name in families:
                return name
    except Exception:
        pass
    return "sans-serif"


def _palette(dark=None):
    dark = _dark if dark is None else dark
    if dark:
        return {
            "ACCENT": DARK_ACCENT,
            "BG": DARK_BG,
            "CARD": DARK_CARD,
            "TEXT": DARK_TEXT,
            "SECONDARY": DARK_SECONDARY,
            "BORDER": DARK_BORDER,
            "DANGER": DARK_DANGER,
        }
    return {
        "ACCENT": ACCENT,
        "BG": BG,
        "CARD": CARD,
        "TEXT": TEXT,
        "SECONDARY": SECONDARY,
        "BORDER": BORDER,
        "DANGER": DANGER,
    }


def build_stylesheet(dark=None):
    """完整 QSS（浅色/深色共用结构，颜色与字体按当前主题解析）。"""
    p = _palette(dark)
    font = _system_ui_font()
    return f"""
QWidget {{
    background-color: {p['BG']};
    color: {p['TEXT']};
    font-family: "{font}";
    font-size: 13px;
}}
QWidget#Card, QFrame#Card {{
    background-color: {p['CARD']};
    border: 1px solid {p['BORDER']};
    border-radius: 10px;
}}
QLabel#Title {{
    font-size: 16px;
    font-weight: 600;
}}
QLabel#Hint {{
    color: {p['SECONDARY']};
    font-size: 12px;
}}
QLabel#CodingStatus {{
    background-color: {p['ACCENT']}22;
    border: 1px solid {p['ACCENT']};
    border-radius: 8px;
    color: {p['TEXT']};
    padding: 6px 10px;
    margin: 6px 8px;
}}
QPushButton {{
    background-color: {p['CARD']};
    border: 1px solid {p['BORDER']};
    border-radius: 8px;
    padding: 7px 16px;
}}
QPushButton:hover {{
    border-color: {p['ACCENT']};
    color: {p['ACCENT']};
}}
QPushButton#Primary {{
    background-color: {p['ACCENT']};
    color: white;
    border: none;
    font-weight: 600;
}}
QPushButton#Primary:hover {{
    background-color: {p['ACCENT']}cc;
}}
QPushButton#Danger {{
    color: {p['DANGER']};
}}
QLineEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background-color: {p['CARD']};
    border: 1px solid {p['BORDER']};
    border-radius: 8px;
    padding: 6px 10px;
    selection-background-color: {p['ACCENT']};
}}
QLineEdit:focus, QTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {p['ACCENT']};
}}
QCheckBox, QRadioButton {{
    spacing: 8px;
}}
QTabWidget::pane {{
    border: 1px solid {p['BORDER']};
    border-radius: 10px;
    background: {p['CARD']};
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    padding: 9px 18px;
    margin-right: 4px;
    border-radius: 8px;
    color: {p['SECONDARY']};
}}
QTabBar::tab:selected {{
    background: {p['ACCENT']};
    color: white;
    font-weight: 600;
}}
QTabBar::tab:hover:!selected {{
    color: {p['ACCENT']};
}}
QScrollArea {{
    border: none;
    background: transparent;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
}}
QScrollBar::handle:vertical {{
    background: {p['BORDER']};
    border-radius: 5px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{
    background: {p['SECONDARY']};
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0;
}}
QListWidget, QTableWidget {{
    background: {p['CARD']};
    border: 1px solid {p['BORDER']};
    border-radius: 8px;
    outline: none;
}}
QHeaderView::section {{
    background: {p['BG']};
    border: none;
    padding: 6px;
    font-weight: 600;
}}
QMenu {{
    background-color: {p['CARD']};
    border: 1px solid {p['BORDER']};
    border-radius: 8px;
    padding: 6px;
}}
QMenu::item {{
    padding: 7px 22px;
    border-radius: 6px;
}}
QMenu::item:selected {{
    background: {p['ACCENT']};
    color: white;
}}
QToolTip {{
    background: {p['TEXT']};
    color: {p['BG']};
    border: none;
    padding: 5px 8px;
    border-radius: 6px;
}}
"""


# 兼容旧引用：默认浅色主题
STYLESHEET = build_stylesheet(False)


def bubble_style(role):
    """聊天气泡样式（QTextBrowser 气泡，内边距由 documentMargin 提供）。"""
    p = _palette()
    if role == "user":
        return (
            f"background-color: {p['ACCENT']}; color: white;"
            "border-radius: 12px;"
        )
    if role == "system":
        return (
            f"background-color: {p['ACCENT']}14; color: {p['TEXT']};"
            f"border: 1px solid {p['BORDER']}; border-radius: 12px;"
        )
    return (
        f"background-color: {p['CARD']}; color: {p['TEXT']};"
        f"border: 1px solid {p['BORDER']}; border-radius: 12px;"
    )


def code_style():
    """Markdown 代码块在 QTextDocument 里的默认样式。"""
    p = _palette()
    code_bg = "#f2f3f5" if not is_dark() else "#1f232d"
    return (
        f"code, pre {{ background-color: {code_bg}; color: {p['TEXT']}; "
        "font-family: Consolas, 'Courier New', monospace; }}"
    )
