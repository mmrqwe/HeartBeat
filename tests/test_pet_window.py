"""宠物窗口布局测试。"""

import sys
import time

from PySide6.QtWidgets import QApplication

from gui.pet_window import H, PET_Y, PIXEL, STATUS_HEIGHT, W, PetWindow


def _app():
    return QApplication.instance() or QApplication(sys.argv)


def test_status_hidden_while_bubble():
    """有气泡时底部状态隐藏，气泡消失后恢复。"""
    _app()
    pet = PetWindow({"skin": "orange_cat"})
    pet._bubble_text = "你好呀"
    pet._bubble_until = time.time() + 10
    assert pet.status_visible() is False
    pet._bubble_until = time.time() - 1
    assert pet.status_visible() is True


def test_status_strip_does_not_overlap_pet():
    """像素宠物底部与状态文字之间必须留出独立状态栏空间。"""
    assert PET_Y + 20 * PIXEL <= H - STATUS_HEIGHT


def test_window_geometry_reasonable():
    assert W > 0 and H > 0
    assert PET_Y >= 0
