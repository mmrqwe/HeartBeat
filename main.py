"""HeartBeat 桌宠主入口（PySide6）。运行：py -3.12 main.py"""

import argparse
import os
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

import agent
import core
import db
import skins
import theme
from chat_window import ChatWindow
from pet_window import PetWindow
from search_window import SearchWindow
from settings_window import SettingsWindow

TICK_TIMEOUT_MS = 180_000
CHAT_TIMEOUT_MS = 120_000


def default_config_path():
    """frozen 与开发模式统一：数据放用户数据目录，重编译/升级不丢。"""
    return core.user_data_dir() / "config.json"


def legacy_data_dirs():
    """旧版数据所在位置（app bundle 内 / 源码目录），用于首启自动迁移。"""
    dirs = []
    if getattr(sys, "frozen", False):
        dirs.append(Path(sys.executable).parent)
    dirs.append(Path(__file__).parent)
    return dirs


class Bridge(QObject):
    """工作线程 → GUI 线程的信号桥。"""

    delta = Signal(object, str)
    reply = Signal(object, str)
    tick = Signal(object, object, str)
    status = Signal(str)
    # 工具确认：cmdline, event, result_holder（子线程 emit 后阻塞等 event）
    tool_confirm = Signal(str, object, object)


class HeartBeatApp:
    def __init__(self, config_path=None):
        data_dir = core.migrate_legacy_data(legacy_data_dirs())
        self.config_path = Path(config_path) if config_path else data_dir / "config.json"
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.config_path.exists():
            core.save_config(core.load_config(), self.config_path)
        self.cfg = core.load_config(self.config_path)
        # 补全缺失的默认键（迁移来的旧 config 可能缺新键），保证磁盘配置完整
        core.save_config(self.cfg, self.config_path)
        self.plugins = core.discover_plugins()
        self.db = db.Database(self.config_path.parent / "heartbeat.db")
        self.stats = core.Stats(self.db)
        self.agent = agent.Agent(
            self.cfg, self.plugins, self.config_path.parent, stats=self.stats, db=self.db
        )

        self.bridge = Bridge()
        self.bridge.delta.connect(self._apply_stream_delta)
        self.bridge.reply.connect(self._show_reply)
        self.bridge.tick.connect(self._show_tick_result)
        self.bridge.status.connect(self._set_status)
        self.bridge.tool_confirm.connect(self._on_tool_confirm)
        # 注入工具确认回调：confirm 档写命令由主线程弹窗决定（60s 超时拒绝）
        self.agent.tool_confirm_cb = self._confirm_tool

        self.pet = PetWindow(self.cfg)
        self.pet.open_chat_requested.connect(self._open_chat)
        self.pet.tick_requested.connect(self._autonomy_tick)
        self.pet.say_requested.connect(
            lambda: self.pet.show_bubble("我在呀～想我了？", animation="wave")
        )
        self.pet.settings_requested.connect(self._open_settings)
        self.pet.search_requested.connect(self._open_search)
        self.pet.quit_requested.connect(QApplication.instance().quit)
        self.pet.set_status(f"你好，我是{self.cfg['pet_name']}")
        role = self.cfg.get("role") or "小宠物"
        self.pet.show_bubble(
            f"我是{self.cfg['pet_name']}，{role}，以后我会自己找新鲜事跟你说～",
            seconds=6,
        )

        self.chat_win = None
        self.settings_win = None
        self.search_win = None
        self._busy = False
        self._tick_epoch = 0
        self._tick_timer = QTimer()
        self._tick_timer.setSingleShot(True)
        self._tick_timer.timeout.connect(self._autonomy_tick)
        self._tick_watchdog = QTimer()
        self._tick_watchdog.setSingleShot(True)
        self._tick_watchdog.timeout.connect(self._tick_timeout)
        self._chat_epoch = 0
        self._chat_watchdog = QTimer()
        self._chat_watchdog.setSingleShot(True)
        self._chat_watchdog.timeout.connect(self._chat_timeout)

        self.pet.show()
        QTimer.singleShot(15_000, self._autonomy_tick)

        self._setup_tray()

    # ---------- 状态栏托盘（macOS 菜单栏） ----------

    def _setup_tray(self):
        """macOS 27 上 QSystemTrayIcon 点击必崩（QTBUG-147449），优先用 PyObjC 原生状态栏。

        HB_NO_MAC_TRAY=1 时跳过 PyObjC（无 GUI 会话的 CI/冒烟环境 AppKit 不可用）。
        """
        if sys.platform == "darwin" and not os.environ.get("HB_NO_MAC_TRAY"):
            try:
                if self._setup_macos_status_item():
                    return
            except Exception:
                pass
        self._setup_qt_tray()

    def _setup_macos_status_item(self):
        """用 AppKit 原生 NSStatusItem 创建状态栏图标（绕开 Qt 的 libqcocoa 托盘实现）。"""
        from AppKit import (
            NSImage,
            NSMenu,
            NSMenuItem,
            NSStatusBar,
            NSVariableStatusItemLength,
        )
        from Foundation import NSObject
        import objc

        icon_path = self._app_icon_path()
        if not icon_path:
            return False

        class TrayDelegate(NSObject):
            def initWithActions_(self, actions):
                self = objc.super(TrayDelegate, self).init()
                self._actions = actions
                return self

            def toggle_(self, sender):
                self._actions["toggle"]()

            def tick_(self, sender):
                self._actions["tick"]()

            def settings_(self, sender):
                self._actions["settings"]()

            def search_(self, sender):
                self._actions["search"]()

            def quit_(self, sender):
                self._actions["quit"]()

        delegate = TrayDelegate.alloc().initWithActions_(
            {
                "toggle": self._toggle_pet,
                "tick": self._autonomy_tick,
                "settings": self._open_settings,
                "search": self._open_search,
                "quit": self._quit,
            }
        )

        item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength
        )
        if item is None:
            return False
        image = NSImage.alloc().initWithContentsOfFile_(icon_path)
        if image is None:
            return False
        image.setSize_((18, 18))
        button = item.button()
        button.setImage_(image)
        button.setToolTip_(f"HeartBeat 像素桌宠 · {self.cfg['pet_name']}")

        menu = NSMenu.alloc().init()
        for title, selector in [
            ("显示 / 隐藏桌宠", "toggle:"),
            ("立即巡视", "tick:"),
            ("设置…", "settings:"),
            ("搜索…", "search:"),
            (None, None),
            ("退出", "quit:"),
        ]:
            if title is None:
                menu.addItem_(NSMenuItem.separatorItem())
                continue
            entry = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                title, selector, ""
            )
            entry.setTarget_(delegate)
            menu.addItem_(entry)
            if selector == "toggle:":
                self._mac_tray_toggle_item = entry
        item.setMenu_(menu)
        # 初始勾选状态：桌宠当前可见
        self._set_tray_toggle_state(self.pet.isVisible())

        # 保持强引用，防止 delegate/menu 被 GC 后菜单失效
        self._mac_tray = (item, delegate)
        return True

    def _setup_qt_tray(self):
        self.tray = QSystemTrayIcon(self._app_icon())
        menu = QMenu()
        self._tray_toggle_action = QAction("显示 / 隐藏桌宠")
        self._tray_toggle_action.setCheckable(True)
        self._tray_toggle_action.triggered.connect(self._toggle_pet)
        menu.addAction(self._tray_toggle_action)
        menu.addAction("立即巡视", self._autonomy_tick)
        menu.addAction("设置…", self._open_settings)
        menu.addAction("搜索…", self._open_search)
        menu.addSeparator()
        menu.addAction("退出", self._quit)
        self.tray.setContextMenu(menu)
        self.tray.setToolTip(f"HeartBeat 像素桌宠 · {self.cfg['pet_name']}")
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _app_icon_path(self):
        if getattr(sys, "frozen", False):
            candidates = [
                Path(sys.executable).parent / "HeartBeat.icns",
                Path(sys.executable).parent.parent / "Resources" / "HeartBeat.icns",
            ]
        else:
            candidates = [
                Path(__file__).with_name("HeartBeat.icns"),
                Path(__file__).with_name("HeartBeat.ico"),
            ]
        for path in candidates:
            if path.exists():
                return str(path)
        return None

    def _app_icon(self):
        path = self._app_icon_path()
        return QIcon(path) if path else QIcon()

    def _toggle_pet(self):
        if self.pet.isVisible():
            self.pet.hide()
        else:
            self.pet.show()
            self.pet.raise_()
            self.pet.activateWindow()
        self._set_tray_toggle_state(self.pet.isVisible())

    def _set_tray_toggle_state(self, visible):
        """同步托盘菜单'显示/隐藏桌宠'的勾选状态（ObjC NSMenuItem + Qt QAction）。"""
        item = getattr(self, "_mac_tray_toggle_item", None)
        if item is not None:
            try:
                item.setState_(1 if visible else 0)
            except Exception:
                pass
        action = getattr(self, "_tray_toggle_action", None)
        if action is not None:
            action.setChecked(visible)

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._toggle_pet()

    def _quit(self):
        QApplication.instance().quit()

    # ---------- 自主循环 ----------

    def _interval_ms(self):
        return max(1, int(self.cfg["interval_minutes"])) * 60 * 1000

    def _schedule_next(self):
        self._tick_timer.start(self._interval_ms())

    def _autonomy_tick(self):
        self._schedule_next()
        if self._busy:
            return
        self._busy = True
        self._tick_epoch += 1
        self._set_status("巡视中…")
        self._tick_watchdog.start(TICK_TIMEOUT_MS)
        threading.Thread(
            target=self._tick_worker, args=(self._tick_epoch,), daemon=True
        ).start()

    def _tick_worker(self, epoch):
        try:
            ctx = core.gather(
                self.plugins,
                self.cfg,
                self.stats,
                context={"topics": self.agent.patrol_topics()},
            )
            message = self.agent.think(ctx)
            errors = "，".join(ctx["errors"])
        except Exception as exc:
            message, errors = None, str(exc)
        self.bridge.tick.emit(epoch, message, errors)

    def _show_tick_result(self, epoch, message, errors):
        if epoch != self._tick_epoch:
            return
        self._tick_watchdog.stop()
        self._busy = False
        stamp = time.strftime("%H:%M")
        if message:
            preview = message if len(message) <= 14 else message[:13] + "…"
            self._set_status(f"{stamp} 有新想法：{preview}")
            self.agent.append_chat("assistant", message)
            if self.chat_win:
                self.chat_win.add_message("assistant", message)
            self.pet.show_bubble(message, seconds=15, animation="happy")
        elif errors:
            self._set_status(f"{stamp} 采集异常，下次再试")
            self.agent.append_chat("system", f"{stamp} 巡视异常：{errors}")
            if self.chat_win:
                self.chat_win.add_message("system", f"{stamp} 巡视异常：{errors}")
        else:
            self._set_status(f"{stamp} 巡视完，暂无新事")
        self._update_chat_stats()

    def _tick_timeout(self):
        if not self._busy:
            return
        self._busy = False
        self._set_status("巡视超时，已重置，稍后自动重试")

    def _set_status(self, text):
        self.pet.set_status(text)

    # ---------- 聊天 ----------

    def _open_chat(self):
        if self.chat_win is None:
            self.chat_win = ChatWindow(
                self.cfg["pet_name"],
                on_send=self._send_chat,
                on_clear=self._clear_chat,
            )
            for entry in self.agent.chat_history:
                self.chat_win.add_message(entry["role"], entry["text"], entry.get("time"))
            self.chat_win.set_mood(self.agent.state.get("mood", "平静"))
        self.chat_win.set_daily_stats(self._daily_stats_text())
        self.chat_win.show()
        self.chat_win.raise_()
        self.chat_win.activateWindow()

    # ---------- 工具确认（子线程 → 主线程弹窗） ----------

    def _confirm_tool(self, cmdline):
        """子线程调用：请求主线程弹窗确认，阻塞等待结果（超时按拒绝）。"""
        event = threading.Event()
        holder = {}
        self.bridge.tool_confirm.emit(cmdline, event, holder)
        event.wait(60)
        return bool(holder.get("approved", False))

    def _on_tool_confirm(self, cmdline, event, holder):
        """主线程槽：弹窗显示命令全文，用户允许/拒绝。"""
        try:
            box = QMessageBox(None)
            box.setWindowTitle("桌宠请求执行命令")
            box.setText(
                f"桌宠想在你的电脑上执行：\n\n{cmdline}\n\n"
                "请确认这是你要求的操作。点「否」或等待超时都会取消执行。"
            )
            box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
            box.setDefaultButton(QMessageBox.No)
            # 桌宠是菜单栏应用（无 Dock 图标），弹窗必须置顶才能被用户看到
            box.setWindowFlags(box.windowFlags() | Qt.WindowStaysOnTopHint)
            answer = box.exec()
            holder["approved"] = answer == QMessageBox.Yes
        except Exception:
            holder["approved"] = False
        finally:
            event.set()

    def _send_chat(self, text):
        self.pet.play("think")
        self._chat_epoch += 1
        self._chat_watchdog.start(CHAT_TIMEOUT_MS)
        threading.Thread(
            target=self._chat_worker, args=(self._chat_epoch, text), daemon=True
        ).start()

    def _chat_worker(self, epoch, text):
        try:
            reply = self.agent.chat(
                text, on_delta=lambda t, e=epoch: self._stream_delta(e, t)
            )
        except Exception as exc:
            reply = f"我卡壳了：{exc}"
            self.agent.append_chat("assistant", reply)
        self.bridge.reply.emit(epoch, reply)

    def _stream_delta(self, epoch, text):
        if epoch != self._chat_epoch:
            return
        self.bridge.delta.emit(epoch, text)

    def _apply_stream_delta(self, epoch, text):
        if epoch != self._chat_epoch:
            return
        if self.chat_win:
            self.chat_win.begin_stream()
            self.chat_win.update_last_message(text)
            if self.pet._mode != "talk":
                self.pet.play("talk")

    def _show_reply(self, epoch, reply):
        if epoch != self._chat_epoch:
            return
        self._chat_watchdog.stop()
        self.pet.play("talk", 1600)
        if self.chat_win:
            if self.chat_win.is_streaming():
                # 流式已渲染占位气泡：用最终完整文本收尾，避免重复追加
                self.chat_win.finish_stream(reply)
            else:
                self.chat_win.add_message("assistant", reply)
            self.chat_win.set_thinking(False)
            self.chat_win.set_mood(self.agent.state.get("mood", "平静"))
            self.chat_win.set_daily_stats(self._daily_stats_text())
        self._set_status("陪我聊天中")

    def _chat_timeout(self):
        self._chat_epoch += 1
        self._set_status("回复超时，已停止等待")
        if self.chat_win:
            self.chat_win.set_thinking(False)
            self.chat_win.add_message(
                "system", f"{time.strftime('%H:%M')} 回复超时，网络可能卡住了，请重试"
            )

    def _clear_chat(self):
        self.agent.clear_chat_history()
        if self.chat_win:
            self.chat_win.clear()
            self._set_status("聊天记录已清空")

    # ---------- 设置 ----------

    def _open_settings(self):
        if self.settings_win is None:
            self.settings_win = SettingsWindow(self)
        self.settings_win.show()
        self.settings_win.raise_()
        self.settings_win.activateWindow()

    def _open_search(self):
        if self.search_win is None:
            self.search_win = SearchWindow()
        self.search_win.show()
        self.search_win.raise_()
        self.search_win.activateWindow()

    def save_settings(self, cfg):
        self.cfg = cfg
        core.save_config(cfg, self.config_path)
        self.agent.reload(cfg, self.plugins)
        # 向量补索引放后台线程，避免保存设置卡顿十几秒
        threading.Thread(target=self.agent.reindex_async, daemon=True).start()
        self._schedule_next()
        if self.chat_win:
            self.chat_win.setWindowTitle(f"和{cfg['pet_name']}聊天")
            self.chat_win.set_daily_stats(self._daily_stats_text())
        self._set_status("设置已保存")

    def apply_skin(self, name, sync_role=True):
        if name not in skins.SKINS:
            name = skins.DEFAULT_SKIN
        self.cfg["skin"] = name
        if sync_role:
            self.cfg["role"] = skins.SKINS[name].get(
                "role", self.cfg.get("role", "小宠物")
            )
            # 说话方式未自定义时跟随皮肤默认风格
            if not str(self.cfg.get("speaking_style", "") or "").strip():
                self.cfg["speaking_style"] = skins.SKINS[name].get("style", "")
        core.save_config(self.cfg, self.config_path)
        self.pet.apply_skin(name)
        self._set_status(f"已切换皮肤：{skins.SKINS[name]['label']}")

    # ---------- 统计 ----------

    def _daily_stats_text(self):
        today = self.stats.today()
        info = sum(
            coll.get("entries", 0)
            for coll in today.get("collectors", {}).values()
        )
        return (
            f"今日：模型 {today['llm_calls']} 次 · "
            f"信息 {info} 条 · 想法 {today['thoughts']} 条"
        )

    def _update_chat_stats(self):
        if self.chat_win:
            self.chat_win.set_daily_stats(self._daily_stats_text())


def main():
    parser = argparse.ArgumentParser(description="HeartBeat 像素桌宠（PySide6）")
    parser.add_argument("--config", default=str(default_config_path()))
    parser.add_argument("--smoke", action="store_true", help="启动 3 秒后自动退出")
    parser.add_argument(
        "--cli",
        action="store_true",
        help="命令行模式：python main.py --cli <命令> [参数]",
    )
    args, remaining = parser.parse_known_args()
    if args.cli:
        import cli

        sys.exit(cli.run(remaining, default_config=args.config))

    # 桌宠是常驻状态栏应用：Dock 图标由打包时 Info.plist 的 LSUIElement=true 隐藏
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(theme.STYLESHEET)
    # 桌宠是 Tool 悬浮窗，不算“主窗口”；必须手动控制退出，
    # 否则设置窗口一关，Qt 会误以为没有窗口了而自动退出。
    app.setQuitOnLastWindowClosed(False)
    icon = Path(__file__).with_name("HeartBeat.ico")
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))

    HeartBeatApp(args.config)
    if args.smoke:
        QTimer.singleShot(3000, app.quit)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
