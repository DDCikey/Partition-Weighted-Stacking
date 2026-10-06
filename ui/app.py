# 20261006 DWT 主窗口 v3（控制台重设计）：深色顶栏 + 全宽页面 + 终端式监控
#
# 结构：
#   顶栏（深墨 #171e29，高 92）：teal 徽标 + 标题/副标题 + 居中分段导航
#     （素材 / 参数 / 高级 / 监控）+ 状态胶囊 + 圆形终止/运行钮；
#   内容区（浅灰 SOFT 底，左右留白 40）：素材页 = Drop 取景框 + 行卡清单；
#     参数/高级页 = 双列白卡网格；监控页 = 左侧圆环进度 + 右侧深色终端日志卡；
#   底栏：输出路径（左）与成品信息（右）。
#
# 动效：分段滑块滑动、控件 hover/press 过渡、进度值 lerp；换页即时全部显示
#   （逐卡水波浮现已移除——体感"一格一格跳出来"且切页时帧率骤降）。
#   定时器常转 33ms，只在有动画/内容变化时才重绘。
#
# 窗口几何与参数值持久化在 %APPDATA%\DWT\settings.json；
#   PARAMS_VER 变化时一次性把参数刷成新定稿默认值；
#   素材/输出/帧落盘目录（VOLATILE_KEYS）**不**持久化，每次从空白开始。
# 交互模型（接口文档 §17.6）：事件只到达宿主，宿主做矩形命中并转发给控件；
#   后台引擎经 worker 队列在定时器里 pump，不存在跨线程直接改 UI。

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent                 # DWT/ui
_DWT = _HERE.parent                                     # DWT
_ROOT = _DWT.parent                                     # 仓库根

from wevvmold import WevvMoldWindow, color              # noqa: E402
import style as S                                        # noqa: E402
import theme                                             # noqa: E402
from pws_params import PARAMS, PARAMS_VER, PwsParams, tip  # noqa: E402
from logview import LINE_H, LogModel                     # noqa: E402
from panel import TabPanel                               # noqa: E402
from worker import StackWorker                           # noqa: E402
from widgets import (Chip, ChoiceBox, Ring, RoundButton, Segmented,  # noqa: E402
                     SourceList, SpinEdit, TextEdit)

# 与"这一次要叠哪批数据"绑定的路径字段：不写进持久化设置。
# 否则下次启动会原样恢复上一批数据的目录，换新数据后成品仍落进旧目录（曾出现"换数据还是 6888"）。
VOLATILE_KEYS = frozenset({'photos', 'out', 'frames_dir'})

# 引擎报的阶段名 → 界面上给人看的一句话
PHASE_TEXT = {
    '读帧': '读取帧 · 曝光归一 · 天空平移',
    '星表': '建立固定星表',
    '测星点': '测帧间星点 FWHM（清晰度权重）',
    'R 场': '建分区权重场 R',
    '叠加': '加权叠加 · 逐条带',
    '验收': '验收与导出',
}

_HEADER_H = theme.HEADER_H        # 深色顶栏高 92
_CONTENT_TOP = 112                # 内容区上缘（顶栏下留 20 呼吸）
_FOOT_BAND = 44                   # 底部留给信息条的带高
_TIMER_ID = 1
_TICK_MS = theme.TICK_IDLE
_TICK_ANIM = theme.TICK_ANIM
_SEG_W = 400                      # 分段导航总宽


def mmss(sec: float) -> str:
    """秒 → mm:ss（超过一小时给 h:mm:ss）"""
    s = max(0, int(sec))
    if s >= 3600:
        return f'{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}'
    return f'{s // 60:02d}:{s % 60:02d}'


def settings_path() -> Path:
    base = os.environ.get('APPDATA') or str(Path.home())
    return Path(base) / 'DWT' / 'settings.json'


class MainWindow:
    """DWT 主窗口（WevvMold 宿主：布局 / 事件路由 / 渲染 / 状态机 / 动画）"""

    def __init__(self, hidden: bool = False):
        self._w, self._h = 1100, 740
        self._pos = (120, 80)
        self.win = None
        self.hidden = hidden

        # ---- 控件 ----
        self.chip = Chip('就绪', 'idle')
        self.btn_run = RoundButton('run', on_click=self.start)
        self.btn_stop = RoundButton('stop', on_click=self.cancel)
        self.btn_stop.enabled = False
        self.bar = Ring()
        self.seg = Segmented([name for name, _g in theme.TABS],
                             on_select=self._select_tab)
        self.panel = TabPanel()
        self.panel.on_layout_needed = self._relayout
        self.panel.on_show_tip = self._show_tip
        self.log = LogModel()

        # ---- 交互状态 ----
        self._widgets = [self.seg, self.btn_stop, self.btn_run, self.chip]
        self._focused: TextEdit | None = None
        self._pressed = None
        self._hover = None
        self._mouse = (-1.0, -1.0)
        self._drag = None            # 'panel' | 'log'
        self._scroll_off = 0.0
        self._tip = None             # (lines, x, y, w, h)
        self._tip_anchor = None      # 悬停计时的目标控件
        self._tip_t0 = 0.0
        self._src_row = -1

        # ---- 运行状态 ----
        self.worker: StackWorker | None = None
        self._prog = ('', 0.0, '')   # (阶段, 全局百分比, 引擎原文)
        self._detail_disp = '添加素材后点右下角 ▶ 开始叠加'
        self._t0 = 0.0
        self._auto_out = ''
        self._last_flush = 0.0
        self._dirty = True
        self._foot_info_text = 'XISF · Float32 线性 · —'

        # ---- 动画状态 ----
        self._tick_at = 0.0
        self._tick_cur = _TICK_MS

        self._load_settings()
        # 窗口用恢复后的几何创建
        self.win = WevvMoldWindow(title='DWT · Partition-Weighted Stacking',
                                  width=self._w, height=self._h,
                                  left=self._pos[0], top=self._pos[1])
        self.win.on_event = self._on_event
        self.win.on_render = self._on_render
        self._relayout()

    # ================================================================ 布局
    def _select_tab(self, i):
        self.panel.set_tab(i)
        self._relayout()
        self._redraw()

    def _relayout(self) -> None:
        w, h = self._w, self._h
        cx = theme.CONTENT_X
        content_bot = h - _FOOT_BAND
        self._content_bot = content_bot
        # ---- 顶栏控件 ----
        # 20261006 修正：此前四轮误改了按钮间水平间距，真实问题是控件行整体
        #   贴住顶栏下底边（run 底缘 y=92 恰为 HEADER_H）。现水平间距回滚原值，
        #   整行上移：run/stop 圆心 68→56（底缘 80，离底边 12px），
        #   胶囊 57..79→45..67，分段导航 48→40 起（页签药丸底缘离底边 14px）。
        self.seg.set_rect((w - _SEG_W) / 2, 40, (w + _SEG_W) / 2, 40 + theme.SEG_H)
        self.seg.clip = None
        self.btn_run.set_center(w - 44, 56, theme.RUN_D)
        self.btn_stop.set_center(w - 104, 56, 36)
        cw = self.chip.desired_w()
        self.chip.set_rect(w - 122 - 10 - cw, 45, w - 122 - 10, 67)
        # ---- 监控页几何 ----
        self._ring_cx = cx + 140
        self._ring_cy = _CONTENT_TOP + 142
        ro = theme.RING_OUT
        self.bar.set_rect(self._ring_cx - ro, self._ring_cy - ro,
                          self._ring_cx + ro, self._ring_cy + ro)
        self._log_l = cx + 340
        self._log_t = _CONTENT_TOP + 16
        self._log_r = w - cx
        self._log_b = content_bot
        # ---- 面板（含滚动偏移）----
        if self.panel._reset_scroll:
            self.panel._reset_scroll = False
            self._scroll_off = 0.0
        # 先摆一遍拿内容总高，钳制偏移后再摆一遍：内容高与偏移无关，
        # 但控件矩形依赖偏移——钳制前摆的那遍可能整体跑到视口外（拖滚动条过头时）
        prev_off = self._scroll_off
        self._panel_h = self.panel.layout(cx, _CONTENT_TOP, content_bot,
                                          w - 2 * cx, self._scroll_off)
        view_h = content_bot - self.panel.content_top
        max_off = max(0.0, self._panel_h - view_h)
        self._scroll_off = max(0.0, min(self._scroll_off, max_off))
        if self._scroll_off != prev_off:
            self.panel.layout(cx, _CONTENT_TOP, content_bot,
                              w - 2 * cx, self._scroll_off)
        self._clamp_log()
        self._dirty = True

    def _log_view(self):
        return self._log_t + 42, self._log_b - 12

    def _clamp_log(self):
        ly0, ly1 = self._log_view()
        view_h = ly1 - ly0
        content = self.log.content_h(view_h)
        max_off = max(0.0, content - view_h)
        if self.log.follow:
            self.log.scroll = max_off
        self.log.scroll = max(0.0, min(self.log.scroll, max_off))

    # ================================================================ 渲染
    def _on_render(self, rc):
        w, h = self._w, self._h
        rc.fill_rect(0, 0, w, h, S.SOFT)

        # ---- 深色顶栏 ----
        rc.fill_rect(0, 0, w, _HEADER_H, S.DARK)
        S.aa_round_rect(rc, 20, 22, 54, 56, 14, S.ACCENT)
        rc.draw_text('D', 20, 21, 54, 57, 20, S.WHITE, valign=1, halign=1)
        rc.draw_text('D', 20.4, 21, 54.4, 57, 20, S.WHITE, valign=1, halign=1)
        rc.draw_text('DWT · 深空叠加', 66, 12, 366, 38, S.FS_TITLE, S.D_TEXT,
                     valign=1)
        rc.draw_text('DWT · 深空叠加', 66.4, 12, 366.4, 38, S.FS_TITLE, S.D_TEXT,
                     valign=1)
        rc.draw_text('Partition-Weighted Stacking', 66, 38, 366, 58,
                     S.FS_SMALL, S.D_HINT, valign=1)
        self.seg.render(rc)
        self.chip.render(rc)
        self.btn_stop.render(rc)
        self.btn_run.render(rc)

        # ---- 内容区 ----
        if self.panel.current_group() is None:
            self._draw_monitor(rc, w)
        else:
            self.panel.draw_chrome(rc)
            for wd in self.panel.widgets:
                wd.render(rc)
            self._draw_panel_scrollbar(rc)

        # ---- 底栏 ----
        cx = theme.CONTENT_X
        fy = h - _FOOT_BAND + 12
        rc.draw_text(self._foot_path(), cx, fy, w - cx - 220, fy + 20,
                     S.FS_BODY, S.TEXT2, valign=1)
        rc.draw_text(self._foot_info(), cx, fy, w - cx, fy + 20,
                     S.FS_BODY, S.HINT, valign=1, halign=2)

        # ---- 悬停说明浮层（最上层）----
        self._draw_tip(rc)

    def _draw_monitor(self, rc, w):
        cx, cy = self._ring_cx, self._ring_cy
        # 圆环 + 中央大字
        self.bar.render(rc)
        rc.draw_text(self._pct_text(), cx - 90, cy - 28, cx + 90, cy + 28,
                     S.FS_HUGE, S.ACCENT, valign=1, halign=1)
        rc.draw_text(self._pct_text(), cx - 90 + 0.5, cy - 28, cx + 90 + 0.5,
                     cy + 28, S.FS_HUGE, S.ACCENT, valign=1, halign=1)
        rc.draw_text(self._phase_text(), cx - 150, cy + 134, cx + 150, cy + 156,
                     S.FS_TITLE2, S.TEXT, valign=1, halign=1)
        rc.draw_text(S.elide_middle(self._detail_text(), S.FS_BODY, 300),
                     cx - 150, cy + 158, cx + 150, cy + 176,
                     S.FS_BODY, S.HINT, valign=1, halign=1)
        # 终端日志卡
        l, t, r, b = self._log_l, self._log_t, self._log_r, self._log_b
        if r - l > 120 and b - t > 60:
            S.round_rect(rc, l, t, r, b, theme.R_CARD, S.DARK)
            rc.draw_text('控制台', l + 20, t + 10, l + 90, t + 32,
                         S.FS_TITLE2, S.D_TEXT2, valign=1)
            rc.draw_text(f'{len(self.log.lines)} 行', l + 90, t + 12, r - 20,
                         t + 32, S.FS_SMALL, S.D_HINT, valign=1, halign=2)
            rc.fill_rect(l + 16, t + 38, r - 16, t + 39, S.D_TRACK)
            lx0, ly0 = l + 20, t + 42
            lx1, ly1 = r - 32, b - 12
            n = len(self.log.lines)
            first = max(0, int(self.log.scroll // LINE_H) - 1)
            last = min(n, int((self.log.scroll + (ly1 - ly0)) // LINE_H) + 2)
            for i in range(first, last):
                stamp, txt, col = self.log.lines[i]
                y = ly0 + i * LINE_H - self.log.scroll + 4
                if y < ly0 or y + LINE_H > ly1:      # 部分越界的行不画（无裁剪区）
                    continue
                rc.draw_text(stamp, lx0, y, lx0 + 38, y + LINE_H, S.FS_SMALL,
                             S.D_HINT, valign=1)
                rc.draw_text(S.elide_middle(txt, S.FS_BODY, lx1 - lx0 - 44),
                             lx0 + 44, y, lx1, y + LINE_H, S.FS_BODY, col,
                             valign=1)
            # 日志滚动条（浅色页面右缘之外没有底，画在卡内右缘）
            view_h = ly1 - ly0
            content = self.log.content_h(view_h)
            max_off = content - view_h
            if max_off > 0:
                x = r - 14
                th = max(24.0, view_h / content * (ly1 - ly0))
                ty = ly0 + (ly1 - ly0 - th) * (self.log.scroll / max_off)
                col = S.D_TEXT2 if self._drag == 'log' else S.D_TRACK
                S.round_rect(rc, x, ty, x + 5, ty + th, 2.5, col)

    def _draw_panel_scrollbar(self, rc):
        top = self.panel.content_top
        view_h = self._content_bot - top
        max_off = self._panel_h - view_h
        if max_off <= 0:
            return
        x = self._w - theme.CONTENT_X + 10
        t, b = top + 3, self._content_bot - 3
        th = max(24.0, view_h / self._panel_h * (b - t))
        ty = t + (b - t - th) * (self._scroll_off / max_off)
        col = S.SB_HOVER if (self._drag == 'panel' or
                              self._sb_hover(x, t, x + 6, b, ty, th)) else S.SB_HANDLE
        S.round_rect(rc, x, ty, x + 6, ty + th, 3, col)

    def _sb_hover(self, l, t, r, b, ty, th) -> bool:
        mx, my = self._mouse
        return l <= mx <= r and ty <= my <= ty + th

    def _draw_tip(self, rc):
        if not self._tip:
            return
        lines, x, y, w, h = self._tip
        S.shadow(rc, x, y, x + w, y + h, 6, alpha=40)
        S.round_rect_border(rc, x, y, x + w, y + h, 6, S.BORDER, S.CARD)
        ty = y + 6
        for ln in lines:
            rc.draw_text(ln, x + 8, ty, x + w - 8, ty + 17, S.FS_BODY, S.TEXT,
                         valign=1)
            ty += 17

    # ---- 显示文本 ----
    def _pct_text(self) -> str:
        return f'{self._prog[1] * 100:.0f}%'

    def _phase_text(self) -> str:
        if not self._prog[0]:
            return '等待开始'
        return PHASE_TEXT.get(self._prog[0], self._prog[0])

    def _detail_text(self) -> str:
        return self._detail_disp

    def _foot_path(self) -> str:
        try:
            p = self.panel.values()
        except Exception:
            return '输出：—'
        out = (p.out or '').strip()
        return f'输出：{out}' if out else '输出：—（留空将自动放在素材目录旁）'

    def _foot_info(self) -> str:
        return getattr(self, '_foot_info_text', 'XISF · Float32 线性 · —')

    # ================================================================ 事件
    def _on_event(self, ev):
        t = ev.get('type')
        E = WevvMoldWindow.EVENT
        if t == E['RESIZED']:
            self._w, self._h = ev['width'], ev['height']
            self._relayout()
        elif t == E['MOVED']:
            self._pos = (ev.get('x', self._pos[0]), ev.get('y', self._pos[1]))
        elif t == E['CLOSE_REQUESTED']:
            self._shutdown()
        elif t == E['POINTER_MOVE']:
            self._on_move(ev['x'], ev['y'])
        elif t == E['POINTER_BUTTON_DOWN'] and ev.get('button', 1) == 1:
            self._on_down(ev['x'], ev['y'])
        elif t == E['POINTER_BUTTON_UP'] and ev.get('button', 1) == 1:
            self._on_up(ev['x'], ev['y'])
        elif t == E['POINTER_WHEEL']:
            self._on_wheel(ev['x'], ev['y'], ev.get('wheel_delta', 0))
        elif t == E['KEY_DOWN']:
            self._on_key(ev)
        elif t == E['CHAR_INPUT']:
            self._on_char(ev)
        elif t == E['TIMER_TICK'] and ev.get('timer_id') == _TIMER_ID:
            self._on_tick(self._tick_cur)

    def _hit(self, x, y):
        """命中：顶栏控件优先；面板控件限在内容视口内（页签切换后非当前页不可点）"""
        for wd in reversed(self._widgets):
            if wd.interactive and wd.contains(x, y):
                return wd
        in_content = _CONTENT_TOP <= y <= self._content_bot
        for wd in reversed(self.panel.widgets):
            if not wd.interactive or not wd.contains(x, y):
                continue
            if not in_content and not (isinstance(wd, ChoiceBox) and wd.open):
                continue
            return wd
        return None

    def _on_move(self, x, y):
        self._mouse = (x, y)
        if self._drag == 'panel':
            self._scroll_off = self._drag_off + (y - self._drag_y0) * self._drag_rate
            self._relayout()
            self._redraw()
            return
        if self._drag == 'log':
            ly0, ly1 = self._log_view()
            view_h = ly1 - ly0
            content = self.log.content_h(view_h)
            max_off = max(1.0, content - view_h)
            self.log.scroll = self._drag_off + (y - self._drag_y0) * (max_off / max(1.0, view_h))
            self.log.follow = False
            self._clamp_log()
            self._redraw()
            return
        if self._pressed is not None and isinstance(self._pressed, TextEdit):
            self._pressed.on_drag(x, y)
            self._redraw()
            return
        wd = self._hit(x, y)
        need = False
        if wd is not self._hover:
            if self._hover is not None:
                self._hover.on_leave()
            self._hover = wd
            if wd is not None:
                wd.on_move(x, y)
            self._tip_anchor = wd if (wd is not None and getattr(wd, 'tip', None)) else None
            self._tip_t0 = time.perf_counter()
            need = True
        elif wd is not None:
            wd.on_move(x, y)
            if isinstance(wd, SourceList):
                row = wd._hover_row
                if row != self._src_row:
                    self._src_row = row
                    self._tip_anchor = wd if row >= 0 else None
                    self._tip_t0 = time.perf_counter()
                    need = True
        # 只在命中目标真正变化时重绘：鼠标 125Hz 事件每个都全场景重绘是卡顿主因；
        # 悬停后的渐变动画由 _on_tick 的 anim 通道驱动
        if need:
            self._redraw()

    def _on_down(self, x, y):
        # 滚动条优先：面板（内容右缘外侧）、日志（监控页卡内右缘）
        sb_x = self._w - theme.CONTENT_X + 8
        if (self.panel.current_group() is not None and
                self.panel.content_top < y < self._content_bot and
                sb_x <= x <= sb_x + 10):
            max_off = self._panel_h - (self._content_bot - self.panel.content_top)
            if max_off > 0:
                self._drag = 'panel'
                self._drag_y0 = y
                self._drag_off = self._scroll_off
                self._drag_rate = 1.0
                self._redraw()
                return
        if (self.panel.current_group() is None and
                self._log_t + 42 < y < self._log_b and
                self._log_r - 18 <= x <= self._log_r - 4):
            self._drag = 'log'
            self._drag_y0 = y
            self._drag_off = self.log.scroll
            self._redraw()
            return
        # 浮层外点击关闭
        if self._tip:
            tx, ty, tw, th = self._tip[1], self._tip[2], self._tip[3], self._tip[4]
            if not (tx <= x <= tx + tw and ty <= y <= ty + th):
                self._tip = None
        wd = self._hit(x, y)
        if isinstance(wd, TextEdit) and self._focused is not wd:
            self._defocus()
        if wd is not None and wd.enabled:
            if wd.on_press(x, y):
                self._pressed = wd
                if isinstance(wd, TextEdit):
                    self._focused = wd      # on_press 内部已置 focused=True
                self._redraw()
                return
        self._pressed = None
        if not isinstance(wd, TextEdit):
            self._defocus()
        self._redraw()

    def _on_up(self, x, y):
        if self._drag:
            self._drag = None
            self._redraw()
            return
        wd = self._pressed
        self._pressed = None
        if wd is not None:
            wd.on_release(x, y)
            if isinstance(wd, SpinEdit) and not wd.focused:
                wd.commit_now()
            self._relayout()
            self._redraw()

    def _on_wheel(self, x, y, delta):
        wd = self._hit(x, y)
        if isinstance(wd, SpinEdit) and wd.enabled:
            wd.on_wheel(x, y, delta)
            self._redraw()
            return
        if self.panel.current_group() is None:
            if (self._log_l <= x <= self._log_r and
                    self._log_t <= y <= self._log_b):
                ly0, ly1 = self._log_view()
                view_h = ly1 - ly0
                content = self.log.content_h(view_h)
                self.log.scroll -= delta / 120.0 * 60.0
                self.log.follow = self.log.scroll >= content - view_h - 1
                self._clamp_log()
                self._redraw()
        elif theme.CONTENT_TOP - 20 <= x <= self._w - 4 and \
                _CONTENT_TOP <= y <= self._content_bot:
            self._scroll_off = max(0.0, self._scroll_off - delta / 120.0 * 60.0)
            self._relayout()
            self._redraw()

    def _defocus(self):
        if self._focused is not None:
            self._focused.focused = False
            if isinstance(self._focused, SpinEdit):
                self._focused.commit_now()
            self._focused = None

    def _on_key(self, ev):
        if self._focused is not None and self._focused.on_key(ev):
            self._redraw()
            return
        if ev.get('key_code') == 27:        # ESC 关浮层
            self._tip = None
            self._redraw()

    def _on_char(self, ev):
        if self._focused is not None and self._focused.on_char(ev):
            self._redraw()

    # ---- 悬停说明 ----
    def _show_tip(self, prm, rect):
        self._tip = self._make_tip(tip(prm), rect)

    def _make_tip(self, text, rect):
        w = min(380.0, max(200.0, self._w - 40))
        lines = S.wrap_text(text, S.FS_BODY, w - 16)
        h = len(lines) * 17 + 12
        x = max(14, min(rect[0] - 40, self._w - w - 14))
        y = rect[3] + 4
        if y + h > self._h - 8:
            y = max(_HEADER_H, rect[1] - h - 4)
        return (lines, x, y, w, h)

    # ================================================================ 定时器
    def _all_widgets(self):
        return self._widgets + [self.bar] + self.panel.widgets

    def _on_tick(self, dt):
        now = time.perf_counter()
        dt_ms = min(250.0, (now - self._tick_at) * 1000.0) if self._tick_at \
            else float(dt)
        self._tick_at = now

        # 引擎消息
        if self.worker is not None:
            for msg in self.worker.pump():
                kind = msg[0]
                if kind == 'log':
                    self.log.append(msg[1])
                    self._clamp_log()
                    self._dirty = True
                elif kind == 'progress':
                    self._prog = (msg[1], msg[2], msg[3])
                elif kind == 'done':
                    self._on_done(msg[1])
                elif kind == 'cancelled':
                    self._on_cancelled()
                elif kind == 'error':
                    self._on_error(msg[1])
        # 进度约每 0.7s 刷一次，肉眼是"跳动"
        if self.worker is not None and now - self._last_flush >= 0.7:
            self._flush_progress(now)

        # 控件动画（换页水波已移除：切页即时全部显示）
        # 20261006 修"最后一帧不渲染"：_approach 在本帧把动画量推到终点的
        #   同一拍 anim_active() 已变 False，终帧若不重绘画面就停在差一帧的
        #   位置（滑块到位要等鼠标移动）。tick 前记旧态，刚结束的这帧仍算动画。
        anim = False
        for wd in self._all_widgets():
            was = wd.anim_active()
            wd.tick(dt_ms)
            if wd.anim_active() or was:
                anim = True

        # 悬停说明：500ms 后自动弹出
        if self._tip is None and self._tip_anchor is not None and \
                now - self._tip_t0 >= 0.5:
            wd = self._tip_anchor
            if isinstance(wd, SourceList) and wd._hover_row >= 0:
                self._tip = self._make_tip(wd.sources[wd._hover_row], wd.rect)
            elif getattr(wd, 'tip_param', None) is not None:
                self._show_tip(wd.tip_param, wd.rect)
            elif getattr(wd, 'tip', None):
                self._tip = self._make_tip(wd.tip, wd.rect)
            self._tip_anchor = None
            self._dirty = True

        # 有动画时把定时器切到快周期，动画结束回到泵消息的慢周期
        desired = _TICK_ANIM if anim else _TICK_MS
        if desired != self._tick_cur and self.win is not None:
            self._tick_cur = desired
            try:
                self.win.start_timer(_TIMER_ID, desired)
            except Exception:
                pass

        if anim or self._dirty:
            self._dirty = False
            self._redraw()

    def _redraw(self):
        if self.win is None:
            return
        try:
            self.win.request_redraw()
        except RuntimeError:
            pass

    # ================================================================ 状态
    def _set_state(self, state: str, text: str) -> None:
        self.chip.set_state(state, text)
        self._dirty = True

    def _fail(self, msg: str) -> None:
        """开跑前的拦截：红字进日志，不建任何文件"""
        self.log.append(f'[错误] {msg}')
        self._set_state('err', '错误')
        self._clamp_log()
        self._redraw()

    # ================================================================ 运行
    def start(self) -> None:
        p = self.panel.values()
        sources = [s.strip() for s in (p.photos or '').split(';') if s.strip()]
        if not sources:
            self._fail('请先添加素材（点取景框添加目录，或「添加单张」）')
            return
        for s in sources:
            path = Path(s)
            if not path.exists():
                self._fail(f'素材不存在：{s}')
                return
            if path.is_dir():
                if not (next(path.glob('*.xisf'), None) or next(path.glob('*.fit*'), None)):
                    self._fail(f'目录里没有 XISF / FITS 帧：{s}')
                    return
            elif path.suffix.lower() not in ('.xisf', '.fit', '.fits', '.fts'):
                self._fail(f'不支持的帧格式：{path.name}')
                return
        # 输出目录：留空，或上次是自动推导的（换素材就跟着换），都按当前素材重推。
        # 用户手动填过的自定义目录会被保留（它不等于上次自动推导值）。
        if not (p.out or '').strip() or p.out == self._auto_out:
            # 不留空跑不了：默认放第一个来源**旁边**，避免成品被下一轮当成帧读进来
            first = Path(sources[0])
            anchor = first if first.is_dir() else first.parent
            p.out = str(anchor.parent / f'{anchor.name}_DWT')
            self._auto_out = p.out
            self.panel.set_values(p)

        self.log.reset_clock()
        self.log.clear()
        self.log.append(f'[帧] 素材 {len(sources)} 项：{"; ".join(sources)}')
        self.log.append(f'[输出] 成品目录 {p.out}')
        self._prog = ('读帧', 0.0, '准备')
        self._detail_disp = '已用 00:00'
        self._t0 = time.perf_counter()
        self.bar.reset()
        self._dirty = True
        self.panel.set_locked(True)
        self.btn_run.enabled = False
        self.btn_stop.enabled = True
        self._set_state('run', '运行中')
        self._clamp_log()
        self.seg.set_index(3)          # 自动切到监控页

        self.worker = StackWorker(p)
        self._last_flush = time.perf_counter()
        self._tick_at = 0.0
        self.win.start_timer(_TIMER_ID, self._tick_cur)
        self.worker.start()
        self._redraw()

    def cancel(self) -> None:
        """只置取消位：引擎在检查点退出并清掉临时帧文件"""
        if self.worker is not None and self.worker.is_alive():
            self.btn_stop.enabled = False
            self.log.append('[终止] 已请求终止，等当前步骤结束…')
            self.worker.cancel()
            self._dirty = True
            self._redraw()

    def _flush_progress(self, now: float) -> None:
        self._last_flush = now
        phase, frac, text = self._prog
        if not phase:
            return
        self.bar.set_value(frac)
        el = time.perf_counter() - self._t0
        eta = ''
        if 0.03 <= frac < 1.0:
            eta = f' · 预计剩余 {mmss(el * (1.0 - frac) / frac)}'
        self._detail_disp = f'{text} · 已用 {mmss(el)}{eta}'
        self._dirty = True

    def _settle(self) -> None:
        """收尾：恢复可操作"""
        self._flush_progress(time.perf_counter())
        self.panel.set_locked(False)
        self.btn_run.enabled = True
        self.btn_stop.enabled = False
        self._dirty = True

    def _on_done(self, res: dict) -> None:
        self._prog = ('验收', 1.0, '完成')
        self.worker = None
        self._settle()
        h, w = res['shape']
        self.log.append(f'[验收] 成品 {w}×{h}  星点 FWHM {res["fwhm_out"]:.2f}px'
                        f'（单帧中位 {res["fwhm_med"]:.2f}px）  '
                        f'排异剔除率 {res["rej_rate"]:.3%}')
        self._foot_info_text = f'XISF · Float32 线性 · {w}×{h}'
        self._set_state('done', '完成')
        self._clamp_log()
        self._redraw()

    def _on_cancelled(self) -> None:
        self._prog = ('', 0.0, '已终止')
        self.worker = None
        self._settle()
        self.log.append('[终止] 已终止；临时帧文件已清理')
        self._set_state('idle', '已终止')
        self._clamp_log()
        self._redraw()

    def _on_error(self, text: str) -> None:
        self.worker = None
        self._settle()
        for line in (text or '').rstrip().splitlines() or ['未知错误']:
            self.log.append(f'[错误] {line}')
        self._set_state('err', '错误')
        self._clamp_log()
        self._redraw()

    # ================================================================ 持久化
    def _load_settings(self) -> None:
        """恢复几何与参数；PARAMS_VER 变了就整体回定稿默认值"""
        data = {}
        try:
            f = settings_path()
            if f.exists():
                data = json.loads(f.read_text(encoding='utf-8'))
        except Exception:
            data = {}
        geo = data.get('geometry')
        if isinstance(geo, dict):
            self._pos = (int(geo.get('left', 120)), int(geo.get('top', 80)))
            self._w = max(860, int(geo.get('width', 1100)))
            self._h = max(560, int(geo.get('height', 740)))
        p = PwsParams()
        if data.get('params_ver') == PARAMS_VER:
            saved = data.get('params') or {}
            for prm in PARAMS:
                if prm.key in VOLATILE_KEYS or prm.key not in saved:
                    continue
                v = saved[prm.key]
                try:
                    if prm.kind == 'float':
                        v = float(v)
                    elif prm.kind == 'int':
                        v = int(v)
                    elif prm.kind == 'check':
                        v = bool(v)
                    else:
                        v = str(v)
                except (TypeError, ValueError):
                    continue
                setattr(p, prm.key, v)
        self.panel.set_values(p)
        self._params_ver = PARAMS_VER

    def _save_settings(self) -> None:
        try:
            f = settings_path()
            f.parent.mkdir(parents=True, exist_ok=True)
            p = self.panel.values()
            params = {prm.key: getattr(p, prm.key) for prm in PARAMS
                      if prm.key not in VOLATILE_KEYS}
            data = {'geometry': {'left': self._pos[0], 'top': self._pos[1],
                                 'width': self._w, 'height': self._h},
                    'params_ver': PARAMS_VER, 'params': params}
            f.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                         encoding='utf-8')
        except Exception:
            pass

    def _shutdown(self) -> None:
        """关窗前先停引擎，别把 memmap 临时文件丢在盘上"""
        w = self.worker
        if w is not None:
            try:
                if w.is_alive():
                    w.cancel()
                    w.join(8.0)
            except RuntimeError:
                pass
        self._save_settings()
        self.win.request_close()

    # ================================================================ 入口
    def show_and_run(self) -> int:
        if not self.hidden:
            self.win.show()
            # 空闲也要有 tick：hover 过渡、分段滑块、悬停说明全靠 _on_tick 驱动
            self.win.start_timer(_TIMER_ID, _TICK_MS)
        return self.win.run()


def main(argv=None) -> int:
    """GUI 入口（命令行入口在 core/pws.py，两者共用同一份参数定义）"""
    win = MainWindow()
    return win.show_and_run()


if __name__ == '__main__':
    raise SystemExit(main())
