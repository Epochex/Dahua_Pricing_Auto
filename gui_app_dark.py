# gui_app.py
from __future__ import annotations

import sys
import re
import queue
import threading
import builtins

from PyQt6.QtCore import QObject, pyqtSignal, Qt
from PyQt6.QtGui import QFont, QFontDatabase, QGuiApplication, QFontMetricsF
from PyQt6.QtWidgets import (
    QApplication,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPlainTextEdit,
    QLineEdit,
    QLabel,
    QPushButton,
    QFrame,
)


# 你的 CLI 里可能有 ANSI（加粗/颜色）；GUI 不解释，直接移除
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# 统一字号（你要更大就改这里）
FONT_PT = 13


class EmittingStream(QObject):
    text_written = pyqtSignal(str)

    def write(self, s: str) -> None:
        if not s:
            return
        # 1) 去 ANSI
        s = ANSI_RE.sub("", s)
        # 2) 去掉 \r（carriage return），避免 GUI 行覆盖导致的“竖线漂移/残影”
        s = s.replace("\r", "")
        self.text_written.emit(s)

    def flush(self) -> None:
        pass


class CliWorker(QObject):
    finished = pyqtSignal(int)

    def __init__(self, input_queue: "queue.Queue[str]"):
        super().__init__()
        self.input_queue = input_queue

    def run(self) -> None:
        import os
        os.environ["DAHUA_NO_ANSI"] = "1"
        try:
            import main as cli_main  # 你的 main.py
        except Exception as e:
            print(f"❌ 无法 import main.py：{e}\n")
            self.finished.emit(1)
            return

        original_input = builtins.input

        def gui_input(prompt: str = "") -> str:
            if prompt:
                print(prompt, end="")
            return self.input_queue.get()

        builtins.input = gui_input

        try:
            cli_main.main()
            self.finished.emit(0)
        except SystemExit as e:
            code = int(getattr(e, "code", 0) or 0)
            self.finished.emit(code)
        except Exception as e:
            print(f"\n❌ 程序异常：{e}\n")
            self.finished.emit(2)
        finally:
            builtins.input = original_input


def _fixed_mono_font(point_size: int = FONT_PT) -> QFont:
    """
    关键：用 Qt 系统固定宽度字体（FixedFont），避免 fallback 到非等宽字体导致对齐崩溃。
    """
    f = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    f.setPointSize(point_size)
    f.setStyleHint(QFont.StyleHint.Monospace)
    f.setFixedPitch(True)
    return f


METRO_QSS = """
QWidget {
    background: #0F172A;
    color: #E5E7EB;
    font-family: "Segoe UI";
    font-size: 12px;
}

QFrame#Header {
    background: #111C33;
    border: 1px solid #1F2A44;
    border-radius: 14px;
}
QLabel#Title {
    font-size: 16px;
    font-weight: 600;
    color: #F9FAFB;
}
QLabel#SubTitle, QLabel#Status {
    color: #9CA3AF;
}

QPlainTextEdit#Console {
    background: #0B1224;
    border: 1px solid #1F2A44;
    border-radius: 14px;
    padding: 12px;
    selection-background-color: #0EA5E9;
    selection-color: #081018;

    /* 关键：强制等宽字体，否则 tabulate 必错位 */
    font-family: "Cascadia Mono", "Consolas", "Courier New";
    font-size: 13pt;
}

QFrame#CommandBar {
    background: #0B1224;
    border: 1px solid #1F2A44;
    border-radius: 14px;
}

/* ===== Command input ===== */
QLineEdit#CommandInput {
    background: #0F172A;
    border: 1px solid #24324F;
    border-radius: 12px;
    padding: 10px 12px;
    color: #E5E7EB;

    /* 输入也用等宽，观感一致 */
    font-family: "Cascadia Mono", "Consolas", "Courier New";
    font-size: 13pt;
}

QLineEdit#CommandInput:focus {
    border: 1px solid #38BDF8;
}

QPushButton {
    background: #111C33;
    border: 1px solid #24324F;
    border-radius: 12px;
    padding: 10px 14px;
    color: #E5E7EB;
}
QPushButton:hover {
    border: 1px solid #38BDF8;
}
QPushButton:pressed {
    background: #0F172A;
}
QPushButton#Primary {
    background: #0EA5E9;
    border: 1px solid #0EA5E9;
    color: #081018;
    font-weight: 600;
}
QPushButton#Primary:hover {
    background: #38BDF8;
    border: 1px solid #38BDF8;
}
QPushButton#Danger {
    background: #EF4444;
    border: 1px solid #EF4444;
    color: #0B1224;
    font-weight: 600;
}
QPushButton#Danger:hover {
    background: #F87171;
    border: 1px solid #F87171;
}
"""


class CliWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Dahua Pricing Automation")
        self.resize(1050, 760)
        self.setStyleSheet(METRO_QSS)

        root = QVBoxLayout()
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(12)

        # Header
        header = QFrame()
        header.setObjectName("Header")
        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(16, 14, 16, 14)
        header_layout.setSpacing(12)

        title_box = QVBoxLayout()
        title_box.setSpacing(2)

        self.title = QLabel("大华法国驻地专用产品自动化计算查询软件")
        self.title.setObjectName("Title")
        self.subtitle = QLabel("CLI Wrapper (Metro UI) — Unicode-safe output")
        self.subtitle.setObjectName("SubTitle")

        title_box.addWidget(self.title)
        title_box.addWidget(self.subtitle)

        header_layout.addLayout(title_box)
        header_layout.addStretch(1)

        self.btn_copy_all = QPushButton("Copy")
        self.btn_copy_all.clicked.connect(self.copy_all)
        self.btn_clear = QPushButton("Clear")
        self.btn_clear.clicked.connect(self.clear_console)

        header_layout.addWidget(self.btn_copy_all)
        header_layout.addWidget(self.btn_clear)
        header.setLayout(header_layout)
        root.addWidget(header)

        # Console (必须：等宽 + NoWrap)
        self.console = QPlainTextEdit()
        self.console.setObjectName("Console")
        self.console.setReadOnly(True)
        self.console.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

        mono = _fixed_mono_font(FONT_PT)
        self.console.setFont(mono)
        self.console.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.console.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        # 如果输出里存在 TAB，强制固定 tab stop（可选，但稳）
        fm = QFontMetricsF(mono)
        self.console.setTabStopDistance(fm.horizontalAdvance(" ") * 4)

        root.addWidget(self.console, 1)

        # Command bar
        cmd_bar = QFrame()
        cmd_bar.setObjectName("CommandBar")
        cmd_layout = QHBoxLayout()
        cmd_layout.setContentsMargins(12, 10, 12, 10)
        cmd_layout.setSpacing(10)

        self.input = QLineEdit()
        self.input.setObjectName("CommandInput")
        self.input.setFont(mono)  # 输入也用等宽
        self.input.setPlaceholderText("输入 Part No. 回车发送；直接回车进入批量；输入 quit 退出。")
        self.input.returnPressed.connect(self.on_send)

        self.btn_send = QPushButton("Send")
        self.btn_send.setObjectName("Primary")
        self.btn_send.clicked.connect(self.on_send)

        self.btn_quit = QPushButton("Quit")
        self.btn_quit.setObjectName("Danger")
        self.btn_quit.clicked.connect(self.send_quit)

        cmd_layout.addWidget(self.input, 1)
        cmd_layout.addWidget(self.btn_send)
        cmd_layout.addWidget(self.btn_quit)

        cmd_bar.setLayout(cmd_layout)
        root.addWidget(cmd_bar)

        self.status = QLabel("Ready")
        self.status.setObjectName("Status")
        root.addWidget(self.status)

        self.setLayout(root)

        # CLI plumbing
        self.input_queue: "queue.Queue[str]" = queue.Queue()

        self.stdout_stream = EmittingStream()
        self.stderr_stream = EmittingStream()
        self.stdout_stream.text_written.connect(self.append_text)
        self.stderr_stream.text_written.connect(self.append_text)

        self._orig_stdout = sys.stdout
        self._orig_stderr = sys.stderr
        sys.stdout = self.stdout_stream  # type: ignore[assignment]
        sys.stderr = self.stderr_stream  # type: ignore[assignment]

        self.worker = CliWorker(self.input_queue)
        self.worker.finished.connect(self.on_finished)  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.worker.run, daemon=True)
        self.thread.start()

        self.input.setFocus()

    def append_text(self, s: str) -> None:
        # 追加输出
        self.console.moveCursor(self.console.textCursor().MoveOperation.End)
        self.console.insertPlainText(s)
        self.console.moveCursor(self.console.textCursor().MoveOperation.End)

    def on_send(self) -> None:
        text = self.input.text()
        self.input.clear()
        self.input_queue.put(text)
        self.status.setText(f"Sent: {text}")

    def send_quit(self) -> None:
        self.input.clear()
        self.input_queue.put("quit")
        self.status.setText("Sent: quit")

    def clear_console(self) -> None:
        self.console.clear()
        self.status.setText("Console cleared")

    def copy_all(self) -> None:
        QGuiApplication.clipboard().setText(self.console.toPlainText())
        self.status.setText("Copied console text to clipboard")

    def on_finished(self, code: int) -> None:
        self.status.setText(f"CLI finished with code={code}")

    def closeEvent(self, event) -> None:
        sys.stdout = self._orig_stdout
        sys.stderr = self._orig_stderr
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    w = CliWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
