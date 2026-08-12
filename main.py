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
import kernel
from brain.smoke import smoke_test_module
from gui import skins, theme
from gui.chat_window import ChatWindow
from gui.pet_window import PetWindow
from gui.search_window import SearchWindow
from gui.settings_window import SettingsWindow

TICK_TIMEOUT_MS = 180_000
CHAT_TIMEOUT_MS = 1_800_000  # 30 分钟：支持 100 轮工具循环的复杂任务


class Bridge(QObject):
    """工作线程 → GUI 线程的信号桥。

    reply/tick 结果已由 kernel.runtime 的任务信号回主线程（on_result 回调），
    这里只保留子线程实时事件：流式增量、状态、工具确认。
    """

    delta = Signal(object, str)
    status = Signal(str)
    # 自我进化进度/结果（agent 后台线程 → 主线程，自动 QueuedConnection）
    evolve_status = Signal(str)
    # 工具确认：cmdline, event, result_holder（子线程 emit 后阻塞等 event）
    tool_confirm = Signal(str, object, object)


class HeartBeatApp:
    def __init__(self, config_path=None):
        # 内核：迁移/配置/插件发现/运行时调度（Kernel 门面）
        self.kernel = kernel.Kernel(config_path)
        self.cfg = self.kernel.cfg
        self.plugins = self.kernel.plugins
        self.data_dir = self.kernel.data_dir
        self.db = db.Database(self.data_dir / "heartbeat.db")
        self.stats = core.Stats(self.db)
        self.agent = agent.create_agent(
            self.cfg,
            self.plugins,
            self.data_dir,
            stats=self.stats,
            db=self.db,
            brain_loader=self.kernel.updater,
        )
        # 事件总线注入：agent 工具执行 → tool.executed 旁路通知（异步回主线程）
        self.agent.eventbus = self.kernel.eventbus
        # 运行期健康监控（kernel.monitor）：tick/chat 心跳 + 超阈值自动回滚
        self.monitor = self.kernel.monitor
        # 自进化注入：updater 的 L2 冒烟 runner（候选模块真实 Agent 实测）
        self.kernel.updater.smoke_runner = smoke_test_module
        # 热切换订阅：updater 切换 brain 版本 → 主线程重载领域模块。
        # async_=True 保证 handler 经 Qt QueuedConnection 回主线程执行——
        # 未来 agent 在子线程触发升级时不会与 chat/think 线程竞态。
        self.kernel.eventbus.subscribe("brain.switched", self._on_brain_switched, async_=True)

        self.bridge = Bridge()
        self.bridge.delta.connect(self._apply_stream_delta)
        self.bridge.status.connect(self._set_status)
        self.bridge.evolve_status.connect(self._on_evolve_status)
        self.bridge.tool_confirm.connect(self._on_tool_confirm)
        self._setup_runtime()
        # 注入工具确认回调：confirm 档写命令由主线程弹窗决定（60s 超时拒绝）
        self.agent.tool_confirm_cb = self._confirm_tool
        # 注入自我进化状态回调：后台线程 emit → 信号桥回主线程（跨线程安全）
        self.agent.evolve_status_cb = self.bridge.evolve_status.emit

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

        self.pet.show()

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

    # ---------- 自主循环（调度在 kernel.runtime） ----------

    def _interval_ms(self):
        return max(1, int(self.cfg["interval_minutes"])) * 60 * 1000

    def _setup_runtime(self):
        """注册内核任务：tick（自主巡视）与 chat（聊天）。

        定时调度 / 看门狗超时 / 线程提交 / epoch 竞态保护全部由
        kernel.runtime 管理，这里只提供任务体与结果回调。
        """
        self.kernel.runtime.add_task(
            "tick",
            interval_ms=self._interval_ms(),
            timeout_ms=TICK_TIMEOUT_MS,
            work=self._tick_work,
            on_result=self._show_tick_result,
            on_timeout=self._tick_timeout,
            # 定时到点走 _autonomy_tick：重排 + busy 检查 + “巡视中…”状态提示
            # （与原版 QTimer 直接连 _autonomy_tick 的语义逐行等价）
            on_timer=self._autonomy_tick,
        )
        self.kernel.runtime.add_task(
            "chat",
            timeout_ms=CHAT_TIMEOUT_MS,
            work=self._chat_work,
            on_result=self._show_reply,
            on_timeout=self._chat_timeout,
        )
        # 启动后 15 秒首次巡视
        QTimer.singleShot(15_000, self._autonomy_tick)

    def _schedule_next(self):
        self.kernel.runtime.schedule_next("tick", self._interval_ms())

    def _autonomy_tick(self):
        self._schedule_next()
        if not self.kernel.runtime.trigger("tick"):
            return
        self._set_status("巡视中…")

    def _tick_work(self, epoch):
        """巡视任务体（子线程执行）：采集 + 思考。返回 (message, errors)。"""
        try:
            ctx = core.gather(
                self.plugins,
                self.cfg,
                self.stats,
                context={"topics": self.agent.patrol_topics()},
            )
            message = self.agent.live(ctx)
            errors = "，".join(ctx["errors"])
        except Exception as exc:
            message, errors = None, str(exc)
        return message, errors

    def _show_tick_result(self, result):
        """主线程回调（kernel.runtime 已过滤过期 epoch）。"""
        message, errors = result
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
        self._set_status("巡视超时，已重置，稍后自动重试")

    def _set_status(self, text):
        self.pet.set_status(text)

    def _on_brain_switched(self, payload):
        """updater 热切换回调：重载领域模块（失败保持旧模块，不中断会话）。"""
        module_name, version = payload
        ok = self.agent.reload_brain_modules()
        self._set_status(
            f"大脑模块 {module_name} 已切换到 {version}"
            + ("" if ok else "（重载失败，保持旧模块）")
        )

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
            box.setWindowTitle("桌宠请求确认")
            is_auth = "skill_auth" in cmdline or "认证" in cmdline
            if is_auth:
                box.setText(
                    f"桌宠要配置技能认证（如知乎），这不是危险操作，"
                    f"请点「是」允许完成配置：\n\n{cmdline}\n\n"
                    "点「是」= 允许；点「否」或等待超时 = 取消。"
                )
            else:
                box.setText(
                    f"桌宠想在你的电脑上执行：\n\n{cmdline}\n\n"
                    "请确认这是你要求的操作。点「否」或等待超时都会取消执行。"
                )
            box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
            box.setDefaultButton(QMessageBox.No)
            # 桌宠是菜单栏应用（无 Dock 图标），弹窗必须置顶才能被用户看到
            box.setWindowFlags(box.windowFlags() | Qt.WindowStaysOnTopHint)
            # 认证配置是用户主动发起的操作：允许后不重复打扰，超时更宽松
            box.setWindowModality(Qt.WindowModal)
            answer = box.exec()
            holder["approved"] = answer == QMessageBox.Yes
        except Exception:
            holder["approved"] = False
        finally:
            event.set()

    def _send_chat(self, text):
        self.pet.play("think")
        self.kernel.runtime.trigger("chat", text)

    def _chat_work(self, epoch, text):
        """聊天任务体（子线程执行）：带工具循环的 LLM 回复。"""
        try:
            return self.agent.chat(
                text, on_delta=lambda t, e=epoch: self._stream_delta(e, t)
            )
        except Exception as exc:
            reply = f"我卡壳了：{exc}"
            self.agent.append_chat("assistant", reply)
            return reply

    def _stream_delta(self, epoch, text):
        if epoch != self.kernel.runtime.current_epoch("chat"):
            return
        self.bridge.delta.emit(epoch, text)

    def _apply_stream_delta(self, epoch, text):
        if epoch != self.kernel.runtime.current_epoch("chat"):
            return
        if self.chat_win:
            self.chat_win.begin_stream()
            self.chat_win.update_last_message(text)
            if self.pet._mode != "talk":
                self.pet.play("talk")

    def _show_reply(self, reply):
        """主线程回调（kernel.runtime 已过滤过期 epoch）。"""
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

    def _on_evolve_status(self, text):
        """自我进化进度/结果（agent 后台线程 → 信号桥 → 主线程）。"""
        if self.chat_win:
            self.chat_win.add_message("assistant", text)
        self.pet.play("talk", 1600)
        preview = text if len(text) <= 16 else text[:15] + "…"
        self._set_status(preview)

    def _chat_timeout(self):
        self._set_status("回复超时，已停止等待")
        self.monitor.record_chat(False)  # 超时计入窗口失败（与异常同权）
        if self.chat_win:
            self.chat_win.cancel_stream()  # 清理流式占位气泡，避免影响下次回复
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
        self.kernel.save_settings(cfg)
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
            # 完整人设同步：身份无条件覆盖；性格/说话方式/示例台词仅未自定义时跟随
            skins.apply_persona(self.cfg, name)
        self.kernel.save_settings(self.cfg)
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
    parser.add_argument("--config", default=str(kernel.boot.default_config_path()))
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
