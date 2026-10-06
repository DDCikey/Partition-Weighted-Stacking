# 织铸 WevvMold —— 纯 Python 封装层（接口文档第 2~6/7 节所描述的公开 API）
#
# 分发包内原本只有原生层 `_wevvmold.pyd` + `WevvMoldCore.dll`，缺这一层；
# 本文件按《接口文档.md》重建封装，覆盖窗口 / 渲染上下文 / 控件三族。
# 事件与常量取值经 20261005 对 .pyd 实测核验（事件为 dict、rc 为整型句柄、
# WEVVMOLD_EVENT 按文档顺序 0..16、TIMER_TICK=15）。
#
# 生命周期：Window/Control 用 close() 释放；不做 __del__ 自动销毁
#   （文档 §17.4 的悬垂指针陷阱源于 GC 触发销毁，这里显式管理更安全）。

from __future__ import annotations

import _wevvmold as _native


def get_version() -> str:
    return _native.version()


def color(r: int, g: int, b: int, a: int = 255):
    """0..255 整数 → 0..1 浮点 RGBA 元组"""
    return (r / 255.0, g / 255.0, b / 255.0, a / 255.0)


# 20261006 补导出缓动求值（原生 ease 一直在 .so 里，wrapper 漏了）：
#   kind 0..9 = LINEAR/QUAD/CUBIC/QUART/SINE/EXPO/CIRC/BACK/ELASTIC/BOUNCE
#   mode 0..2 = IN/OUT/IN_OUT；t ∈ [0,1]
ease = _native.ease


WEVVMOLD_EVENT = {
    'WINDOW_CREATED': 0, 'CLOSED': 1, 'CLOSE_REQUESTED': 2, 'SHOWN': 3,
    'MOVED': 4, 'RESIZED': 5, 'ACTIVATED': 6, 'DEACTIVATED': 7,
    'POINTER_MOVE': 8, 'POINTER_BUTTON_DOWN': 9, 'POINTER_BUTTON_UP': 10,
    'POINTER_WHEEL': 11, 'KEY_DOWN': 12, 'KEY_UP': 13, 'CHAR_INPUT': 14,
    'TIMER_TICK': 15, 'DPI_CHANGED': 16,
}

CONTROL_TYPE = {
    'LABEL': 0, 'SEPARATOR': 1, 'IMAGE': 2, 'PROGRESS_BAR': 3, 'BUTTON': 4,
    'SWITCH': 5, 'CHECK_BOX': 6, 'RADIO_BUTTON': 7, 'TEXT_BOX': 8,
    'TEXT_AREA': 9, 'SPIN_BOX': 10, 'SLIDER': 11, 'SCROLL_BAR': 12,
    'LIST_BOX': 13, 'COMBO_BOX': 14, 'TREE': 15, 'TABLE': 16,
    'COLOR_PICKER': 17, 'DATE_PICKER': 18, 'MESSAGE_BOX': 19, 'WIZARD': 20,
    'CONTAINER': 21, 'STACK': 22, 'TAB': 23, 'SPLITTER': 24, 'DOCK': 25,
    'FLOATING_PANEL': 26, 'POPUP_HOST': 27, 'MENU_PANEL': 28, 'SCROLL': 29,
    'MULTI_SELECT': 30, 'DRAG_DROP': 31, 'CANVAS': 32, 'CHART': 33,
    'DATA_GRID': 34,
}

CONTROL_STATE = {'NORMAL': 0, 'HOVERED': 1, 'PRESSED': 2, 'DISABLED': 3, 'FOCUSED': 4}
CONTROL_EVENT = {'MOVE': 0, 'PRESS': 1, 'RELEASE': 2, 'LEAVE': 3, 'KEY_DOWN': 4,
                 'KEY_UP': 5, 'CHAR': 6, 'WHEEL': 7, 'FOCUS_IN': 8, 'FOCUS_OUT': 9}
CONTROL_POLL = {'CLICKED': 0, 'VALUE_CHANGED': 1, 'TEXT_CHANGED': 2, 'SELECTION_CHANGED': 3}
WEVVMOLD_MOUSE_BUTTON = {'NONE': 0, 'LEFT': 1, 'MIDDLE': 2, 'RIGHT': 3,
                         'ITEM_XBUTTON1': 4, 'ITEM_XBUTTON2': 5}


class RenderContext:
    """on_render 回调期内有效的绘制命令包装（rc 为原生整型句柄）"""

    __slots__ = ('_rc', '_win')

    def __init__(self, rc, win_handle):
        self._rc = rc
        self._win = win_handle

    def fill_rect(self, left, top, right, bottom, col):
        _native.render_fill_rect(self._rc, left, top, right, bottom, *col)

    def fill_ellipse(self, cx, cy, rx, ry, col):
        _native.render_fill_ellipse(self._rc, cx, cy, rx, ry, *col)

    def fill_polygon(self, points, col):
        # 原生层实际要求扁平数值序列 [x1,y1,x2,y2,...]（docstring 的元组列表写法实测报错）
        _native.render_fill_polygon(self._rc,
                                    [v for p in points for v in (p[0], p[1])],
                                    *col)

    def draw_text(self, text, left, top, right, bottom, size, col, valign=0, halign=0):
        _native.render_draw_text(self._rc, text, left, top, right, bottom, size,
                                 *col, int(valign), int(halign))

    def measure_text(self, text, size):
        """20261006 文本度量：指定字号下文本实际渲染宽度（逻辑像素，不含尾随
        空白，与 draw_text 的视觉右缘一致；空串返回 0.0）。仅 on_render 回调内可用。
        （接口文档 §4 / §16：WevvMoldAbi_RenderMeasureText ↔ render_measure_text）"""
        return _native.render_measure_text(self._rc, text, float(size))

    def render_control(self, control, dx=0.0, dy=0.0):
        h = control.handle if isinstance(control, WevvMoldControl) else control
        _native.control_render(h, self._win, dx, dy)

    def upload_bitmap(self, width, height, pixels):
        return _native.render_upload_gpu_bitmap(self._rc, width, height, pixels)

    def draw_bitmap(self, token, src_rect, dst_rect, opacity=1.0, interpolation=1):
        _native.render_draw_gpu_bitmap(self._rc, token, *src_rect, *dst_rect,
                                       opacity, interpolation)

    def destroy_bitmap(self, token):
        _native.render_destroy_gpu_bitmap(self._rc, token)


class WevvMoldWindow:
    """无边框自绘窗口（DWM 自定义框架 + 标题栏三控制按钮）"""

    EVENT = WEVVMOLD_EVENT

    def __init__(self, title='', width=320, height=240, left=0, top=0):
        self._h = _native.create_window(title, left, top, width, height)
        self._closed = False
        self.on_event = None
        self.on_render = None
        _native.set_event_callback(self._h, self._dispatch_event)
        _native.set_window_render_callback(self._h, self._dispatch_render)

    # ---- 回调桥接 ----
    def _dispatch_event(self, ev):
        cb = self.on_event
        if cb is not None:
            cb(ev)

    def _dispatch_render(self, rc):
        cb = self.on_render
        if cb is not None:
            cb(RenderContext(rc, self._h))

    # ---- 生命周期与操作 ----
    @property
    def handle(self):
        return self._h

    def show(self):
        _native.show_window(self._h)

    def hide(self):
        _native.hide_window(self._h)

    def set_title(self, title):
        _native.set_title(self._h, title)

    def run(self) -> int:
        return _native.run_window(self._h)

    def request_close(self):
        _native.request_close(self._h)

    def close(self):
        if not self._closed:
            self._closed = True
            self.on_event = None
            self.on_render = None
            _native.destroy_window(self._h)

    def should_quit(self) -> bool:
        return _native.should_quit(self._h)

    def start_timer(self, timer_id, interval_ms) -> bool:
        return _native.start_timer(self._h, timer_id, interval_ms)

    def stop_timer(self, timer_id):
        _native.stop_timer(self._h, timer_id)

    def request_redraw(self):
        _native.request_redraw(self._h)


class WevvMoldControl:
    """统一控件类（type_: CONTROL_TYPE 键名或 int）"""

    def __init__(self, type_, name=''):
        t = type_ if isinstance(type_, int) else CONTROL_TYPE[type_]
        self._h = _native.create_control(t, name)
        self._closed = False

    @property
    def handle(self):
        return self._h

    def close(self):
        if not self._closed:
            self._closed = True
            _native.destroy_control(self._h)

    # ---- 通用 ----
    def set_rect(self, l, t, r, b):
        _native.control_set_rect(self._h, l, t, r, b)

    def get_rect(self):
        return _native.control_get_rect(self._h)

    rect = property(get_rect, lambda self, v: self.set_rect(*v))

    def set_text(self, text):
        _native.control_set_text(self._h, text)

    def get_text(self) -> str:
        return _native.control_get_text(self._h)

    text = property(get_text, set_text)

    def set_checked(self, v):
        _native.control_set_checked(self._h, bool(v))

    def is_checked(self) -> bool:
        return _native.control_is_checked(self._h)

    checked = property(is_checked, set_checked)

    def set_value(self, v):
        _native.control_set_value(self._h, v)

    def get_value(self) -> float:
        return _native.control_get_value(self._h)

    value = property(get_value, set_value)

    def set_range(self, vmin, vmax):
        _native.control_set_range(self._h, vmin, vmax)

    def set_step(self, step):
        _native.control_set_step(self._h, step)

    def set_read_only(self, ro):
        _native.control_set_read_only(self._h, bool(ro))

    def set_disabled(self, v):
        _native.control_set_disabled(self._h, bool(v))

    def set_focused(self, v):
        _native.control_set_focused(self._h, bool(v))

    def get_state(self) -> int:
        return _native.control_get_state(self._h)

    def add_item(self, item):
        _native.control_add_item(self._h, item)

    def get_item_count(self) -> int:
        return _native.control_get_item_count(self._h)

    item_count = property(get_item_count)

    def set_selected(self, i):
        _native.control_set_selected(self._h, i)

    def get_selected(self) -> int:
        return _native.control_get_selected(self._h)

    selected = property(get_selected, set_selected)

    def add_child(self, child):
        h = child.handle if isinstance(child, WevvMoldControl) else child
        return _native.container_add_child(self._h, h)

    def container_clear(self):
        _native.container_clear(self._h)

    # ---- 事件注入与轮询 ----
    def dispatch(self, kind, x=0.0, y=0.0, key_code=0, char_code=0):
        k = kind if isinstance(kind, int) else CONTROL_EVENT[kind]
        return _native.control_dispatch(self._h, k, x, y, key_code, char_code)

    def dispatch_ex(self, kind, x=0.0, y=0.0, key_code=0, char_code=0, modifiers=0):
        k = kind if isinstance(kind, int) else CONTROL_EVENT[kind]
        return _native.control_dispatch_ex(self._h, k, x, y, key_code, char_code, modifiers)

    def dispatch_wheel(self, x, y, wheel_delta, ctrl=False, shift=False, alt=False):
        mods = (1 if ctrl else 0) | (2 if shift else 0) | (4 if alt else 0)
        return _native.control_dispatch_wheel(self._h, x, y, wheel_delta, mods)

    def poll(self, which) -> bool:
        w = which if isinstance(which, int) else CONTROL_POLL[which]
        return _native.control_poll(self._h, w)

    def poll_clicked(self):
        return self.poll('CLICKED')

    def poll_value_changed(self):
        return self.poll('VALUE_CHANGED')

    def poll_text_changed(self):
        return self.poll('TEXT_CHANGED')

    def poll_selection_changed(self):
        return self.poll('SELECTION_CHANGED')
