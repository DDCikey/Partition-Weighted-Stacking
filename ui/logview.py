# 20261005 DWT 日志模型：按阶段标签着色（WevvMold 自绘版）
#
# 引擎每行日志以 [阶段] 开头（[帧] [星表] [C] [R] [W] [叠加] [验收] …），
#   这里据此给该行上色。着色只分四类，不做彩虹：
#     读帧/落盘类 → 提示灰      权重与分区类 → 强调青绿
#     结果与输出类 → 蓝          错误与终止 → 红
# 不指定任何字体族（只用系统默认字体）。
# 本模块只存数据与着色；绘制与滚动由 app.py 的日志区负责（虚拟渲染，只画可见行）。

from __future__ import annotations

import time

import style as S

# 阶段标签 → 颜色（监控页是深底终端，全部用亮色变体）
_TAG_COLOR = {
    # 读帧 / 落盘 / 杂项
    '帧': S.D_HINT, 'memmap': S.D_HINT, '归一': S.D_HINT, '并行': S.D_HINT,
    '裁剪': S.D_HINT, '时': S.D_HINT,
    # 权重与分区
    '星表': S.D_ACCENT, 'C': S.D_ACCENT, 'S': S.D_ACCENT, 'σ': S.D_ACCENT,
    'R': S.D_ACCENT, 'W': S.D_ACCENT,
    # 结果与输出
    '叠加': S.D_BLUE, '读数': S.D_BLUE, '验收': S.D_BLUE, '输出': S.D_BLUE,
    # 异常
    '错误': S.D_RED, '终止': S.D_RED,
}
_DEFAULT = S.D_TEXT
_STAMP = S.D_HINT

LINE_H = 18
MAX_LINES = 20000


def tag_of(line: str) -> str:
    """取行首 [xxx] 里的标签；没有则返回空串"""
    s = line.lstrip()
    if not s.startswith('['):
        return ''
    end = s.find(']')
    return s[1:end] if end > 0 else ''


class LogModel:
    """只读日志：append(line) 即可；每行 = (mm:ss, 正文, 颜色)"""

    def __init__(self):
        self.lines: list = []
        self._t0 = time.perf_counter()
        self.scroll = 0.0        # 0 = 顶部；由 app 钳制
        self.follow = True       # 自动贴底

    def reset_clock(self) -> None:
        """每次开跑把耗时前缀归零"""
        self._t0 = time.perf_counter()

    def clear(self) -> None:
        self.lines = []
        self.scroll = 0.0
        self.follow = True

    def append(self, line: str) -> None:
        el = time.perf_counter() - self._t0
        stamp = f'{int(el // 60):02d}:{int(el % 60):02d}'
        color = _TAG_COLOR.get(tag_of(line), _DEFAULT)
        self.lines.append((stamp, line, color))
        if len(self.lines) > MAX_LINES:
            del self.lines[:len(self.lines) - MAX_LINES]
        self.follow = True

    def plain_lines(self):
        """供自检用：'mm:ss 正文' 列表"""
        return [f'{st} {txt}' for st, txt, _c in self.lines]

    def content_h(self, view_h: float) -> float:
        return max(view_h, len(self.lines) * LINE_H + 8)
