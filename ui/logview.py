# DWT 日志视图：按阶段标签着色
# 本文件以 MIT 许可证发布，全文见 LICENSE，授权范围见 README.md。
#
# 引擎每行日志以 [阶段] 开头（[帧] [星表] [C] [R] [W] [叠加] [验收] …），
#   这里据此给该行上色。着色共分四类：
#     读帧/落盘类 → 提示灰      权重与分区类 → 强调青绿
#     结果与输出类 → 蓝          错误与终止 → 红
# 不指定任何字体族（只用系统默认字体）。

from __future__ import annotations

import html
import time

from PySide6.QtWidgets import QPlainTextEdit

# 阶段标签 → 颜色（不在此表里的标签走默认色）
_TAG_COLOR = {
    # 读帧 / 落盘 / 杂项
    '帧': '#93a0b0', 'memmap': '#93a0b0', '归一': '#93a0b0', '并行': '#93a0b0',
    '裁剪': '#93a0b0', '时': '#93a0b0',
    # 权重与分区
    '星表': '#0f9d90', 'C': '#0f9d90', 'S': '#0f9d90', 'σ': '#0f9d90',
    'R': '#0f9d90', 'W': '#0f9d90',
    # 结果与输出
    '叠加': '#2b6ea8', '读数': '#2b6ea8', '验收': '#2b6ea8', '输出': '#2b6ea8',
    # 异常
    '错误': '#c0392b', '终止': '#c0392b',
}
_DEFAULT = '#1c2431'
_STAMP = '#b6bfca'


def tag_of(line: str) -> str:
    """取行首 [xxx] 里的标签；没有则返回空串"""
    s = line.lstrip()
    if not s.startswith('['):
        return ''
    end = s.find(']')
    return s[1:end] if end > 0 else ''


class LogView(QPlainTextEdit):
    """只读日志框：调用 append(line)，由内部负责着色与滚动。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('log')
        self.setReadOnly(True)
        self.setMaximumBlockCount(20000)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._t0 = time.perf_counter()

    def append(self, line: str) -> None:
        """追加一行：前缀为 mm:ss 耗时（浅灰），正文按标签着色。"""
        el = time.perf_counter() - self._t0
        stamp = f'{int(el // 60):02d}:{int(el % 60):02d}'
        body = html.escape(line)
        color = _TAG_COLOR.get(tag_of(line), _DEFAULT)
        self.appendHtml(
            f'<span style="color:{_STAMP}">{stamp}</span> '
            f'<span style="color:{color}">{body}</span>')
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

    def reset_clock(self) -> None:
        """每次开始运行时将耗时前缀归零。"""
        self._t0 = time.perf_counter()
