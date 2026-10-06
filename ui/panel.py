# 20261006 DWT 参数面板 v3（控制台重设计）：全宽四页视图
#
# 页签由 app 顶栏的分段导航驱动（panel.set_tab），本文件只管页面内容：
#   素材页 = 取景框 Drop 区 + 行卡来源清单 + 输出/成品名双列卡；
#   参数 / 高级页 = 双列参数卡网格（卡内：左上标签、右上控件、底部一句说明）；
#   监控页 = 无控件（圆环与终端日志由 app 渲染）。
#
# 控件仍全部由 pws_params.PARAMS 生成 —— **不允许在这里手写任何一个参数**。
# 对 app 的契约不变：editors[group][key] / values() / set_values() /
#   set_locked() / pick_dir() / layout()→content_h / draw_chrome / widgets /
#   set_tab / current_group / on_layout_needed / on_show_tip / _reset_scroll。
#
# 可见性模型：布局时给控件设 clip，基类要求**完全**落视口才绘制（无裁剪区）；
#   非当前页控件 visible=False。换页即全部显示，不做逐卡浮现动画
#   （水波动画体感是"一格一格跳出来"且每帧刷可见性开销大，20261006 移除）。

from __future__ import annotations

from typing import Dict, List

import style as S
import theme
from pws_params import GROUP_ORDER, PARAMS, PwsParams, params_of, tip
from widgets import (Button, ChoiceBox, DropZone, SourceList, SpinEdit,
                     TextEdit, Toggle)

KEY_OF = {p.key: p for p in PARAMS}

_SPIN_W = 150              # 数值控件宽
_SPIN_H = 32               # 数值控件高
_EDIT_H = 28               # 文本/目录编辑框高
_BTN_H = 26                # 小按钮高


class TabPanel:
    """四页全宽面板：读写 PwsParams + 运行中整体锁定（换页即时显示）"""

    def __init__(self):
        self.editors: Dict[str, Dict[str, object]] = {}
        self.browse: Dict[str, list] = {}
        self.help_btns: Dict[str, Button] = {}
        self._row_btn: Dict[str, Button] = {}
        self._chrome = []              # (prm, l, t, r, b) 参数卡
        self._view = (0, 0, 0, 0)
        self.content_top = 0.0
        self.content_h = 0.0
        self.locked = False
        self.tab_index = 0
        self._reset_scroll = False

        self.widgets: List = []
        self.dropzone = None
        self.sl = None

        # app 注入的回调
        self.on_layout_needed = lambda: None
        self.on_show_tip = lambda prm, rect: None

        for group in GROUP_ORDER:
            self.editors[group] = {}
            self.browse[group] = []
            for prm in params_of(group):
                w = self._make_editor(prm)
                w._prm_key = prm.key
                w._group = group
                self.editors[group][prm.key] = w
                self.widgets.append(w)
                if prm.kind == 'sources':
                    self.sl = w
                    self.dropzone = DropZone(on_pick=w._pick_dir)
                    self.dropzone._group = group
                    self.widgets.append(self.dropzone)
                    for b in w.buttons:
                        b._prm_key = prm.key
                        b._group = group
                        self.widgets.append(b)
                if prm.kind == 'dir':
                    btn = Button('…', 'browse',
                                 on_click=lambda k=prm.key: self.pick_dir(k))
                    btn.tip = '选择目录'
                    btn._prm_key = prm.key
                    btn._group = group
                    self.browse[group].append(btn)
                    self._row_btn[prm.key] = btn
                    self.widgets.append(btn)
                hb = Button('?', 'small',
                            on_click=lambda p=prm: self._ask_tip(p))
                hb.is_help = True
                hb._prm_key = prm.key
                hb._group = group
                hb.tip = tip(prm)
                hb.tip_param = prm
                self.help_btns[prm.key] = hb
                self.widgets.append(hb)

    # ---------------------------------------------------------------- 页签
    def set_tab(self, i):
        if i == self.tab_index:
            return
        self.tab_index = i
        self._reset_scroll = True
        self.on_layout_needed()

    def current_group(self):
        return theme.TABS[self.tab_index][1]

    def _make_editor(self, prm):
        if prm.kind == 'sources':
            return SourceList(on_change=lambda: self.on_layout_needed())
        if prm.kind == 'float':
            return SpinEdit(float(prm.lo), float(prm.hi), float(prm.step), prm.dec)
        if prm.kind == 'int':
            return SpinEdit(float(prm.lo), float(prm.hi), float(prm.step), 0,
                            is_int=True)
        if prm.kind == 'choice':
            return ChoiceBox(prm.choices)
        if prm.kind == 'check':
            return Toggle()
        return TextEdit()

    def _ask_tip(self, prm):
        hb = self.help_btns.get(prm.key)
        self.on_show_tip(prm, hb.rect if hb else (0, 0, 0, 0))

    def pick_dir(self, key: str) -> None:
        """「…」按钮：选目录写回编辑框（tkinter 桥，与 SourceList 同一套）"""
        from pathlib import Path
        prm = KEY_OF[key]
        w = self.editors[prm.group][key]
        start = ''
        try:
            cur = (w.text() or '').strip().strip('"')
            if cur and Path(cur).is_dir():
                start = cur
        except Exception:
            pass
        try:
            import tkinter
            from tkinter import filedialog
            root = tkinter.Tk()
            root.withdraw()
            path = filedialog.askdirectory(title=prm.label,
                                           initialdir=start or None)
            root.destroy()
            if path:
                w.set_text(path)
        except Exception:
            pass

    # ---------------------------------------------------------------- 布局
    def layout(self, x, view_top, view_bot, width, offset):
        """摆好当前页控件；offset=内容区滚动量；返回内容总高"""
        self._view = (x, view_top, x + width, view_bot)
        r = x + width
        # 内容起点比视口上缘低 10px：clip 判定要求"完全落视口"（+2 容差），
        # 贴着 view_top 的首行卡片会被判定越界而不绘制
        top = view_top + 10
        self.content_top = top
        clip = (x, view_top, r, view_bot)
        self._chrome = []

        group = self.current_group()
        if group is None:                       # 监控页：内容由 app 画
            self._apply_visibility(group, clip)
            self.content_h = 0.0
            return self.content_h

        cy = top - offset
        if group == '基础':
            cy = self._layout_sources(x, cy, r, clip)
            cards = [p for p in params_of(group) if p.kind != 'sources']
            cy = self._layout_cards(cards, x, cy + 18, r, clip)
        else:
            cy = self._layout_cards(params_of(group), x, cy, r, clip)

        total = cy + offset - top + 16
        self.content_h = max(total, view_bot - top)
        self._apply_visibility(group, clip)
        return self.content_h

    def _layout_sources(self, x, cy, r, clip) -> float:
        """素材页顶部：Drop 取景框 + 增删按钮行 + 行卡清单"""
        dz_h = 210
        dz = self.dropzone
        dz.set_rect(x, cy, r, cy + dz_h)
        dz.clip = clip
        dz.count = len(self.sl.sources)
        cy += dz_h + 16
        bx = x
        for b in self.sl.buttons:
            bwd = b.desired_w(20)
            b.set_rect(bx, cy, bx + bwd, cy + _BTN_H)
            b.clip = clip
            bx += bwd + 8
        cy += _BTN_H + 12
        sl = self.sl
        n = len(sl.sources)
        h = sl.content_h()
        sl.set_rect(x, cy, r, cy + h)
        sl.clip = clip
        return cy + h if n else cy

    def _layout_cards(self, prms, x, cy, r, clip) -> float:
        """双列卡片网格；返回网格底边 y"""
        width = r - x
        cw = (width - theme.CARD_GAP) / 2
        for i, prm in enumerate(prms):
            col, row = i % 2, i // 2
            l = x + col * (cw + theme.CARD_GAP)
            t = cy + row * (theme.CARD_H + theme.CARD_GAP)
            rr, b = l + cw, t + theme.CARD_H
            self._chrome.append((prm, l, t, rr, b))
            self._place_card_ctrls(prm, l, t, rr, b, clip)
        rows = (len(prms) + 1) // 2
        return cy + rows * (theme.CARD_H + theme.CARD_GAP) - theme.CARD_GAP

    def _place_card_ctrls(self, prm, l, t, rr, b, clip):
        w = self.editors[prm.group][prm.key]
        hb = self.help_btns[prm.key]
        w.clip = clip
        hb.clip = clip
        kind = prm.kind
        if kind in ('float', 'int', 'choice', 'check'):
            ctl_w = _SPIN_W if kind != 'check' else Toggle.W
            w.set_rect(rr - 18 - ctl_w, t + 12, rr - 18, t + 12 + _SPIN_H)
            lw = S.text_w(prm.label, S.FS_TITLE2)
            hb.set_rect(l + 18 + lw + 8, t + 14, l + 18 + lw + 30, t + 36)
        else:                                   # dir / text
            hb.set_rect(rr - 40, t + 12, rr - 18, t + 34)
            ex1 = rr - 18
            if prm.kind == 'dir':
                btn = self._row_btn[prm.key]
                btn.set_rect(rr - 18 - 38, t + 44, rr - 18, t + 44 + _EDIT_H)
                btn.clip = clip
                ex1 = rr - 18 - 38 - 8
            w.set_rect(l + 18, t + 44, ex1, t + 44 + _EDIT_H)

    def _apply_visibility(self, group, clip):
        top, bot = clip[1], clip[3]
        for w in self.widgets:
            if group is None or getattr(w, '_group', None) != group:
                w.visible = False
                continue
            l, t, r, b = w.rect
            w.visible = (t >= top + 2 and b <= bot - 2)

    # ---------------------------------------------------------------- 绘制
    def draw_chrome(self, rc):
        """参数卡：白底圆角 + 标签 + 一句说明（控件自己渲染）"""
        top, bot = self._view[1], self._view[3]
        for prm, l, t, r, b in self._chrome:
            if t < top + 2 or b > bot - 2:
                continue
            S.shadow(rc, l, t, r, b, 12, alpha=14)
            S.round_rect_border(rc, l, t, r, b, 12, S.BORDER, S.CARD)
            fg = S.TEXT2 if not self.locked else S.DISABLED
            rc.draw_text(prm.label, l + 18, t + 12, r - 18, t + 34,
                         S.FS_TITLE2, fg, valign=1)
            if prm.kind in ('float', 'int', 'choice', 'check'):
                rc.draw_text(S.elide_middle(prm.desc, S.FS_SMALL, r - l - 36),
                             l + 18, t + 54, r - 18, t + 72, S.FS_SMALL,
                             S.HINT, valign=1)

    # ---------------------------------------------------------------- 读写
    def values(self) -> PwsParams:
        """界面 → PwsParams（字段名与 Param.key 一一对应）"""
        data = {}
        for group, eds in self.editors.items():
            for key, w in eds.items():
                data[key] = read_editor(KEY_OF[key], w)
        return PwsParams(**data)

    def set_values(self, p: PwsParams) -> None:
        """PwsParams → 界面"""
        for group, eds in self.editors.items():
            for key, w in eds.items():
                write_editor(KEY_OF[key], w, getattr(p, key))
        self._reset_scroll = True
        self.on_layout_needed()

    def set_locked(self, locked: bool) -> None:
        """运行中锁参数（页签与「?」说明保持可用，方便边跑边看定义）"""
        self.locked = locked
        for eds in self.editors.values():
            for w in eds.values():
                w.enabled = not locked
                if isinstance(w, TextEdit):
                    w.focused = False
        for btns in self.browse.values():
            for b in btns:
                b.enabled = not locked
        if self.dropzone is not None:
            self.dropzone.enabled = not locked
        sl = self.editors['基础'].get('photos')
        if isinstance(sl, SourceList):
            for b in sl.buttons:
                b.enabled = not locked


# ---------------------------------------------------------------- 读写助手
def read_editor(prm, w):
    """从控件读回参数值（与 _make_editor 一一对应）"""
    if prm.kind in ('float', 'int'):
        return w.value()
    if prm.kind == 'choice':
        return w.value()
    if prm.kind == 'check':
        return w.checked
    if prm.kind == 'sources':
        return w.value()
    return w.text()


def write_editor(prm, w, value) -> None:
    """把参数值写进控件"""
    if prm.kind in ('float', 'int'):
        w.set_value(value)
    elif prm.kind == 'choice':
        w.set_value(value)
    elif prm.kind == 'check':
        w.set_checked(bool(value))
    elif prm.kind == 'sources':
        w.set_value('' if value is None else str(value))
    else:
        w.set_text('' if value is None else str(value))
