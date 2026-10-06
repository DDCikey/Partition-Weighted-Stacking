# 20261006 DWT 自绘控件 v2（正式版重构，替代 v1 测试版）
#
# 与 v1 的四点区别：
#   ① 基类内建 clip 视口 —— 渲染原语没有裁剪区，控件必须**完全**落在视口内
#      才绘制；宿主布局时给每个控件设 clip，不再靠 _panel_key 之类的补丁。
#   ② hover / press / focus 变成 0..1 的动画量（hover_a / press_a / focus_a），
#      tick(dt_ms) 推进插值，配色用 theme.mix 混合 —— 状态切换有短暂过渡，
#      不再是"啪"地跳色。
#   ③ 新增 TabBar（页签 + 滑动下划线）；SectionHeader 随折叠档一起退役。
#   ④ ProgressBar 的值做 lerp：引擎的跳变进度被平滑成连续运动。
#
# 交互模型不变：宿主（app.py）做矩形命中并把指针/键盘事件转给控件，
# 控件状态变化经回调通知宿主。配色逐一对应原 theme.qss 定稿（浅色扁平、青绿强调）。

from __future__ import annotations

import style as S
import theme
from wevvmold import ease          # 20261006 原生缓动求值（分段导航滑块用）
from style import (ACCENT, ACCENT_BG, ACCENT_BD, ACCENT_D, ACCENT_DS, BLUE,
                   BORDER, CARD, DARK, DARK_2, DARK_HOV, DISABLED, DISABLED_BG,
                   DISABLED_BD, HINT, RED, RED_BD, RED_BG, RED_BD2, SEL_BG, TEXT,
                   TEXT2, TRACK, TINT, TINT_BD, WHITE, char_w, elide_middle,
                   gloss_top, inner_shadow_top, pill, round_rect,
                   round_rect_border, shade_bottom, shadow, text_w)
from style import (D_ACCENT, D_BLUE, D_HINT, D_RED, D_TEXT, D_TEXT2, D_TRACK)
from theme import mix

# Windows 虚拟键码
VK_BACK, VK_TAB, VK_RETURN, VK_SHIFT = 8, 9, 13, 16
VK_CONTROL, VK_ALT = 17, 18
VK_ESCAPE, VK_SPACE = 27, 32
VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN = 37, 38, 39, 40
VK_END, VK_HOME = 35, 36
VK_DELETE = 46
VK_A, VK_C, VK_V, VK_X, VK_Z = 65, 67, 86, 88, 90


# ---------------------------------------------------------------- tk 桥（剪贴板）
_TK = None


def _tk():
    global _TK
    if _TK is None:
        try:
            import tkinter
            _TK = tkinter.Tk()
            _TK.withdraw()
        except Exception:
            _TK = False
    return _TK


def clip_get() -> str:
    r = _tk()
    if not r:
        return ''
    try:
        return r.clipboard_get()
    except Exception:
        return ''


def clip_set(s: str) -> None:
    r = _tk()
    if r:
        try:
            r.clipboard_clear()
            r.clipboard_append(s)
        except Exception:
            pass


def _approach(v, target, step):
    """把 v 以 step 的步长推向 target"""
    if v == target:
        return v
    if v < target:
        return min(target, v + step)
    return max(target, v - step)


# ---------------------------------------------------------------- 基类
class Widget:
    """矩形控件基类：rect 由宿主布局赋值；clip 限定绘制视口；动画态由 tick 推进"""

    interactive = True
    hover_ms = theme.HOVER_MS
    press_ms = theme.PRESS_MS

    def __init__(self):
        self.rect = (0.0, 0.0, 0.0, 0.0)
        self.clip = None          # (l,t,r,b) 或 None；可见性=完全落在 clip 内
        self.visible = True
        self.enabled = True
        self.hover = False
        self.pressed = False
        self.hover_a = 0.0        # hover 过渡量 0..1
        self.press_a = 0.0        # press 过渡量 0..1
        self.on_click = None
        self.tip = ''

    def set_rect(self, l, t, r, b):
        self.rect = (l, t, r, b)

    def contains(self, x, y) -> bool:
        l, t, r, b = self.rect
        return self.visible and l <= x < r and t <= y < b

    def in_clip(self) -> bool:
        if self.clip is None:
            return True
        l, t, r, b = self.rect
        cl, ct, cr, cb = self.clip
        return (t >= ct + 2 and b <= cb - 2 and
                l >= cl - 2 and r <= cr + 2)

    # ---- 绘制：子类只写 render_content，越界裁剪在基类统一把关 ----
    def render(self, rc):
        if self.visible:
            self.render_content(rc)

    def render_content(self, rc):
        pass

    # ---- 事件（返回是否消费） ----
    def on_press(self, x, y) -> bool:
        if self.contains(x, y) and self.enabled and self.on_click is not None:
            self.pressed = True
            return True
        return False

    def on_release(self, x, y):
        was = self.pressed
        self.pressed = False
        if was and self.contains(x, y) and self.enabled and self.on_click:
            self.on_click()

    def on_move(self, x, y):
        self.hover = True

    def on_leave(self):
        self.hover = False

    def on_wheel(self, x, y, delta) -> bool:
        return False

    def on_key(self, ev) -> bool:
        return False

    def on_char(self, ev) -> bool:
        return False

    # ---- 动画 ----
    def tick(self, dt_ms) -> bool:
        """推进插值；返回是否仍在运动中"""
        ht = 1.0 if (self.hover and self.enabled) else 0.0
        pt = 1.0 if self.pressed else 0.0
        self.hover_a = _approach(self.hover_a, ht, dt_ms / self.hover_ms)
        self.press_a = _approach(self.press_a, pt, dt_ms / self.press_ms)
        return self.anim_active()

    def anim_active(self) -> bool:
        return (self.hover_a not in (0.0, 1.0) or
                self.press_a not in (0.0, 1.0))


# ---------------------------------------------------------------- 按钮
class Button(Widget):
    """kind: normal / run / stop / small / browse / src"""

    def __init__(self, text='', kind='normal', on_click=None):
        super().__init__()
        self.text = text
        self.kind = kind
        self.on_click = on_click

    def render_content(self, rc):
        l, t, r, b = self.rect
        k = self.kind
        en = self.enabled
        ha, pa = self.hover_a, self.press_a
        hp = max(ha, pa)                       # 按下继承悬停的色向
        if k == 'run':
            rad = 7
            bg = mix(ACCENT, ACCENT_D, ha)
            bg = mix(bg, ACCENT_D, pa)
            fg = WHITE
            if not en:
                bg, fg = ACCENT_DS, WHITE
            if en:
                shadow(rc, l, t, r, b, rad, alpha=int(26 - 18 * pa))
            round_rect(rc, l, t, r, b, rad, bg)
            if en:
                if pa > 0.5:
                    inner_shadow_top(rc, l, t, r, b, rad, alpha=int(18 * pa))
                else:
                    gloss_top(rc, l, t, r, b, rad, alpha=int(30 * (1 - pa)))
                    shade_bottom(rc, l, t, r, b, rad, alpha=int(20 * (1 - pa)))
        elif k == 'stop':
            rad = 6
            if not en:
                bg, fg, bd = DISABLED_BG, DISABLED, DISABLED_BD
            else:
                bd = mix(BORDER, RED_BD2, hp)
                fg = mix(TEXT2, RED, hp)
                bg = mix(CARD, ACCENT_BG, pa)
                shadow(rc, l, t, r, b, rad, alpha=int(14 - 8 * pa))
            round_rect_border(rc, l, t, r, b, rad, bd, bg)
            if en:
                if pa > 0.5:
                    inner_shadow_top(rc, l, t, r, b, rad, alpha=int(10 * pa))
                else:
                    shade_bottom(rc, l, t, r, b, rad, alpha=10)
        elif k in ('small', 'src', 'browse'):
            rad = 5
            base_fg = TEXT2 if k == 'src' else TEXT
            if not en:
                bg, fg, bd = DISABLED_BG, DISABLED, DISABLED_BD
            else:
                bd = mix(BORDER, ACCENT, hp)
                fg = mix(base_fg, ACCENT, hp)
                bg = mix(CARD, ACCENT_BG, pa)
                shadow(rc, l, t, r, b, rad, alpha=int(12 - 6 * pa))
            round_rect_border(rc, l, t, r, b, rad, bd, bg)
            if en:
                if pa > 0.5:
                    inner_shadow_top(rc, l, t, r, b, rad, alpha=int(8 * pa))
                else:
                    shade_bottom(rc, l, t, r, b, rad, alpha=8)
        else:  # normal
            rad = 6
            if not en:
                bg, fg, bd = DISABLED_BG, DISABLED, DISABLED_BD
            else:
                bd = mix(BORDER, ACCENT, hp)
                fg = mix(TEXT, ACCENT, hp)
                bg = mix(CARD, ACCENT_BG, pa)
                shadow(rc, l, t, r, b, rad, alpha=int(14 - 8 * pa))
            round_rect_border(rc, l, t, r, b, rad, bd, bg)
            if en:
                if pa > 0.5:
                    inner_shadow_top(rc, l, t, r, b, rad, alpha=int(10 * pa))
                else:
                    shade_bottom(rc, l, t, r, b, rad, alpha=10)
        fs = S.FS_SMALL if k == 'src' else S.FS_BODY
        rc.draw_text(self.text, l, t - 0.5, r, b, fs, fg, valign=1, halign=1)
        if k == 'run':                          # 粗体近似：同色重影偏移 0.4px
            rc.draw_text(self.text, l + 0.4, t - 0.5, r + 0.4, b, fs, fg,
                         valign=1, halign=1)

    def desired_w(self, pad=28):
        return text_w(self.text, S.FS_SMALL if self.kind == 'src' else S.FS_BODY) + pad


# ---------------------------------------------------------------- 状态胶囊
class Chip(Widget):
    interactive = False

    def __init__(self, text='就绪', state='idle'):
        super().__init__()
        self.text = text
        self.state = state

    def set_state(self, state, text):
        self.state = state
        self.text = text

    def render_content(self, rc):
        # 深色顶栏变体：暗底胶囊 + 亮色文字，状态点在前
        l, t, r, b = self.rect
        if self.state in ('run', 'done'):
            fg, dot = D_ACCENT, ACCENT
        elif self.state == 'err':
            fg, dot = D_RED, RED
        else:
            fg, dot = D_TEXT2, D_HINT
        pill(rc, l, t, r, b, DARK_2)
        cy = (t + b) / 2
        rc.fill_ellipse(l + 13.5, cy, 3.5, 3.5, dot)
        rc.draw_text(self.text, l + 22, t, r, b, S.FS_BODY, fg, valign=1)

    def desired_w(self):
        return text_w(self.text, S.FS_BODY) + 40


# ---------------------------------------------------------------- 复选框
class CheckBox(Widget):
    def __init__(self, text='', on_toggle=None):
        super().__init__()
        self.text = text
        self.checked = False
        self.on_toggle = on_toggle
        self._pressed_box = False

    def set_checked(self, v):
        self.checked = bool(v)

    def render_content(self, rc):
        l, t, r, b = self.rect
        cy = (t + b) / 2
        bs = 14
        bx = l
        if self.hover_a > 0.01 and self.enabled:
            round_rect(rc, bx - 3, cy - bs / 2 - 3, bx + bs + 3, cy + bs / 2 + 3,
                       6, S.color(15, 157, 144, int(14 * self.hover_a)))
        if self.checked:
            round_rect(rc, bx, cy - bs / 2, bx + bs, cy + bs / 2, 3,
                       ACCENT if self.enabled else DISABLED_BD)
            x0, y0 = bx + 3.2, cy - 0.4
            pts = [(x0, y0), (x0 + 2.6, y0 + 2.8), (x0 + 8.2, y0 - 3.4),
                   (x0 + 9.6, y0 - 2.2), (x0 + 2.7, y0 + 4.6), (x0 - 1.2, y0 + 0.4)]
            rc.fill_polygon(pts, WHITE)
        else:
            round_rect_border(rc, bx, cy - bs / 2, bx + bs, cy + bs / 2, 3,
                              BORDER if self.enabled else DISABLED_BD,
                              CARD if self.enabled else DISABLED_BG)
        fg = TEXT2 if self.enabled else DISABLED
        rc.draw_text(self.text, bx + bs + 6, t, r, b, S.FS_BODY, fg, valign=1)

    def on_press(self, x, y) -> bool:
        if self.contains(x, y) and self.enabled:
            self._pressed_box = True
            self.pressed = True
            return True
        return False

    def on_release(self, x, y):
        was = self._pressed_box
        self._pressed_box = False
        self.pressed = False
        if was and self.contains(x, y) and self.enabled:
            self.checked = not self.checked
            if self.on_toggle:
                self.on_toggle()


# ---------------------------------------------------------------- 进度条
class ProgressBar(Widget):
    """值带 lerp：引擎跳变的进度被平滑成连续运动"""

    interactive = False

    def __init__(self):
        super().__init__()
        self.frac = 0.0           # 目标值（自检与逻辑读这个）
        self._disp = 0.0          # 显示值（动画插值）

    def set_value(self, frac):
        self.frac = max(0.0, min(1.0, float(frac)))

    def reset(self):
        self.frac = self._disp = 0.0

    def tick(self, dt_ms) -> bool:
        self._disp = _approach(self._disp, self.frac, dt_ms / theme.PROGRESS_MS)
        return self.anim_active()

    def anim_active(self) -> bool:
        return self._disp != self.frac

    def render_content(self, rc):
        l, t, r, b = self.rect
        rad = (b - t) / 2
        pill(rc, l, t, r, b, TRACK)
        inner_shadow_top(rc, l, t, r, b, rad, alpha=14)    # 轨道内凹
        w = (r - l) * self._disp
        if w >= (b - t):
            pill(rc, l, t, l + w, b, ACCENT_BD)
            gloss_top(rc, l, t, l + w, b, rad, alpha=26)   # 填充上亮
        elif w > 0:
            rc.fill_rect(l, t, l + w, b, ACCENT_BD)
            rc.fill_rect(l, t + 0.5, l + w, (t + b) / 2, S.color(255, 255, 255, 26))


# ---------------------------------------------------------------- 文本编辑框
class TextEdit(Widget):
    """单行文本编辑：光标/选择/剪贴板/横向滚动，全部自绘；聚焦有光晕过渡"""

    PAD = 6.0

    def __init__(self, placeholder='', on_commit=None):
        super().__init__()
        self._text = ''
        self._caret = 0
        self._anchor = 0
        self._view = 0.0
        self.focused = False
        self.focus_a = 0.0
        self.read_only = False
        self.placeholder = placeholder
        self.on_commit = on_commit
        self._blink_on = True
        self._blink_t = 0.0
        self._dragging = False

    # ---- 值 ----
    def text(self) -> str:
        return self._text

    def set_text(self, v):
        self._text = '' if v is None else str(v)
        self._caret = self._anchor = len(self._text)
        self._clamp_view()

    def _sel_range(self):
        return (min(self._caret, self._anchor), max(self._caret, self._anchor))

    def _delete_sel(self):
        a, b = self._sel_range()
        if a != b:
            self._text = self._text[:a] + self._text[b:]
            self._caret = self._anchor = a
            return True
        return False

    def _insert(self, s):
        self._delete_sel()
        i = self._caret
        self._text = self._text[:i] + s + self._text[i:]
        self._caret = self._anchor = i + len(s)

    # ---- 几何 ----
    def _x_of(self, idx) -> float:
        l, t, r, b = self.rect
        return l + self.PAD + text_w(self._text[:idx], S.FS_BODY) - self._view

    def _idx_of_x(self, x) -> int:
        px = x - (self.rect[0] + self.PAD) + self._view
        acc = 0.0
        for i, ch in enumerate(self._text):
            w = char_w(ch, S.FS_BODY)
            if acc + w / 2 > px:
                return i
            acc += w
        return len(self._text)

    def _clamp_view(self):
        l, t, r, b = self.rect
        vw = max(1.0, r - l - 2 * self.PAD)
        cx = text_w(self._text[:self._caret], S.FS_BODY)
        if cx < self._view:
            self._view = max(0.0, cx - 8)
        elif cx > self._view + vw - 4:
            self._view = cx - vw + 8
        tw = text_w(self._text, S.FS_BODY)
        self._view = max(0.0, min(self._view, max(0.0, tw - vw + 4)))

    # ---- 渲染 ----
    def render_content(self, rc):
        l, t, r, b = self.rect
        fa = self.focus_a
        if not self.enabled:
            bd, bg, fg = DISABLED_BD, DISABLED_BG, DISABLED
        else:
            bd = mix(BORDER, ACCENT, fa)
            bg, fg = CARD, TEXT
        if fa > 0.01 and self.enabled:
            round_rect(rc, l - 2, t - 2, r + 2, b + 2, 7,
                       S.color(15, 157, 144, int(26 * fa)))     # 聚焦光晕
        round_rect_border(rc, l, t, r, b, 5, bd, bg)
        if self.enabled:
            inner_shadow_top(rc, l, t, r, b, 5, alpha=5)          # 输入槽内凹
        if not self._text and self.placeholder and not self.focused:
            rc.draw_text(self.placeholder, l + self.PAD, t, r - self.PAD, b,
                         S.FS_BODY, HINT, valign=1)
        a, z = self._sel_range()
        if a != z and self.enabled:
            xa, xb = self._x_of(a), self._x_of(z)
            xa = max(xa, l + 1)
            xb = min(xb, r - 1)
            if xb > xa:
                rc.fill_rect(xa, t + 4, xb, b - 4, SEL_BG)
        # 正文：逐字符画，天然带横向视口裁剪（无裁剪区约束）
        x = l + self.PAD - self._view
        for ch in self._text:
            if x > r - self.PAD:
                break
            if x + char_w(ch, S.FS_BODY) >= l + self.PAD - 1:
                rc.draw_text(ch, x, t, r, b, S.FS_BODY, fg, valign=1)
            x += char_w(ch, S.FS_BODY)
        if self.focused and self.enabled and self._blink_on and not self.read_only:
            cx = self._x_of(self._caret)
            if l + 1 <= cx <= r - 1:
                rc.fill_rect(cx, t + 3, cx + 1.2, b - 3, TEXT)

    # ---- 事件 ----
    def on_press(self, x, y) -> bool:
        if not (self.contains(x, y) and self.enabled):
            return False
        self.focused = True
        self._blink_on = True
        self._caret = self._idx_of_x(x)
        self._anchor = self._caret
        self._dragging = True
        self._clamp_view()
        return True

    def on_drag(self, x, y):
        if self._dragging and self.enabled:
            self._caret = self._idx_of_x(x)
            self._clamp_view()

    def on_release(self, x, y):
        self._dragging = False

    def on_wheel(self, x, y, delta) -> bool:
        return False

    def _commit(self):
        if self.on_commit:
            self.on_commit()

    def on_key(self, ev) -> bool:
        if not (self.focused and self.enabled):
            return False
        vk = ev.get('key_code', 0)
        ctrl = ev.get('ctrl', False)
        shift = ev.get('shift', False)
        n = len(self._text)
        if vk == VK_LEFT:
            self._caret = max(0, self._caret - 1)
            if not shift:
                self._anchor = self._caret
        elif vk == VK_RIGHT:
            self._caret = min(n, self._caret + 1)
            if not shift:
                self._anchor = self._caret
        elif vk == VK_HOME:
            self._caret = 0
            if not shift:
                self._anchor = 0
        elif vk == VK_END:
            self._caret = n
            if not shift:
                self._anchor = n
        elif vk == VK_BACK:
            if not self.read_only:
                if not self._delete_sel():
                    i = self._caret
                    if i > 0:
                        self._text = self._text[:i - 1] + self._text[i:]
                        self._caret = self._anchor = i - 1
                self._commit()
        elif vk == VK_DELETE:
            if not self.read_only:
                if not self._delete_sel():
                    i = self._caret
                    if i < n:
                        self._text = self._text[:i] + self._text[i + 1:]
                self._commit()
        elif vk == VK_RETURN:
            self._commit()
        elif vk == VK_TAB:
            self.focused = False
            self._commit()
        elif vk == VK_ESCAPE:
            self.focused = False
            self._anchor = self._caret
        elif ctrl and vk == VK_A:
            self._caret, self._anchor = 0, n
        elif ctrl and vk == VK_C:
            a, z = self._sel_range()
            clip_set(self._text[a:z])
        elif ctrl and vk == VK_X:
            a, z = self._sel_range()
            if a != z and not self.read_only:
                clip_set(self._text[a:z])
                self._delete_sel()
                self._commit()
        elif ctrl and vk == VK_V:
            if not self.read_only:
                s = clip_get().replace('\r', '').replace('\n', '')
                if s:
                    self._insert(s)
                    self._commit()
        else:
            return False
        self._blink_on = True
        self._clamp_view()
        return True

    def on_char(self, ev) -> bool:
        if not (self.focused and self.enabled and not self.read_only):
            return False
        cc = ev.get('char_code', 0)
        if cc < 32 or cc == 127:
            return False
        self._insert(chr(cc))
        self._blink_on = True
        self._clamp_view()
        self._commit()
        return True

    def tick(self, dt_ms) -> bool:
        super().tick(dt_ms)
        ft = 1.0 if (self.focused and self.enabled) else 0.0
        self.focus_a = _approach(self.focus_a, ft, dt_ms / theme.FOCUS_MS)
        if self.focused and self.enabled:
            self._blink_t += dt_ms
            if self._blink_t >= 450:
                self._blink_t = 0.0
                self._blink_on = not self._blink_on
        return self.anim_active()

    def anim_active(self) -> bool:
        return super().anim_active() or self.focus_a not in (0.0, 1.0)


# ---------------------------------------------------------------- 数值微调框
class SpinEdit(TextEdit):
    """文本编辑 + 右侧上下箭头 + 滚轮步进；提交时钳制范围并按 dec 格式化"""

    ARROW_W = 16

    def __init__(self, lo=0.0, hi=1.0, step=1.0, dec=2, is_int=False):
        super().__init__()
        self.lo, self.hi = float(lo), float(hi)
        self.step = float(step)
        self.dec = int(dec)
        self.is_int = is_int
        self.on_value_changed = None

    def fmt(self, v) -> str:
        if self.is_int:
            return str(int(round(v)))
        return f'{v:.{self.dec}f}'

    def value(self) -> float:
        try:
            v = float(self._text)
        except ValueError:
            v = 0.0
        return self._clamp(v)

    def set_value(self, v):
        self._text = self.fmt(self._clamp(v))
        self._caret = self._anchor = len(self._text)

    def _clamp(self, v):
        v = max(self.lo, min(self.hi, v))
        return round(v) if self.is_int else v

    def _commit(self):
        if not self.focused:
            try:
                v = self._clamp(float(self._text))
                self._text = self.fmt(v)
            except ValueError:
                pass
        else:
            try:
                v = self._clamp(float(self._text))
                if not self._dragging:
                    self._text = self.fmt(v)
            except ValueError:
                pass

    def commit_now(self):
        v = self.value()
        self._text = self.fmt(v)
        self._caret = self._anchor = len(self._text)
        if self.on_value_changed:
            self.on_value_changed()

    def _step(self, d):
        v = self.value() + d * self.step
        v = self._clamp(v)
        self._text = self.fmt(v)
        self._caret = self._anchor = len(self._text)
        if self.on_value_changed:
            self.on_value_changed()

    def render_content(self, rc):
        super().render_content(rc)
        l, t, r, b = self.rect
        ax = r - self.ARROW_W
        bd = DISABLED_BD if not self.enabled else BORDER
        rc.fill_rect(ax, t + 1, ax + 1, b - 1, bd)
        my = (t + b) / 2
        fg = DISABLED if not self.enabled else mix(TEXT2, ACCENT, self.hover_a)
        rc.fill_polygon([(ax + 4, my - 3), (ax + 12, my - 3), (ax + 8, my - 7)], fg)
        rc.fill_polygon([(ax + 4, my + 3), (ax + 12, my + 3), (ax + 8, my + 7)], fg)

    def on_press(self, x, y) -> bool:
        if not self.contains(x, y):
            return False
        if not self.enabled:
            return True
        l, t, r, b = self.rect
        ax = r - self.ARROW_W
        if x >= ax:
            self._step(-1 if y > (t + b) / 2 else 1)
            self._dragging = False
            return True
        return super().on_press(x, y)

    def on_wheel(self, x, y, delta) -> bool:
        if not self.enabled:
            return False
        self._step(1 if delta > 0 else -1)
        return True


# ---------------------------------------------------------------- 页签条
class TabBar(Widget):
    """页签行：文字 + 一条在页签间滑动的下划线"""

    def __init__(self, labels, on_select=None):
        super().__init__()
        self.labels = list(labels)
        self.index = 0
        self.on_select = on_select
        self._slide = 0.0         # 下划线所在页签（浮点，向 index 插值）
        self._hover_i = -1

    def set_index(self, i):
        if i != self.index:
            self.index = i
            if self.on_select:
                self.on_select(i)

    def _widths(self):
        return [text_w(s, S.FS_BODY) + 24 for s in self.labels]

    def _tab_at(self, x):
        px = self.rect[0]
        for i, w in enumerate(self._widths()):
            if px <= x < px + w:
                return i
            px += w
        return -1

    def _center(self, f):
        l = self.rect[0]
        ws = self._widths()
        n = len(ws)
        if n == 0:
            return l
        i0 = max(0, min(n - 1, int(f)))
        i1 = max(0, min(n - 1, i0 + 1))
        fr = min(1.0, max(0.0, f - i0))
        c0 = l + sum(ws[:i0]) + ws[i0] / 2
        c1 = l + sum(ws[:i1]) + ws[i1] / 2
        return c0 + (c1 - c0) * fr

    def render_content(self, rc):
        l, t, r, b = self.rect
        x = l
        for i, lab in enumerate(self.labels):
            w = text_w(lab, S.FS_BODY) + 24
            if i == self.index:
                fg = ACCENT
                rc.draw_text(lab, x, t, x + w, b, S.FS_BODY, fg, valign=1, halign=1)
                rc.draw_text(lab, x + 0.4, t, x + w + 0.4, b, S.FS_BODY, fg,
                             valign=1, halign=1)     # 选中加粗近似
            else:
                fg = TEXT if i == self._hover_i else TEXT2
                rc.draw_text(lab, x, t, x + w, b, S.FS_BODY, fg, valign=1, halign=1)
            x += w
        # 下划线：2.5px 圆条，在页签中心间滑动
        ci = self._center(self._slide)
        ii = max(0, min(len(self.labels) - 1, int(round(self._slide))))
        uw = text_w(self.labels[ii], S.FS_BODY) + 12
        round_rect(rc, ci - uw / 2, b - 3.5, ci + uw / 2, b - 0.5, 1.5, ACCENT)

    def tick(self, dt_ms) -> bool:
        self._slide = _approach(self._slide, float(self.index), dt_ms / theme.SLIDE_MS)
        return self.anim_active()

    def anim_active(self) -> bool:
        return self._slide != float(self.index)

    def on_press(self, x, y) -> bool:
        if not self.contains(x, y) or not self.enabled:
            return False
        i = self._tab_at(x)
        if i >= 0:
            self.set_index(i)
        return True

    def on_move(self, x, y):
        super().on_move(x, y)
        self._hover_i = self._tab_at(x)

    def on_leave(self):
        super().on_leave()
        self._hover_i = -1


# ---------------------------------------------------------------- 下拉选择框
class ChoiceBox(Widget):
    """(界面文字, 值) 列表的下拉选择（当前 PARAMS 无 choice 参数，保留以兜底）"""

    ITEM_H = 24
    PAD = 6.0

    def __init__(self, choices=()):
        super().__init__()
        self.choices = list(choices)
        self.index = 0
        self.open = False
        self._mouse_y = -1.0

    def value(self):
        return self.choices[self.index][1] if self.choices else None

    def set_value(self, v):
        for i, (_t, val) in enumerate(self.choices):
            if val == v:
                self.index = i
                return
        self.index = 0

    def _item_rect(self, i):
        l, t, r, b = self.rect
        y = b + i * self.ITEM_H
        return (l, y, r, y + self.ITEM_H)

    def render_content(self, rc):
        l, t, r, b = self.rect
        bd = DISABLED_BD if not self.enabled else (
            ACCENT if (self.hover or self.open) else BORDER)
        round_rect_border(rc, l, t, r, b, 5, bd,
                          DISABLED_BG if not self.enabled else CARD)
        txt = self.choices[self.index][0] if self.choices else ''
        rc.draw_text(txt, l + self.PAD, t, r - 18, b, S.FS_BODY,
                     DISABLED if not self.enabled else TEXT, valign=1)
        rc.fill_polygon([(r - 13, (t + b) / 2 - 2), (r - 5, (t + b) / 2 - 2),
                         (r - 9, (t + b) / 2 + 3)], TEXT2)
        if self.open:
            for i in range(len(self.choices)):
                il, it, ir, ib = self._item_rect(i)
                hover = it <= self._mouse_y < ib
                round_rect_border(rc, il, it, ir, ib, 5 if i == 0 else 0,
                                  BORDER, ACCENT_BG if hover else CARD)
                rc.draw_text(self.choices[i][0], il + self.PAD, it, ir, ib,
                             S.FS_BODY, ACCENT if i == self.index else TEXT, valign=1)

    def on_press(self, x, y) -> bool:
        if not self.contains(x, y) or not self.enabled:
            if self.open:
                for i in range(len(self.choices)):
                    il, it, ir, ib = self._item_rect(i)
                    if it <= y < ib and il <= x < ir:
                        self.index = i
                        self.open = False
                        if self.on_click:
                            self.on_click()
                        return True
                self.open = False
            return False
        self.open = not self.open
        return True

    def on_move(self, x, y):
        super().on_move(x, y)
        self._mouse_y = y


# ---------------------------------------------------------------- 素材来源列表
class SourceList(Widget):
    """v3 行卡清单：每条来源一张白圆角卡（teal 取景框图标 + 名称/上级目录
    两行 + 行尾「×」）；空态交给 DropZone，增删按钮只剩「添加单张 / 清空」"""

    ROW_H = 44
    ROW_GAP = 8

    def __init__(self, on_change=None):
        super().__init__()
        self.sources = []
        self.on_change = on_change
        self._hover_row = -1
        self.buttons = [
            Button('添加单张', 'src', on_click=self._pick_files),
            Button('清空', 'src', on_click=self.clear_all),
        ]
        self.buttons[0].tip = '追加单张帧文件（可多选）'
        self.buttons[1].tip = '移除全部素材来源'

    # ---- 文件对话框（tkinter 桥，不引入 PySide6）----
    def _start_dir(self) -> str:
        from pathlib import Path
        for s in self.sources:
            p = Path(s)
            if p.exists():
                return str(p if p.is_dir() else p.parent)
        return ''

    def _pick_dir(self):
        try:
            import tkinter
            from tkinter import filedialog
            root = tkinter.Tk()
            root.withdraw()
            path = filedialog.askdirectory(title='添加素材目录',
                                           initialdir=self._start_dir() or None)
            root.destroy()
            if path:
                self.add_paths([path])
        except Exception:
            pass

    def _pick_files(self):
        try:
            import tkinter
            from tkinter import filedialog
            root = tkinter.Tk()
            root.withdraw()
            paths = filedialog.askopenfilenames(
                title='添加单张帧', initialdir=self._start_dir() or None,
                filetypes=[('帧文件', '*.xisf *.fit *.fits *.fts')])
            root.destroy()
            if paths:
                self.add_paths(list(paths))
        except Exception:
            pass

    def value(self) -> str:
        return ';'.join(self.sources)

    def set_value(self, v):
        import os
        cur, seen = [], set()
        for s in ('' if v is None else str(v)).split(';'):
            s = s.strip().strip('"')
            k = os.path.normcase(s)
            if s and k not in seen:
                seen.add(k)
                cur.append(s)
        self.sources = cur

    def add_paths(self, paths):
        import os
        seen = {os.path.normcase(p) for p in self.sources}
        for p in paths:
            p = (p or '').strip().strip('"')
            k = os.path.normcase(p)
            if p and k not in seen:
                seen.add(k)
                self.sources.append(p)
        if self.on_change:
            self.on_change()

    def remove(self, i):
        if 0 <= i < len(self.sources):
            del self.sources[i]
            if self.on_change:
                self.on_change()

    def clear_all(self):
        self.sources = []
        if self.on_change:
            self.on_change()

    def content_h(self) -> float:
        return len(self.sources) * (self.ROW_H + self.ROW_GAP)

    def _row_rect(self, i):
        l, t, r, b = self.rect
        y = t + i * (self.ROW_H + self.ROW_GAP)
        return (l, y, r, y + self.ROW_H)

    def render_content(self, rc):
        import os
        l, t, r, b = self.rect
        if not self.sources:
            return
        for i, src in enumerate(self.sources):
            rl, rt, rr, rb = self._row_rect(i)
            if rb > b + 1:
                break
            hov = (i == self._hover_row and self.enabled)
            shadow(rc, rl, rt, rr, rb, 10, alpha=14)
            round_rect_border(rc, rl, rt, rr, rb, 10,
                              (ACCENT if hov else BORDER) if self.enabled
                              else DISABLED_BD,
                              CARD if self.enabled else DISABLED_BG)
            # teal 取景框图标
            ix, iy = rl + 12 + 12, (rt + rb) / 2
            round_rect(rc, ix - 12, iy - 12, ix + 12, iy + 12, 7,
                       ACCENT if self.enabled else DISABLED_BD)
            rc.fill_rect(ix - 6, iy - 5, ix + 6, iy - 3.4, WHITE)
            rc.fill_rect(ix - 6, iy + 3.4, ix + 6, iy + 5, WHITE)
            rc.fill_rect(ix - 6, iy - 5, ix - 4.4, iy + 5, WHITE)
            rc.fill_rect(ix + 4.4, iy - 5, ix + 6, iy + 5, WHITE)
            # 两行：名称 + 上级目录
            x0 = ix + 20
            x1 = rr - 44
            name = (src.replace('\\', '/').rstrip('/').split('/')[-1]) or src
            parent = src.replace('\\', '/').rstrip('/')
            parent = parent[:parent.rfind('/')] if '/' in parent[:-1] else ''
            rc.draw_text(elide_middle(name, S.FS_BODY, x1 - x0), x0, rt + 5,
                         x1, rt + 24, S.FS_BODY,
                         TEXT if self.enabled else DISABLED, valign=1)
            if parent:
                rc.draw_text(elide_middle(parent, S.FS_SMALL, x1 - x0), x0, rt + 23,
                             x1, rb - 5, S.FS_SMALL, HINT, valign=1)
            # 行尾 ×
            zx = rr - 36
            if hov:
                round_rect(rc, zx - 4, (rt + rb) / 2 - 9, zx + 18,
                           (rt + rb) / 2 + 9, 9, RED_BG)
                rc.draw_text('×', zx - 4, (rt + rb) / 2 - 9, zx + 18,
                             (rt + rb) / 2 + 9, 13, RED, valign=1, halign=1)
            else:
                rc.draw_text('×', zx - 4, rt, zx + 18, rb, 13, HINT,
                             valign=1, halign=1)

    def on_move(self, x, y):
        super().on_move(x, y)
        row = -1
        if self.enabled:
            for i in range(len(self.sources)):
                rl, rt, rr, rb = self._row_rect(i)
                if rt <= y < rb:
                    row = i
                    break
        self._hover_row = row

    def on_leave(self):
        super().on_leave()
        self._hover_row = -1

    def on_press(self, x, y) -> bool:
        if not self.contains(x, y) or not self.enabled:
            return False
        return True

    def on_release(self, x, y):
        if not self.enabled:
            return
        for i in range(len(self.sources)):
            rl, rt, rr, rb = self._row_rect(i)
            if rt <= y < rb and x >= rr - 40:
                self.remove(i)
                return

    def full_text_at(self, x, y) -> str:
        for i in range(len(self.sources)):
            rl, rt, rr, rb = self._row_rect(i)
            if rt <= y < rb:
                return self.sources[i]
        return ''


# ================================================================ 控制台重设计 v3
# 深色顶栏 + 全宽页面带来的新控件：分段导航 / 圆形运行钮 / 大开关 /
# 圆环进度 / 取景框式 Drop 区。浅色页的旧控件继续保留复用。


def _lerp_rect(a, b, t):
    return tuple(x + (y - x) * t for x, y in zip(a, b))


class Segmented(Widget):
    """深色底上的分段导航：等宽块，选中块是滑动的青绿胶囊"""

    def __init__(self, labels, on_select=None):
        super().__init__()
        self.labels = list(labels)
        self.index = 0
        self.on_select = on_select
        self._slide = 0.0
        self._from = 0.0          # 20261006 缓动补间起点（换页瞬间捕获当前视觉位）
        self._t = 1.0             # 补间进度 0..1（1=静止）
        self._hover_i = -1

    def set_index(self, i):
        if i != self.index:
            self.index = i
            self._from = self._slide    # 半途改向也从当前位续动，不跳变
            self._t = 0.0
            if self.on_select:
                self.on_select(i)

    def _seg(self, i):
        l, t, r, b = self.rect
        n = max(1, len(self.labels))
        pad = 4.0
        sw = (r - l - 2 * pad) / n
        x = l + pad + i * sw
        return (x + 2, t + 3, x + sw - 2, b - 3)

    def render_content(self, rc):
        l, t, r, b = self.rect
        round_rect(rc, l, t, r, b, (b - t) / 2, DARK_2)
        n = max(1, len(self.labels))
        i0 = max(0, min(n - 1, int(self._slide)))
        i1 = max(0, min(n - 1, i0 + 1))
        a = self._seg(i0)
        bb = self._seg(i1)
        sl = _lerp_rect(a, bb, max(0.0, min(1.0, self._slide - i0)))
        round_rect(rc, sl[0], sl[1], sl[2], sl[3], (sl[3] - sl[1]) / 2, ACCENT)
        for i, lab in enumerate(self.labels):
            x0, y0, x1, y1 = self._seg(i)
            if i == self.index:
                rc.draw_text(lab, x0, y0, x1, y1, S.FS_BODY, WHITE, valign=1, halign=1)
                rc.draw_text(lab, x0 + 0.4, y0, x1 + 0.4, y1, S.FS_BODY, WHITE,
                             valign=1, halign=1)
            else:
                fg = D_TEXT if i == self._hover_i else D_TEXT2
                rc.draw_text(lab, x0, y0, x1, y1, S.FS_BODY, fg, valign=1, halign=1)

    def tick(self, dt_ms) -> bool:
        # 20261006 匀速 _approach → 原生 ease CUBIC OUT（kind=2,mode=1）：
        #   起步快、落点减速，短时长下比匀速"跟手"；中途改页从当前位续动
        if self._t < 1.0:
            self._t = min(1.0, self._t + dt_ms / theme.SLIDE_MS)
            tgt = float(self.index)
            self._slide = self._from + (tgt - self._from) * ease(2, 1, self._t)
        return self.anim_active()

    def anim_active(self) -> bool:
        return self._t < 1.0

    def _seg_at(self, x):
        l, t, r, b = self.rect
        n = max(1, len(self.labels))
        pad = 4.0
        sw = (r - l - 2 * pad) / n
        i = int((x - l - pad) // sw)
        return max(0, min(n - 1, i)) if x >= l + pad and x <= r - pad else -1

    def on_press(self, x, y) -> bool:
        if not self.contains(x, y) or not self.enabled:
            return False
        i = self._seg_at(x)
        if i >= 0:
            self.set_index(i)
        return True

    def on_move(self, x, y):
        super().on_move(x, y)
        self._hover_i = self._seg_at(x)

    def on_leave(self):
        super().on_leave()
        self._hover_i = -1


class RoundButton(Widget):
    """圆形图标钮：kind 'run'（青底白▶）/ 'stop'（深底红方块）"""

    def __init__(self, kind='run', on_click=None):
        super().__init__()
        self.kind = kind
        self.on_click = on_click

    def set_center(self, cx, cy, d):
        self.set_rect(cx - d / 2, cy - d / 2, cx + d / 2, cy + d / 2)

    def render_content(self, rc):
        l, t, r, b = self.rect
        cx, cy = (l + r) / 2, (t + b) / 2
        hp = max(self.hover_a, self.press_a)
        rad = (r - l) / 2 - 2.0 * self.press_a      # 按下微缩
        if self.kind == 'run':
            if self.enabled:
                bg = mix(ACCENT, ACCENT_D, hp)
                s = rad * 0.40
                tri = [(cx - s * 0.72, cy - s), (cx - s * 0.72, cy + s), (cx + s, cy)]
                fg = WHITE
            else:
                bg = DARK_2
                s = rad * 0.40
                tri = [(cx - s * 0.72, cy - s), (cx - s * 0.72, cy + s), (cx + s, cy)]
                fg = D_HINT
            rc.fill_ellipse(cx, cy, rad, rad, bg)
            rc.fill_polygon(tri, fg)
        else:  # stop
            bg = mix(DARK_2, DARK_HOV, hp) if self.enabled else DARK_2
            rc.fill_ellipse(cx, cy, rad, rad, bg)
            s = rad * 0.46
            col = RED if self.enabled and hp < 0.5 else (mix(RED, D_RED, hp) if self.enabled else D_HINT)
            rc.fill_rect(cx - s, cy - s, cx + s, cy + s, col)


class Toggle(Widget):
    """大开关：on=青绿轨道圆钮居右，off=灰轨道圆钮居左；带滑动动画"""

    W = 46
    H = 26

    def __init__(self, on_toggle=None):
        super().__init__()
        self.checked = False
        self.on_toggle = on_toggle
        self._knob = 0.0

    def set_checked(self, v):
        self.checked = bool(v)
        self._knob = 1.0 if self.checked else 0.0

    def render_content(self, rc):
        l, t, r, b = self.rect
        cy = (t + b) / 2
        x0 = l
        w = self.W
        h = self.H
        if not self.enabled:
            track = DISABLED_BG
            knob = DISABLED
        else:
            track = mix(BORDER, ACCENT, self._knob)
            knob = WHITE
        round_rect(rc, x0, cy - h / 2, x0 + w, cy + h / 2, h / 2, track)
        if self.hover_a > 0.01 and self.enabled:
            round_rect(rc, x0 - 3, cy - h / 2 - 3, x0 + w + 3, cy + h / 2 + 3,
                       (h + 6) / 2, S.color(15, 157, 144, int(12 * self.hover_a)))
        kx = x0 + h / 2 + (w - h) * self._knob
        kr = h / 2 - 3
        rc.fill_ellipse(kx, cy, kr, kr, knob)

    def tick(self, dt_ms) -> bool:
        target = 1.0 if self.checked else 0.0
        self._knob = _approach(self._knob, target, dt_ms / theme.FOCUS_MS)
        return self.anim_active()

    def anim_active(self) -> bool:
        return self._knob != (1.0 if self.checked else 0.0)

    def on_press(self, x, y) -> bool:
        if self.contains(x, y) and self.enabled:
            self.pressed = True
            return True
        return False

    def on_release(self, x, y):
        was = self.pressed
        self.pressed = False
        if was and self.contains(x, y) and self.enabled:
            self.checked = not self.checked
            if self.on_toggle:
                self.on_toggle()


def _ring_arc(rc, cx, cy, ro, ri, f0, f1, col):
    """20261006 单多边形扇环（外弧正走 + 内弧反走，简单多边形无接缝）+ 两端
    圆帽点。替代旧"沿中线冲压圆点"：低进度时点距过大，肉眼是一串珠子"""
    import math
    if f1 - f0 <= 0:
        return
    mid, half = (ro + ri) / 2, (ro - ri) / 2
    a0 = -math.pi / 2 + 2 * math.pi * f0
    span = 2 * math.pi * (f1 - f0)
    n = max(2, int(math.ceil(math.degrees(span))))   # 约每度 1 点，1° 弦高误差 <0.01px
    pts = []
    for k in range(n + 1):
        a = a0 + span * k / n
        pts.append((cx + ro * math.cos(a), cy + ro * math.sin(a)))
    for k in range(n, -1, -1):
        a = a0 + span * k / n
        pts.append((cx + ri * math.cos(a), cy + ri * math.sin(a)))
    rc.fill_polygon(pts, col)
    rc.fill_ellipse(cx + mid * math.cos(a0), cy + mid * math.sin(a0),
                    half, half, col)
    a1 = a0 + span
    rc.fill_ellipse(cx + mid * math.cos(a1), cy + mid * math.sin(a1),
                    half, half, col)


class Ring(Widget):
    """圆环进度：浅色轨道 + 青绿填充，值带 lerp；中央文字由宿主绘制"""

    interactive = False

    def __init__(self):
        super().__init__()
        self.frac = 0.0
        self._disp = 0.0
        self.bg = S.SOFT          # 环心要"挖"出的页面底色（轨道用两枚椭圆代替冲压）

    def set_value(self, frac):
        self.frac = max(0.0, min(1.0, float(frac)))

    def reset(self):
        self.frac = self._disp = 0.0

    def tick(self, dt_ms) -> bool:
        self._disp = _approach(self._disp, self.frac, dt_ms / theme.PROGRESS_MS)
        return self.anim_active()

    def anim_active(self) -> bool:
        return self._disp != self.frac

    def render_content(self, rc):
        l, t, r, b = self.rect
        cx, cy = (l + r) / 2, (t + b) / 2
        ro = min(r - l, b - t) / 2
        ri = ro - 26
        rc.fill_ellipse(cx, cy, ro, ro, TRACK)
        rc.fill_ellipse(cx, cy, ri, ri, self.bg)
        if self._disp > 0.004:
            _ring_arc(rc, cx, cy, ro, ri, 0.0, self._disp, ACCENT)


class DropZone(Widget):
    """取景框角标式大 Drop 区：点击=添加素材目录"""

    def __init__(self, on_pick=None):
        super().__init__()
        self.on_pick = on_pick
        self.count = 0

    def render_content(self, rc):
        l, t, r, b = self.rect
        ha = self.hover_a
        base = mix(S.TINT, S.color(232, 247, 243), ha)
        round_rect(rc, l, t, r, b, theme.R_CARD, base)
        # 四角 L 形取景标
        m, ln, th = 18, 26, 3
        col = mix(S.ACCENT, S.ACCENT_D, ha)
        for (cx, cy, sx, sy) in ((l + m, t + m, 1, 1), (r - m, t + m, -1, 1),
                                  (l + m, b - m, 1, -1), (r - m, b - m, -1, -1)):
            # 横笔
            rc.fill_rect(cx if sx > 0 else cx - ln, cy - th / 2,
                         cx + ln if sx > 0 else cx, cy + th / 2, col)
            # 竖笔
            rc.fill_rect(cx - th / 2, cy if sy > 0 else cy - ln,
                         cx + th / 2, cy + ln if sy > 0 else cy, col)
        # 中央：加号 + 主文 + 副文
        ccx, ccy = (l + r) / 2, (t + b) / 2 - 16
        rc.fill_rect(ccx - 13, ccy - 2.5, ccx + 13, ccy + 2.5, col)
        rc.fill_rect(ccx - 2.5, ccy - 13, ccx + 2.5, ccy + 13, col)
        rc.draw_text('点击添加素材目录', ccx - 160, ccy + 24, ccx + 160, ccy + 46,
                     S.FS_TITLE2, TEXT, valign=1, halign=1)
        rc.draw_text('XISF / FITS · 目录与单张帧可混用 · 重复声明只算一帧',
                     ccx - 220, ccy + 48, ccx + 220, ccy + 64,
                     S.FS_SMALL, HINT, valign=1, halign=1)
        if self.count:
            rc.draw_text(f'已添加 {self.count} 项，可继续添加', l + 24, b - 36, l + 220, b - 16,
                         S.FS_SMALL, ACCENT, valign=1)

    def on_release(self, x, y):
        was = self.pressed
        self.pressed = False
        if was and self.contains(x, y) and self.enabled and self.on_pick:
            self.on_pick()

    def on_press(self, x, y) -> bool:
        if self.contains(x, y) and self.enabled:
            self.pressed = True
            return True
        return False
