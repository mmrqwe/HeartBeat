"""HeartBeat 桌宠主入口（PySide6）。运行：py -3.12 main.py"""

import argparse
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QInputDialog,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
)

import agent
import core
import db
import kernel
import tools
from gui import skins, theme
from gui.chat_window import ChatWindow
from gui.pet_window import PetWindow
from gui.search_window import SearchWindow
from gui.settings_window import SettingsWindow

from kernel.embedqueue import EmbedQueue

TICK_TIMEOUT_MS = 180_000
CONVERSATION_TIMEOUT_MS = 2_700_000  # 45 分钟：统一会话任务（含编码后台构建/测试轮询）


class Bridge(QObject):
    """工作线程 → GUI 线程的信号桥。

    reply/tick 结果已由 kernel.runtime 的任务信号回主线程（on_result 回调），
    这里只保留子线程实时事件：流式增量、状态、工具确认。
    """

    delta = Signal(object, str, str)  # (epoch, text, session_id)
    status = Signal(str)
    # 主动打招呼回复（后台线程生成 → 主线程气泡）
    greet_reply = Signal(str)
    # Coding 任务步骤进度（agent 子线程 → 主线程状态行）
    coding_status = Signal(str, str)  # (text, session_id)
    # 编码计划确认：plan, event, result_holder（子线程 emit 后阻塞等 event）
    plan_confirm = Signal(str, object, object)
    # 工具确认：cmdline, event, result_holder（子线程 emit 后阻塞等 event）
    tool_confirm = Signal(str, object, object)


class HeartBeatApp:
    def __init__(self, config_path=None, data_dir=None):
        # 内核：迁移/配置/插件发现/运行时调度（Kernel 门面）
        self.kernel = kernel.Kernel(config_path, data_dir=data_dir)
        self.cfg = self.kernel.cfg
        self._apply_theme()
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
        self.bridge.plan_confirm.connect(self._on_plan_confirm)
        self.bridge.tool_confirm.connect(self._on_tool_confirm)
        self._setup_runtime()
        # 注入工具确认回调：confirm 档写命令由主线程弹窗决定（60s 超时拒绝）
        self.agent.tool_confirm_cb = self._confirm_tool
        # 注入编码计划确认：开始动手前先给主人看 3-5 步计划
        self.agent.confirm_plan_cb = self._confirm_plan

        self.pet = PetWindow(self.cfg)
        self.pet.open_chat_requested.connect(self._open_chat)
        self.pet.tick_requested.connect(self._autonomy_tick)
        self.pet.say_requested.connect(self._on_greet_requested)
        self.pet.settings_requested.connect(self._open_settings)
        self.pet.search_requested.connect(self._open_search)
        self.pet.stop_coding_requested.connect(self._cancel_coding)
        self.pet.quit_requested.connect(self._quit)
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
        self._confirm_allowlist = set()  # 本次会话记住的只读/低风险命令
        self._coding_cancel_event = threading.Event()
        self._coding_status_text = ""
        self._coding_running = False
        self._task_mode = "chat"
        # 当前正在执行任务归属的会话（超时/失败回调仍要写回正确的会话）
        self._chat_task_session = "default"
        # 当前活跃会话（多对话）：任务入队时随 payload 透传，回复归属不会串
        self.current_session_id = "default"

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
        self.close()
        QApplication.instance().quit()

    def close(self):
        """优雅关闭：停任务调度 → 停向量队列 → 关闭数据库。

        Windows 上 SQLite 文件会因连接未关闭而锁死临时目录/重命名，
        macOS/Linux 虽可删除打开中的文件，也会留下未收尾的线程，统一走这里。
        """
        try:
            self._save_window_state()
        except Exception:
            pass
        for win in (self.pet, self.chat_win, self.settings_win, self.search_win):
            if win is not None:
                try:
                    win.close()
                except Exception:
                    pass
                try:
                    win.deleteLater()
                except Exception:
                    pass
        try:
            self.kernel.runtime.stop_all()
        except Exception:
            pass
        try:
            self.embed_queue.stop(timeout=2.0)
        except Exception:
            pass
        try:
            self.db.close()
        except Exception:
            pass

    def _save_window_state(self):
        """把聊天/设置/搜索窗口几何保存进配置，下次打开恢复。"""
        state = dict(self.cfg.get("window_state") or {})
        for key, win in (
            ("chat", getattr(self, "chat_win", None)),
            ("settings", getattr(self, "settings_win", None)),
            ("search", getattr(self, "search_win", None)),
        ):
            if win is not None:
                try:
                    state[key] = [win.x(), win.y(), win.width(), win.height()]
                except Exception:
                    pass
        self.cfg["window_state"] = state
        try:
            self.kernel.save_settings(self.cfg)
        except Exception:
            pass

    def _restore_window_geometry(self, win, key):
        try:
            state = (self.cfg.get("window_state") or {}).get(key)
            if state and len(state) == 4:
                win.setGeometry(
                    int(state[0]), int(state[1]),
                    int(state[2]), int(state[3]),
                )
        except Exception:
            pass

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
            timeout_ms=CONVERSATION_TIMEOUT_MS,
            work=self._chat_work,
            on_result=self._show_reply,
            on_error=self._chat_error,
            on_timeout=self._chat_timeout,
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
            # 主动发言属于“默认聊天”，不往当前项目会话里插
            if self.chat_win and self.current_session_id == "default":
                self.chat_win.add_message("assistant", message)
            self.pet.show_bubble(message, seconds=15, animation="happy")
        elif errors:
            self._set_status(f"{stamp} 采集异常，下次再试")
            self.agent.append_chat("system", f"{stamp} 思考异常：{errors}")
            if self.chat_win and self.current_session_id == "default":
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
        if self.chat_win and self.current_session_id == "default":
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
                on_new_session=self._new_session,
                on_switch_session=self._switch_session,
                on_delete_session=self._delete_session,
                on_rename_session=self._rename_session,
                on_cancel_coding=self._cancel_coding,
                on_open_workspace=self._open_workspace,
            )
            for entry in self.agent.chat_history(session_id=self.current_session_id):
                self.chat_win.add_message(entry["role"], entry["text"], entry.get("time"))
            self.chat_win.set_mood(self.agent.state.get("mood", "平静"))
            self.chat_win.set_project_dir(str(self.cfg.get("project_dir", "") or ""))
            self._refresh_sessions()
        self.chat_win.set_daily_stats(self._daily_stats_text())
        self.chat_win.show()
        self._restore_window_geometry(self.chat_win, "chat")
        self.chat_win.set_coding_running(self._coding_running)
        if (
            self._coding_running
            and self._chat_task_session != self.current_session_id
        ):
            info = self.db.session(self._chat_task_session) or {}
            self.chat_win.set_coding_status(
                f"编码任务正在「{info.get('name') or '其他会话'}」会话运行，可在此停止"
            )
        self.chat_win.raise_()
        self.chat_win.activateWindow()

    def _open_workspace(self):
        """工作区按钮：在系统文件管理器里打开 Agent 自己的默认文件夹。"""
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        from kernel.workspace import workspace_root

        root = workspace_root(base=self.data_dir)
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(root))):
            self._set_status(f"打不开工作区：{root}")

    def _pick_project_dir(self):
        """目录按钮：选择编程项目目录 → 启用该目录对应的会话（没有则新建绑定）。

        目录↔会话一对一：已有绑定会话则切过去（保留之前的对话上下文），
        否则用目录名新建会话并绑定。
        """
        from PySide6.QtWidgets import QFileDialog

        current = str(self.cfg.get("project_dir", "") or "")
        path = QFileDialog.getExistingDirectory(
            self.chat_win, "选择编程项目目录", current or str(Path.home())
        )
        if not path:
            return
        existing = self.db.find_session_by_project_dir(path)
        if existing is not None:
            self._switch_session(existing["id"])
            self._set_status(f"编程目录：{path}")
            return
        info = self.db.session(self.current_session_id)
        can_bind = (
            info is not None
            and info["id"] != "default"
            and not info.get("project_dir")
            and not self.db.chat_items(session_id=info["id"], limit=1)
        )
        if can_bind:
            # 当前是刚新建的空对话：直接把它绑到文件夹，不在左侧多加一项
            if self.db.bind_session_project_dir(info["id"], path):
                if (info.get("name") or "").startswith("新对话"):
                    self.db.rename_session(info["id"], Path(path).name or "新会话")
                self._apply_project_dir(path)
                self._refresh_sessions()
                self._set_status(f"编程目录：{path}（已绑定到当前对话）")
                return
        sid = self.db.create_session(Path(path).name or "新会话", project_dir=path)
        self._switch_session(sid)
        self._set_status(f"编程目录：{path}")

    # ---------- 多对话（会话列表） ----------

    def _apply_project_dir(self, path):
        """把全局编程目录切到 path（绑定当前会话后刷新目录按钮）。"""
        self.cfg["project_dir"] = db._normalize_project_dir(path) or path
        self.kernel.save_settings(self.cfg)
        if self.chat_win:
            self.chat_win.set_project_dir(self.cfg["project_dir"])

    def _refresh_sessions(self):
        """聊天窗会话列表刷新（消息数/活跃排序变化后调用）；非真实窗口则跳过。"""
        if self.chat_win is not None and hasattr(self.chat_win, "set_sessions"):
            self.chat_win.set_sessions(self.db.list_sessions(), self.current_session_id)

    def _switch_session(self, session_id):
        """切换当前会话：加载该会话历史；会话绑定目录则同步切换全局编程目录。"""
        info = self.db.session(session_id)
        if info is None:
            session_id = "default"
            info = self.db.session("default")
        if session_id == self.current_session_id and self.chat_win is not None:
            self._refresh_sessions()
            return
        self.current_session_id = session_id
        target_dir = info.get("project_dir") if info else None
        if str(self.cfg.get("project_dir", "") or "") != str(target_dir or ""):
            # 绑定目录的会话切过去，未绑定会话切回来（含默认会话）必须清空，
            # 否则编码任务会继续在旧项目目录里执行。
            self.cfg["project_dir"] = target_dir or ""
            self.kernel.save_settings(self.cfg)  # 持久化 + config.saved 广播
        if self.chat_win is not None:
            self.chat_win.clear()
            for entry in self.agent.chat_history(session_id=session_id):
                self.chat_win.add_message(entry["role"], entry["text"], entry.get("time"))
            self.chat_win.set_project_dir(str(self.cfg.get("project_dir", "") or ""))
            self._refresh_sessions()

    def _new_session(self):
        """新建会话（名称带序号），切过去；不绑定目录（选目录时自然绑定）。"""
        existing = self.db.list_sessions()
        n = sum(
            1 for s in existing
            if (s.get("name") or "").startswith("新对话")
        ) + 1
        sid = self.db.create_session(f"新对话 {n}")
        self._switch_session(sid)
        self._set_status(f"已新建会话：新对话 {n}")

    def _delete_session(self, session_id):
        """删除会话（UI 已确认）；删除的是当前会话则切回默认。"""
        if session_id == "default":
            return
        if not self.db.delete_session(session_id):
            return
        # 会话删除后同步清掉它的滚动摘要状态
        self.agent.state.pop(f"conversation_summary:{session_id}", None)
        self.agent.db.delete_state(f"conversation_summary:{session_id}")
        self.agent._save_state()
        if self.current_session_id == session_id:
            self._switch_session("default")
        else:
            self._refresh_sessions()
        self._set_status("会话已删除")

    def _rename_session(self, session_id):
        info = self.db.session(session_id)
        if info is None:
            return
        name, ok = QInputDialog.getText(
            self.chat_win if self.chat_win is not None else None,
            "重命名对话",
            "新名称：",
            text=str(info.get("name") or ""),
        )
        if not ok or not str(name or "").strip():
            return
        self.db.rename_session(session_id, name.strip())
        self._refresh_sessions()
        self._set_status("会话已重命名")

    # ---------- 工具确认（子线程 → 主线程弹窗） ----------

    def _confirm_tool(self, cmdline):
        """子线程调用：请求主线程弹窗确认，阻塞等待结果（超时按拒绝）。"""
        if (
            isinstance(cmdline, str)
            and cmdline in self._confirm_allowlist
            and not self._is_destructive_confirm(cmdline)
        ):
            return True
        event = threading.Event()
        holder = {}
        self.bridge.tool_confirm.emit(cmdline, event, holder)
        event.wait(60)
        return bool(holder.get("approved", False))

    def _is_destructive_confirm(self, cmdline):
        """写/删除/编辑/恢复类操作永远不能进“记住”白名单。"""
        if not isinstance(cmdline, str):
            return True
        low = cmdline.lower()
        markers = (
            "rm ", "rm -", "mv ", "del ", "remove-item", "write_file",
            "edit_file", "restore", "sudo", ">", ">>", "|",
        )
        return any(m in low for m in markers)

    def _confirm_plan(self, plan):
        """子线程调用：编码动手前请主人确认计划（超时按取消）。"""
        event = threading.Event()
        holder = {}
        self.bridge.plan_confirm.emit(
            tools.redact_secrets(str(plan or "")), event, holder
        )
        event.wait(120)
        return bool(holder.get("approved", False))

    def _format_confirm_text(self, cmdline):
        """确认弹窗文案：diff 载荷转成“改前/改后”预览，并打码密钥。"""
        if isinstance(cmdline, dict):
            action = {
                "write_file": "写入文件",
                "edit_file": "编辑文件",
            }.get(cmdline.get("action"), "修改文件")
            path = str(cmdline.get("path", ""))
            before = str(cmdline.get("before", ""))
            after = str(cmdline.get("after", ""))

            def snippet(text, lines=30):
                parts = text.splitlines()
                body = "\n".join(parts[:lines])
                return body + ("\n…" if len(parts) > lines else "")

            text = "\n".join([
                f"桌宠想{action}：{path}",
                "",
                "修改前：",
                snippet(before),
                "",
                "修改后：",
                snippet(after),
            ])
            return tools.redact_secrets(text)
        return tools.redact_secrets(str(cmdline))

    def _on_plan_confirm(self, plan, event, holder):
        """主线程槽：显示编码计划，等待主人确认/拒绝。"""
        try:
            box = QMessageBox(None)
            box.setWindowTitle("编码计划确认")
            box.setText(
                f"桌宠打算这样改：\n\n{plan}\n\n"
                "确认按这个计划动手吗？点「否」或等待超时会取消。"
            )
            box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
            box.setDefaultButton(QMessageBox.No)
            box.setWindowFlags(box.windowFlags() | Qt.WindowStaysOnTopHint)
            box.setWindowModality(Qt.WindowModal)
            answer = box.exec()
            holder["approved"] = answer == QMessageBox.Yes
        except Exception:
            holder["approved"] = False
        finally:
            event.set()

    def _on_tool_confirm(self, cmdline, event, holder):
        """主线程槽：弹窗显示命令全文，用户允许/拒绝。"""
        try:
            box = QMessageBox(None)
            box.setWindowTitle("桌宠请求确认")
            is_auth = isinstance(cmdline, str) and (
                "skill_auth" in cmdline or "认证" in cmdline
            )
            detail = self._format_confirm_text(cmdline)
            remember = QCheckBox("本次会话记住（只读/低风险命令）")
            box.setCheckBox(remember)
            if is_auth:
                box.setText(
                    f"桌宠要配置技能认证（如知乎），这不是危险操作，"
                    f"请点「是」允许完成配置：\n\n{detail}\n\n"
                    "点「是」= 允许；点「否」或等待超时 = 取消。"
                )
            else:
                box.setText(
                    f"桌宠想在你的电脑上执行：\n\n{detail}\n\n"
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
            if (
                answer == QMessageBox.Yes
                and remember.isChecked()
                and isinstance(cmdline, str)
                and not self._is_destructive_confirm(cmdline)
            ):
                self._confirm_allowlist.add(cmdline)
        except Exception:
            holder["approved"] = False
        finally:
            event.set()

    def _session_is_coding(self, session_id):
        """会话是否绑定了编程目录：绑定即走编码模式，不做关键词猜测。"""
        info = self.db.session(session_id) or {}
        return bool(str(info.get("project_dir") or "").strip())

    def _begin_task_state(self, session_id):
        """任务真正启动后记录归属会话与模式，并同步停止按钮显隐。"""
        self._chat_task_session = session_id
        self._task_mode = "coding" if self._session_is_coding(session_id) else "chat"
        self._coding_running = self._task_mode == "coding"
        if self.chat_win:
            self.chat_win.set_coding_running(self._coding_running)

    def _send_chat(self, text):
        self.pet.play("think")
        # 编码模式跑着的时候，用户问进度/要求停止，直接由宠物回答，不排队走聊天
        if self._coding_running:
            low = str(text).strip().lower()
            if re.search(r"停止|取消|别写了", low):
                self._cancel_coding()
                return
            if re.search(r"在干嘛|干嘛|进度|到哪|完成|好了吗|怎么样|还在吗", low):
                status = self._coding_status_text or "正在处理你的编码任务"
                self._reply_direct(f"我正在：{status}。还要一会儿，好了我叫你～")
                return
        # 只有一个 Agent / 一个任务槽：文件夹会话自然走编码模式，其余走聊天。
        if not self.kernel.runtime.trigger("chat", text, self.current_session_id):
            self._chat_pending.append((text, self.current_session_id))
            self._set_status("上一条还在处理中，已排队")
            return
        self._begin_task_state(self.current_session_id)
        self._refresh_sessions()

    def _reply_direct(self, text):
        """不经过任务队列的直接回复（本地引导提示等），归属当前会话。"""
        self.agent.append_chat("assistant", text, session_id=self.current_session_id)
        if self.chat_win:
            self.chat_win.add_message("assistant", text)
            self.chat_win.set_thinking(False)
        self.pet.show_bubble(text.splitlines()[0], seconds=6)
        self._refresh_sessions()

    def _chat_work(self, epoch, text, session_id="default"):
        """统一会话任务体（子线程执行）：按会话绑定走聊天或编码模式。"""
        trace = f"chat_{uuid.uuid4().hex[:8]}"
        self._coding_cancel_event.clear()
        self.agent._trace_id = trace
        self.db.log_event(
            db.EventType.CHAT_STARTED, "main.chat", {"text_len": len(text)}, trace,
        )
        t0 = time.time()
        try:
            mode, reply = self.agent.converse(
                text,
                on_delta=lambda t, e=epoch, sid=session_id: self._stream_delta(e, t, sid),
                on_status=lambda s, sid=session_id: self.bridge.coding_status.emit(s, sid),
                session_id=session_id,
                cancel_event=self._coding_cancel_event,
            )
            return session_id, mode, reply
        except Exception as exc:
            # 失败消息归属任务会话（子线程直接落库）；主线程回调只做 UI 提示
            prefix = "编码任务失败" if self._session_is_coding(session_id) else "回复失败"
            self.agent.append_chat(
                "system", f"{prefix}：{exc}", session_id=session_id
            )
            raise
        finally:
            self.db.log_event(
                db.EventType.CHAT_FINISHED, "main.chat",
                {"elapsed_ms": int((time.time() - t0) * 1000)}, trace,
            )

    def _stream_delta(self, epoch, text, session_id):
        if epoch != self.kernel.runtime.current_epoch("chat"):
            return
        self.bridge.delta.emit(epoch, text, session_id)

    def _apply_stream_delta(self, epoch, text, session_id):
        if epoch != self.kernel.runtime.current_epoch("chat"):
            return
        if session_id != self.current_session_id:
            return  # 用户已切到别的会话：不往错误窗口里流式渲染
        if self.chat_win:
            self.chat_win.begin_stream()
            self.chat_win.update_last_message(text)
            if self.pet._mode != "talk":
                self.pet.play("talk")

    def _show_reply(self, result):
        """主线程回调（kernel.runtime 已过滤过期 epoch）；result=(session_id, mode, reply)。"""
        session_id, mode, reply = result
        if mode == "coding":
            self._finish_coding(session_id, reply)
            return
        self.pet.play("talk", 1600)
        if self.chat_win and session_id == self.current_session_id:
            if self.chat_win.is_streaming():
                # 流式已渲染占位气泡：用最终完整文本收尾，避免重复追加
                self.chat_win.finish_stream(reply)
            else:
                self.chat_win.add_message("assistant", reply)
            self.chat_win.set_thinking(False)
            self.chat_win.set_mood(self.agent.state.get("mood", "平静"))
            self.chat_win.set_daily_stats(self._daily_stats_text())
        self._set_status("陪我聊天中")
        self._refresh_sessions()
        self._drain_chat_pending()

    # ---------- 编码模式 UI（统一会话任务的一个分支） ----------

    def _finish_coding(self, session_id, reply):
        """编码模式完成：宠物气泡播报 + 步骤状态收尾（回复已由 Agent 落库）。"""
        self.pet.play("happy", 1600)
        self.pet.show_bubble(self._coding_bubble_text(reply), seconds=6)
        if self.chat_win and session_id == self.current_session_id:
            self.chat_win.add_message("assistant", reply)
            self.chat_win.set_thinking(False)
            self.chat_win.set_coding_status("任务完成")
            self.chat_win.set_coding_running(False)
        self._coding_status_text = ""
        self._coding_running = False
        self._task_mode = "chat"
        self._set_status("编码任务完成～")
        self._refresh_sessions()
        self._drain_chat_pending()

    def _on_coding_status(self, text, session_id):
        """编码模式步骤进度（agent 子线程 → 主线程）：只写归属会话的聊天窗。"""
        text = tools.redact_secrets(str(text or ""))
        self._coding_status_text = text
        self._coding_running = True
        if self.chat_win and session_id == self.current_session_id:
            self.chat_win.set_coding_status(text)
            self.chat_win.set_coding_running(True)
        preview = f"编码中：{text}"
        if len(preview) > 20:
            preview = preview[:19] + "…"
        self._set_status(preview)

    def _coding_bubble_text(self, reply):
        """把编码最终回复压缩成宠物气泡的一句话（不贴日志/JSON）。"""
        text = tools.redact_secrets(str(reply or "")).strip()
        lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("```")
        ]
        if not lines:
            return "搞定啦～"
        summary = " ".join(lines[:2])
        if len(summary) > 80:
            summary = summary[:80] + "…"
        return summary

    def _chat_timeout(self):
        session_id = self._chat_task_session
        if self._task_mode == "coding":
            self._set_status("呜，编码任务超时了")
            self.agent.append_chat(
                "system",
                f"{time.strftime('%H:%M')} 编码任务超时（45 分钟）。",
                session_id=session_id,
            )
            self.pet.show_bubble(
                "呜，任务跑超时了……别担心，改过的文件都有备份。",
                seconds=6,
                animation="think",
            )
            self._coding_status_text = ""
            self._coding_running = False
            self._task_mode = "chat"
            if self.chat_win and session_id == self.current_session_id:
                self.chat_win.set_thinking(False)
                self.chat_win.set_coding_status("任务超时")
                self.chat_win.set_coding_running(False)
                self.chat_win.add_message(
                    "system",
                    f"{time.strftime('%H:%M')} 编码任务超时（45 分钟）。"
                    "后台进程已按超时强杀；文件修改都有备份。",
                )
        else:
            self._set_status("回复超时，已停止等待")
            self.agent.append_chat(
                "system",
                f"{time.strftime('%H:%M')} 回复超时，网络可能卡住了，请重试",
                session_id=session_id,
            )
            if self.chat_win and session_id == self.current_session_id:
                self.chat_win.cancel_stream()  # 清理流式占位气泡，避免影响下次回复
                self.chat_win.set_thinking(False)
                self.chat_win.add_message(
                    "system", f"{time.strftime('%H:%M')} 回复超时，网络可能卡住了，请重试"
                )
        self._drain_chat_pending()

    def _chat_error(self, error):
        """统一会话任务异常（主线程回调）：UI 提示（消息已由任务体落库）。"""
        session_id = self._chat_task_session
        stamp = time.strftime("%H:%M")
        if self._task_mode == "coding":
            text = f"{stamp} 编码任务失败：{error}"
            self._set_status("呜，编码任务失败了")
            self.pet.show_bubble(
                "呜，任务失败了……别怕，改过的文件都有备份，要我回滚吗？",
                seconds=8,
                animation="think",
            )
            self._coding_status_text = ""
            self._coding_running = False
            self._task_mode = "chat"
            if self.chat_win and session_id == self.current_session_id:
                self.chat_win.set_thinking(False)
                self.chat_win.set_coding_status("任务失败")
                self.chat_win.set_coding_running(False)
                self.chat_win.add_message("system", text)
        else:
            text = f"{stamp} 回复失败：{error}"
            self._set_status("回复失败，请稍后重试")
            if self.chat_win and session_id == self.current_session_id:
                self.chat_win.cancel_stream()
                self.chat_win.set_thinking(False)
                self.chat_win.add_message("system", text)
        self._drain_chat_pending()

    def _cancel_coding(self):
        """用户主动停止编码模式：取消统一任务、清排队、杀后台进程、气泡确认。"""
        session_id = self._chat_task_session
        running = self.kernel.runtime.cancel("chat")
        had_pending = bool(self._chat_pending)
        self._chat_pending.clear()
        self._coding_cancel_event.set()
        tools.cancel_all_background()
        if not running and not had_pending:
            self._reply_direct("现在没有编码任务在跑呢～")
            return
        self._coding_status_text = ""
        self._coding_running = False
        self._task_mode = "chat"
        if self.chat_win and session_id == self.current_session_id:
            self.chat_win.set_thinking(False)
            self.chat_win.set_coding_status("任务已停止")
            self.chat_win.set_coding_running(False)
            self.chat_win.add_message(
                "system", f"{time.strftime('%H:%M')} 编码任务已手动停止"
            )
        self.agent.append_chat(
            "system",
            f"{time.strftime('%H:%M')} 编码任务已手动停止",
            session_id=session_id,
        )
        self.pet.show_bubble(
            "好，我停下来了～改到一半的文件都有备份。",
            seconds=6,
            animation="wave",
        )
        self._set_status("编码任务已停止")
        self._refresh_sessions()

    def _drain_chat_pending(self):
        """统一会话任务空闲后取出排队消息（FIFO，含会话归属）。"""
        if not self._chat_pending:
            return
        text, session_id = self._chat_pending[0]
        if self.kernel.runtime.trigger("chat", text, session_id):
            self._chat_pending.pop(0)
            self._begin_task_state(session_id)
            self.pet.play("think")

    def _clear_chat(self):
        self.agent.clear_chat_history(session_id=self.current_session_id)
        if self.chat_win:
            self.chat_win.clear()
            self._set_status("当前对话记录已清空")

    # ---------- 设置 ----------

    def _open_settings(self):
        if self.settings_win is None:
            self.settings_win = SettingsWindow(self)
        self.settings_win.show()
        self._restore_window_geometry(self.settings_win, "settings")
        self.settings_win.raise_()
        self.settings_win.activateWindow()

    def _open_search(self):
        if self.search_win is None:
            self.search_win = SearchWindow()
        self.search_win.show()
        self._restore_window_geometry(self.search_win, "search")
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
        self._apply_theme()
        self.agent.reload(cfg, self.plugins)
        # 向量补索引放后台线程，避免保存设置卡顿十几秒
        threading.Thread(target=self.agent.reindex_async, daemon=True).start()
        self._schedule_next()
        if self.chat_win:
            self.chat_win.setWindowTitle(f"和{cfg['pet_name']}聊天")
            self.chat_win.set_daily_stats(self._daily_stats_text())
        self._set_status("设置已保存")

    def _apply_theme(self):
        """按配置应用深色模式与全局字体（聊天气泡同步刷新）。"""
        theme.set_dark(bool(self.cfg.get("dark_mode", False)))
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(theme.build_stylesheet())
        chat_win = getattr(self, "chat_win", None)
        if chat_win is not None and hasattr(chat_win, "apply_theme"):
            chat_win.apply_theme()

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
