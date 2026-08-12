"""pytest 会话级清理。

PySide6 在 Windows 上若让 QCoreApplication/QApplication 单例拖到解释器
退出阶段再析构，进程会以原生崩溃码退出（拿不到测试摘要）。这里在会话
结束时显式发送 DeferredDelete，让 Qt 在正常运行时完成析构。
"""


def pytest_sessionfinish(session, exitstatus):
    from PySide6.QtCore import QCoreApplication, QEvent

    app = QCoreApplication.instance()
    if app is not None:
        app.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
