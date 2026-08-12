"""HeartBeat 桌宠主入口（PySide6）。运行：py -3.12 main.py"""

import argparse
import os
import sys
import threading
import time
import uuid
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

import agent
import core
import db
import kernel
from brain.coding_agent import is_coding_intent
from gui import skins, theme
from gui.chat_window import ChatWindow
from gui.pet_window import PetWindow
from gui.search_window import SearchWindow
from gui.settings_window import SettingsWindow

from kernel.embedqueue import EmbedQueue

TICK_TIMEOUT_MS = 180_000
CHAT_TIMEOUT_MS = 1_800_000  # 30 分钟：支持 100 轮工具循环的复杂任务
CODING_TIMEOUT_MS = 2_700_000  # 45 分钟：Coding 任务（含后台构建/测试轮询）


class Bridge(QObject):
    """工作线程 → GUI 线程的信号桥。

    reply/tick 结果已由 kernel.runtime 的任务信号回主线程（on_result 回调），
    这里只保留子线程实时事件：流式增量、状态、工具确认。
    """

    delta = Signal(object, str)
    status = Signal(str)
    # 主动打招呼回复（后台线程生成 → 主线程气泡）
    greet_reply = Signal(str)
    # Coding 任务步骤进度（agent 子线程 → 主线程状态行）
    coding_status = Signal(str)
    # 工具确认：cmdline, event, result_holder（子线程 emit 后阻塞等 event）
    tool_confirm = Signal(str, object, object)


class HeartBeatApp:
    def __init__(self, config_path=None, data_dir=None):
        # 内核：迁移/配置/插件发现/运行时调度（Kernel 门面）
        self.kernel = kernel.Kernel(config_path, data_dir=data_dir)
        self.cfg = self.kernel.cfg
        self.plugins = self.kernel.plugins
        self.data_dir = self.kernel.data_dir
        self.db = db.Database(self.data_dir / "heartbeat.db")
        self.stats = core.Stats(self.db)
        # 向量索引异步队列（P0）：embedding 挪到后台单 worker，聊天/记忆写入
        # 不再同步跑 ONNX 推理；worker 失败 log-and-drop，缺失向量由 reindex 补齐
        self.embed_queue = EmbedQueue()
        self.agent = agent.create_agent(
            self.cfg,
            self.plugins,
            self.data_dir,
            stats=self.stats,
            db=self.db,
            embed_queue=self.embed_queue,
        )
        self.embed_queue.set_worker(self._embed_worker)
        # 事件总线注入：agent 工具执行 → tool.executed 旁路通知（异步回主线程）
        self.agent.eventbus = self.kernel.eventbus

        self.bridge = Bridge()
        self.bridge.delta.connect(self._apply_stream_delta)
        self.bridge.status.connect(self._set_status)
        self.bridge.greet_reply.connect(self._show_greet_reply)
        self.bridge.coding_status.connect(self._on_coding_status)
        self.bridge.tool_confirm.connect(self._on_tool_confirm)
        self._setup_runtime()
        # 注入工具确认回调：confirm 档写命令由主线程弹窗决定（60s 超时拒绝）
        self.agent.tool_confirm_cb = self._confirm_tool

        self.pet = PetWindow(self.cfg)
        self.pet.open_chat_requested.connect(self._open_chat)
        self.pet.tick_requested.connect(self._autonomy_tick)
        self.pet.say_requested.connect(self._on_greet_requested)
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
        self._chat_pending = []
        self._coding_pending = []

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
            ("主动思考一下", "tick:"),
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
        menu.addAction("主动思考一下", self._autonomy_tick)
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
        """注册内核任务：tick（主动思考/生活循环）与 chat（聊天）。

        定时调度 / 看门狗超时 / 线程提交 / epoch 竞态保护全部由
        kernel.runtime 管理，这里只提供任务体与结果回调。
        """
        self.kernel.runtime.add_task(
            "tick",
            interval_ms=self._interval_ms(),
            timeout_ms=TICK_TIMEOUT_MS,
            work=self._tick_work,
            on_result=self._show_tick_result,
            on_error=self._tick_error,
            on_timeout=self._tick_timeout,
            # 定时到点走 _autonomy_tick：重排 + busy 检查 + “思考中…”状态提示
            # （与原版 QTimer 直接连 _autonomy_tick 的语义逐行等价）
            on_timer=self._autonomy_tick,
        )
        self.kernel.runtime.add_task(
            "chat",
            timeout_ms=CHAT_TIMEOUT_MS,
            work=self._chat_work,
            on_result=self._show_reply,
            on_error=self._chat_error,
            on_timeout=self._chat_timeout,
        )
        self.kernel.runtime.add_task(
            "coding",
            timeout_ms=CODING_TIMEOUT_MS,
            work=self._coding_work,
            on_result=self._show_coding_reply,
            on_error=self._coding_error,
            on_timeout=self._coding_timeout,
        )
        # 启动后 15 秒首次主动思考
        QTimer.singleShot(15_000, self._autonomy_tick)

    def _schedule_next(self):
        self.kernel.runtime.schedule_next("tick", self._interval_ms())

    def _autonomy_tick(self):
        self._schedule_next()
        if not self.kernel.runtime.trigger("tick"):
            return
        self._set_status("思考中…")

    def _tick_work(self, epoch):
        """主动思考任务体（子线程执行）：采集 + 生活循环。返回 (message, errors)。"""
        trace = f"tick_{uuid.uuid4().hex[:8]}"
        self.agent._trace_id = trace
        self.db.log_event(db.EventType.TICK_STARTED, "main.tick", {}, trace)
        t0 = time.time()
        try:
            ctx = core.gather(
                self.plugins,
                self.cfg,
                self.stats,
                context={"topics": self.agent.patrol_topics()},
            )
            message = self.agent.live(ctx)
            errors = "，".join(ctx["errors"])
            return message, errors
        finally:
            self.db.log_event(
                db.EventType.TICK_FINISHED, "main.tick",
                {"elapsed_ms": int((time.time() - t0) * 1000)}, trace,
            )

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
            self.agent.append_chat("system", f"{stamp} 思考异常：{errors}")
            if self.chat_win:
                self.chat_win.add_message("system", f"{stamp} 思考异常：{errors}")
        else:
            self._set_status(f"{stamp} 想了一圈，暂无新事")
        self._update_chat_stats()

    def _tick_timeout(self):
        self._set_status("思考超时，已重置，稍后自动重试")

    def _tick_error(self, error):
        """tick 任务异常（主线程回调）：透出给用户。"""
        stamp = time.strftime("%H:%M")
        text = f"{stamp} 思考异常：{error}"
        self._set_status(text)
        self.agent.append_chat("system", text)
        if self.chat_win:
            self.chat_win.add_message("system", text)
        self._update_chat_stats()

    def _set_status(self, text):
        self.pet.set_status(text)

    def _on_greet_requested(self):
        """用户主动打招呼：后台生成角色回应，避免卡 UI。"""
        threading.Thread(target=self._greet_worker, daemon=True).start()

    def _greet_worker(self):
        try:
            text = self.agent.greet()
        except Exception:
            text = "我在呀～想我了？"
        self.bridge.greet_reply.emit(text or "我在呀～想我了？")

    def _show_greet_reply(self, text):
        self.pet.show_bubble(text, seconds=6, animation="wave")

    # ---------- 聊天 ----------

    def _open_chat(self):
        if self.chat_win is None:
            self.chat_win = ChatWindow(
                self.cfg["pet_name"],
                on_send=self._send_chat,
                on_clear=self._clear_chat,
                on_pick_dir=self._pick_project_dir,
            )
            for entry in self.agent.chat_history:
                self.chat_win.add_message(entry["role"], entry["text"], entry.get("time"))
            self.chat_win.set_mood(self.agent.state.get("mood", "平静"))
            self.chat_win.set_project_dir(str(self.cfg.get("project_dir", "") or ""))
        self.chat_win.set_daily_stats(self._daily_stats_text())
        self.chat_win.show()
        self.chat_win.raise_()
        self.chat_win.activateWindow()

    def _pick_project_dir(self):
        """目录按钮：选择编程项目目录（选好后编程任务自然路由，无需切换模式）。"""
        from PySide6.QtWidgets import QFileDialog

        current = str(self.cfg.get("project_dir", "") or "")
        path = QFileDialog.getExistingDirectory(
            self.chat_win, "选择编程项目目录", current or str(Path.home())
        )
        if not path:
            return
        self.cfg["project_dir"] = path
        self.kernel.save_settings(self.cfg)  # 持久化 + config.saved 广播
        if self.chat_win:
            self.chat_win.set_project_dir(path)
        self._set_status(f"编程目录：{path}")

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
        # 编程任务自然路由（自进化已移除）：强信号关键词命中即走 coding；
        # 未选项目目录时给出引导，不静默降级为闲聊。
        if is_coding_intent(text):
            if not str(self.cfg.get("project_dir", "") or "").strip():
                self._reply_direct(
                    "这是编程任务，但我还不知道项目目录在哪～\n"
                    "请先点击聊天窗右上角的 📁 目录，选择要操作的文件夹。"
                )
                return
            if not self.kernel.runtime.trigger("coding", text):
                self._coding_pending.append(text)
                self._set_status("编码任务还在执行，已排队")
            return
        if not self.kernel.runtime.trigger("chat", text):
            self._chat_pending.append(text)
            self._set_status("上一条还在回复中，已排队")
            return

    def _reply_direct(self, text):
        """不经过任务队列的直接回复（本地引导提示等）。"""
        if self.chat_win:
            self.chat_win.add_message("assistant", text)
            self.chat_win.set_thinking(False)
        self.pet.show_bubble(text.splitlines()[0], seconds=6)

    def _chat_work(self, epoch, text):
        """聊天任务体（子线程执行）：带工具循环的 LLM 回复。"""
        trace = f"chat_{uuid.uuid4().hex[:8]}"
        self.agent._trace_id = trace
        self.db.log_event(
            db.EventType.CHAT_STARTED, "main.chat", {"text_len": len(text)}, trace,
        )
        t0 = time.time()
        try:
            return self.agent.chat(
                text, on_delta=lambda t, e=epoch: self._stream_delta(e, t)
            )
        finally:
            self.db.log_event(
                db.EventType.CHAT_FINISHED, "main.chat",
                {"elapsed_ms": int((time.time() - t0) * 1000)}, trace,
            )

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
        self._drain_chat_pending()

    # ---------- Coding 模式（平行路径：见 brain/coding_agent.py） ----------

    def _coding_work(self, epoch, text):
        """编码任务体（子线程执行）：coding 循环 + 步骤状态回传。"""
        trace = f"coding_{uuid.uuid4().hex[:8]}"
        self.agent._trace_id = trace
        self.db.log_event(
            db.EventType.CHAT_STARTED, "main.coding", {"text_len": len(text)}, trace,
        )
        t0 = time.time()
        try:
            return self.agent.coding_task(
                text,
                on_status=self.bridge.coding_status.emit,
            )
        finally:
            self.db.log_event(
                db.EventType.CHAT_FINISHED, "main.coding",
                {"elapsed_ms": int((time.time() - t0) * 1000)}, trace,
            )

    def _on_coding_status(self, text):
        """编码任务步骤进度（agent 子线程 → 主线程）：聊天窗状态行。"""
        if self.chat_win:
            self.chat_win.set_coding_status(text)
        preview = text if len(text) <= 16 else text[:15] + "…"
        self._set_status(preview)

    def _show_coding_reply(self, reply):
        """编码任务完成（主线程回调，runtime 已过滤过期 epoch）。"""
        self.pet.play("talk", 1600)
        if self.chat_win:
            self.chat_win.add_message("assistant", reply)
            self.chat_win.set_thinking(False)
            self.chat_win.set_coding_status("任务完成")
        self._set_status("编码任务完成")
        self._drain_coding_pending()

    def _coding_timeout(self):
        self._set_status("编码任务超时，已停止等待")
        if self.chat_win:
            self.chat_win.set_thinking(False)
            self.chat_win.set_coding_status("任务超时")
            self.chat_win.add_message(
                "system",
                f"{time.strftime('%H:%M')} 编码任务超时（45 分钟）。"
                "后台进程已按超时强杀；文件修改都有备份。",
            )
        self._drain_coding_pending()

    def _coding_error(self, error):
        stamp = time.strftime("%H:%M")
        text = f"{stamp} 编码任务失败：{error}"
        self._set_status("编码任务失败")
        if self.chat_win:
            self.chat_win.set_thinking(False)
            self.chat_win.set_coding_status("任务失败")
            self.chat_win.add_message("system", text)
        self._drain_coding_pending()

    def _drain_coding_pending(self):
        """coding 空闲后取出排队任务（FIFO）。"""
        if not self._coding_pending:
            return
        text = self._coding_pending[0]
        if self.kernel.runtime.trigger("coding", text):
            self._coding_pending.pop(0)
            self.pet.play("think")

    def _chat_timeout(self):
        self._set_status("回复超时，已停止等待")
        if self.chat_win:
            self.chat_win.cancel_stream()  # 清理流式占位气泡，避免影响下次回复
            self.chat_win.set_thinking(False)
            self.chat_win.add_message(
                "system", f"{time.strftime('%H:%M')} 回复超时，网络可能卡住了，请重试"
            )
        self._drain_chat_pending()

    def _chat_error(self, error):
        """chat 任务异常（主线程回调）：透出给用户。"""
        stamp = time.strftime("%H:%M")
        text = f"{stamp} 回复失败：{error}"
        self._set_status("回复失败，请稍后重试")
        self.agent.append_chat("system", text)
        if self.chat_win:
            self.chat_win.cancel_stream()
            self.chat_win.set_thinking(False)
            self.chat_win.add_message("system", text)
        self._drain_chat_pending()

    def _drain_chat_pending(self):
        """chat 空闲后取出排队消息（FIFO），避免忙碌时输入被静默丢弃。"""
        if not self._chat_pending:
            return
        text = self._chat_pending[0]
        if self.kernel.runtime.trigger("chat", text):
            self._chat_pending.pop(0)
            self.pet.play("think")

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

    def _embed_worker(self, kind, item_id, text):
        """后台向量索引任务（embed 队列单 worker 串行执行，P0）。

        失败 log-and-drop：不重试不阻塞队列，缺失向量由 reindex 补齐。
        embedder 引用实时取 self.agent.embedder——reload 重建后自动生效
        （属性赋值原子替换 + GIL：旧 ONNX session 正在执行的推理安全完成）。
        """
        if kind not in ("memory", "chat"):
            return
        try:
            if not self.agent.db.vec_ready or not self.agent.embedder.ready:
                return
            vector = self.agent.embedder.embed_one(text)
            if vector:
                self.agent.db.add_embedding(kind, item_id, vector)
        except Exception:
            pass  # log-and-drop：缺失向量由下次 reindex 补齐

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
