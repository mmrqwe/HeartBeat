"""HeartBeat Qt 主题：现代浅色风格。"""

ACCENT = "#2f6fed"
BG = "#f5f6f8"
CARD = "#ffffff"
TEXT = "#1f2329"
SECONDARY = "#6b7280"
BORDER = "#e4e7ec"
DANGER = "#e5484d"

STYLESHEET = f"""
QWidget {{
    background-color: {BG};
    color: {TEXT};
    font-family: "Microsoft YaHei UI";
    font-size: 13px;
}}
QWidget#Card, QFrame#Card {{
    background-color: {CARD};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}
QLabel#Title {{
    font-size: 16px;
    font-weight: 600;
}}
QLabel#Hint {{
    color: {SECONDARY};
    font-size: 12px;
}}
QPushButton {{
    background-color: {CARD};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 7px 16px;
}}
QPushButton:hover {{
    border-color: {ACCENT};
    color: {ACCENT};
}}
QPushButton#Primary {{
    background-color: {ACCENT};
    color: white;
    border: none;
    font-weight: 600;
}}
QPushButton#Primary:hover {{
    background-color: #2560d9;
}}
QPushButton#Danger {{
    color: {DANGER};
}}
QLineEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background-color: {CARD};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 6px 10px;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus, QTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {ACCENT};
}}
QCheckBox, QRadioButton {{
    spacing: 8px;
}}
QTabWidget::pane {{
    border: 1px solid {BORDER};
    border-radius: 10px;
    background: {CARD};
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    padding: 9px 18px;
    margin-right: 4px;
    border-radius: 8px;
    color: {SECONDARY};
}}
QTabBar::tab:selected {{
    background: {ACCENT};
    color: white;
    font-weight: 600;
}}
QTabBar::tab:hover:!selected {{
    color: {ACCENT};
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
    background: #d4d8de;
    border-radius: 5px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{
    background: #b9bfc9;
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0;
}}
QListWidget, QTableWidget {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 8px;
    outline: none;
}}
QHeaderView::section {{
    background: #eef0f4;
    border: none;
    padding: 6px;
    font-weight: 600;
}}
QMenu {{
    background-color: {CARD};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 6px;
}}
QMenu::item {{
    padding: 7px 22px;
    border-radius: 6px;
}}
QMenu::item:selected {{
    background: {ACCENT};
    color: white;
}}
QToolTip {{
    background: {TEXT};
    color: white;
    border: none;
    padding: 5px 8px;
    border-radius: 6px;
}}
"""


def bubble_style(role):
    """聊天气泡样式（QTextBrowser 气泡，内边距由 documentMargin 提供）。"""
    if role == "user":
        return (
            f"background-color: {ACCENT}; color: white;"
            "border-radius: 12px;"
        )
    return (
        f"background-color: {CARD}; color: {TEXT};"
        f"border: 1px solid {BORDER}; border-radius: 12px;"
    )
