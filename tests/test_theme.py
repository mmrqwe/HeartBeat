"""主题测试：浅色/深色样式与字体解析。"""

import sys

from PySide6.QtWidgets import QApplication

from gui import theme


def _ensure_app():
    return QApplication.instance() or QApplication(sys.argv)


def test_build_stylesheet_light_and_dark():
    _ensure_app()
    light = theme.build_stylesheet(False)
    dark = theme.build_stylesheet(True)
    assert theme.BG in light
    assert theme.DARK_BG in dark
    assert "font-family" in light


def test_bubble_style_follows_dark_mode():
    _ensure_app()
    theme.set_dark(True)
    try:
        assert theme.DARK_CARD in theme.bubble_style("assistant")
    finally:
        theme.set_dark(False)
    assert theme.CARD in theme.bubble_style("assistant")


def test_code_style_non_empty():
    _ensure_app()
    assert "pre" in theme.code_style()
