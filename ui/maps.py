# DWT 评价页：R 场 / N_eff 场可视化
#
# 数据来源：引擎随成品落盘的 <名>_R.npy 与 <名>_neff.npy（Float32，与成品同尺寸）。
# 成品可达数千万像素，整幅上传位图不现实：np.load(mmap_mode='r') 按整数块归并到
#   长边 ≤ MAX_SIDE，再经 rc.upload_bitmap 上色显示。
# 归并口径：R 取块内最大值（细节区只占 0.1%~0.2%，均值会被背景稀释到看不见），
#   N_eff 取块内均值（密度型量）。统计读数一律在全分辨率数组上计算，与引擎日志同口径。
#
# 页面由 app 直接渲染（不走 panel 的参数卡体系）：panel 对 MAPS_GROUP 返回
#   content_h=0 并隐藏全部参数控件。
# 解析在后台线程做，app 的定时器里 poll() 取回；位图 token 在渲染回调内上传/释放。

from __future__ import annotations

import queue
import threading
from pathlib import Path

import numpy as np

import style as S
from wevvmold import color
from widgets import Button, Widget

MAX_SIDE = 900              # 显示用降采样后的长边上限
MIN_ZOOM, MAX_ZOOM = 1.0, 12.0
_CARD_GAP = 16
_CARD_PAD = 14
_HDR_H = 52
_IMG_RAD = 10
_INFO_H = 68                # 卡片顶部标题 + 两行统计占用的高度
_LEG_H = 42                 # 卡片底部色标 + 刻度占用的高度

IMG_BG = color(16, 22, 31)


# ---------------------------------------------------------------- 色标
def _lut(stops):
    """由 (位置, RGB) 停靠点生成 256×4 uint8 查找表"""
    xs = np.array([s[0] for s in stops], np.float32)
    grid = np.linspace(0.0, 1.0, 256, dtype=np.float32)
    out = np.empty((256, 4), np.uint8)
    for ch in range(3):
        ys = np.array([s[1][ch] for s in stops], np.float32)
        out[:, ch] = np.clip(np.interp(grid, xs, ys), 0, 255).astype(np.uint8)
    out[:, 3] = 255
    return out


# R：0（纯逆方差）→ 青绿 → 琥珀 → 红（全压最锐帧）。暗底让细节区一眼可见。
LUT_R = _lut([(0.00, (26, 36, 50)),
              (0.35, (23, 58, 74)),
              (0.62, (15, 157, 144)),
              (0.82, (226, 168, 70)),
              (1.00, (192, 57, 43))])

# N_eff：viridis 近似，色盲友好、亮度单调。
LUT_N = _lut([(0.00, (68, 1, 84)),
              (0.25, (59, 82, 139)),
              (0.50, (33, 145, 140)),
              (0.75, (94, 201, 98)),
              (1.00, (253, 231, 37))])


def _rgba(arr, lut, vmax) -> bytearray:
    """arr → 位图字节。渲染端按 BGRA 取这 4 个字节（截屏实测校准），
    故此处把 RGB 查找表倒成 BGR；色标条仍用 RGB 原表绘制。"""
    scale = 255.0 / max(float(vmax), 1e-9)
    idx = np.clip(arr * scale, 0.0, 255.0).astype(np.uint8)
    px = lut[idx]
    out = np.empty_like(px)
    out[..., 0] = px[..., 2]
    out[..., 1] = px[..., 1]
    out[..., 2] = px[..., 0]
    out[..., 3] = 255
    return bytearray(out.tobytes())


def _pool(a, f, mode) -> np.ndarray:
    """整数块归并：mode='max' 取块内最大，'mean' 取块内均值"""
    h, w = int(a.shape[0]), int(a.shape[1])
    oh, ow = h // f, w // f
    out = np.empty((oh, ow), np.float32)
    if f == 1:
        out[...] = a[:oh, :ow]
        return out
    for i in range(oh):
        blk = np.array(a[i * f:(i + 1) * f, :ow * f], dtype=np.float32, copy=True)
        blk = blk.reshape(f, ow, f)
        if mode == 'max':
            out[i] = blk.max(axis=(0, 2))
        else:
            out[i] = blk.mean(axis=(0, 2))
    return out


class MapLayer:
    """一层可显示场：显示用降采样数组 + 全分辨率统计 + 上色后的 RGBA 缓冲"""

    __slots__ = ('kind', 'arr', 'pixels', 'factor', 'vmax', 'n', 'stats',
                 'path', 'shape')

    def __init__(self, kind, arr, factor, vmax, n, stats, path, shape):
        self.kind = kind
        self.arr = arr
        self.factor = factor
        self.vmax = float(vmax)
        self.n = int(n)
        self.stats = stats
        self.path = path
        self.shape = shape
        self.pixels = _rgba(arr, LUT_R if kind == 'R' else LUT_N, self.vmax)


def _load_dataset(job):
    """读一组 R / N_eff：显示用块归并 + 全分辨率统计（后台线程内调用）"""
    d = Path(job['out_dir'])
    stem = job['stem']
    rp = d / f'{stem}_R.npy'
    nth = d / f'{stem}_neff.npy'
    if not rp.exists() and not nth.exists():
        raise FileNotFoundError(f'找不到 {rp.name} 或 {nth.name}')
    out = {'label': job.get('label') or stem, 'stem': stem,
           'n': job.get('n'), 'R': None, 'N': None, 'error': ''}
    shape = None
    det = amb = None

    if rp.exists():
        ra = np.load(str(rp), mmap_mode='r')
        shape = (int(ra.shape[0]), int(ra.shape[1]))
        factor = max(1, int(np.ceil(max(shape) / float(MAX_SIDE))))
        det = ra > 0.5
        amb = ra < 0.1
        stats = {'mean': float(ra.mean()),
                 'max': float(ra.max()),
                 'gt50': float(np.count_nonzero(det)) / float(ra.size),
                 'lt10': float(np.count_nonzero(amb)) / float(ra.size)}
        out['R'] = MapLayer('R', _pool(ra, factor, 'max'), factor, 1.0, 0,
                            stats, rp, shape)
        del ra

    if nth.exists():
        na = np.load(str(nth), mmap_mode='r')
        shp = (int(na.shape[0]), int(na.shape[1]))
        if shape is not None and shp != shape:
            out['error'] = 'R 与 N_eff 尺寸不一致，无法对齐读数'
        else:
            shape = shape or shp
            factor = max(1, int(np.ceil(max(shape) / float(MAX_SIDE))))
            p10, p50, p999 = (float(v) for v in
                              np.percentile(na, (10.0, 50.0, 99.9)))
            nmax = float(na.max())
            det_med = (float(np.median(na[det]))
                       if det is not None and bool(det.any()) else None)
            amb_med = (float(np.median(na[amb]))
                       if amb is not None and bool(amb.any()) else None)
            vmax = nmax if nmax > 0 else 1.0
            if nmax > 0 and p999 < 0.95 * nmax:      # 长尾数据：色标收到 P99.9
                vmax = max(1.0, float(np.ceil(p999)))
            n = job.get('n') or int(round(nmax))
            stats = {'median': p50, 'p10': p10, 'min': float(na.min()),
                     'max': nmax, 'det_med': det_med, 'amb_med': amb_med}
            out['N'] = MapLayer('N', _pool(na, factor, 'mean'), factor, vmax,
                                n, stats, nth, shape)
        del na

    if out['R'] is None and out['N'] is None:
        raise FileNotFoundError('两组场文件都无法读取')
    out['n'] = out['n'] or (out['N'].n if out['N'] is not None else None)
    return out


class _Loader(threading.Thread):
    """后台解析：逐组读 npy → 归并 → 统计；结果走队列回主线程"""

    def __init__(self, jobs, q):
        super().__init__(daemon=True)
        self.jobs = jobs
        self.q = q

    def run(self):
        for i, job in enumerate(self.jobs):
            try:
                self.q.put(('ok', i, _load_dataset(job)))
            except Exception as e:
                self.q.put(('err', i, f'{type(e).__name__}: {e}'))
        self.q.put(('end',))


# ---------------------------------------------------------------- 图像控件
class MapView(Widget):
    """一层的自适应显示区：位图上传 / 缩放平移 / 十字读数"""

    redraw_on_move = True

    def __init__(self, kind):
        super().__init__()
        self.kind = kind
        self.layer = None
        self.empty_text = '等待叠加完成'
        self.on_cursor = None           # (u, v) 或 (None, None)
        self._token = None
        self._drop = False
        self._dst = (0.0, 0.0, 0.0, 0.0)
        self._cursor = None
        self._drag = None
        self.zoom = 1.0
        self.cx = 0.5
        self.cy = 0.5

    # ---- 数据 ----
    def set_layer(self, layer) -> None:
        if layer is self.layer:
            return
        self.layer = layer
        self._drop = True               # 旧 token 在下一帧渲染回调内释放
        self.zoom = 1.0
        self.cx = self.cy = 0.5
        self._cursor = None
        self._dst = (0.0, 0.0, 0.0, 0.0)

    def set_cursor(self, u, v) -> None:
        if u is None:
            self._cursor = None
        elif self.layer is not None:
            self._cursor = (min(0.9999, max(0.0, u)),
                            min(0.9999, max(0.0, v)))
        else:
            self._cursor = None

    def reset_view(self) -> None:
        self.zoom = 1.0
        self.cx = self.cy = 0.5

    # ---- 几何 ----
    def _inner(self):
        l, t, r, b = self.rect
        return (l + 3, t + 3, r - 3, b - 3)

    def _geom(self):
        il, it, ir, ib = self._inner()
        iw, ih = int(self.layer.arr.shape[1]), int(self.layer.arr.shape[0])
        bw, bh = ir - il, ib - it
        fit = min(bw / iw, bh / ih)
        dw, dh = iw * fit * self.zoom, ih * fit * self.zoom
        x0 = (il + ir) / 2 - self.cx * dw
        y0 = (it + ib) / 2 - self.cy * dh
        return x0, y0, x0 + dw, y0 + dh

    def _clamp(self):
        il, it, ir, ib = self._inner()
        iw, ih = int(self.layer.arr.shape[1]), int(self.layer.arr.shape[0])
        bw, bh = ir - il, ib - it
        fit = min(bw / iw, bh / ih)
        dw, dh = iw * fit * self.zoom, ih * fit * self.zoom
        if dw > bw:
            mx = bw / (2 * dw)
            self.cx = min(1 - mx, max(mx, self.cx))
        else:
            self.cx = 0.5
        if dh > bh:
            my = bh / (2 * dh)
            self.cy = min(1 - my, max(my, self.cy))
        else:
            self.cy = 0.5

    def _pick(self, x, y):
        if self.layer is None:
            return None
        il, it, ir, ib = self._inner()
        dl, dt, dr, db = self._dst
        if not (il <= x < ir and it <= y < ib):
            return None
        if not (dl <= x < dr and dt <= y < db):
            return None
        return ((x - dl) / max(1e-6, dr - dl), (y - dt) / max(1e-6, db - dt))

    # ---- 绘制 ----
    def render_content(self, rc):
        l, t, r, b = self.rect
        S.round_rect(rc, l, t, r, b, _IMG_RAD, S.BORDER)
        S.round_rect(rc, l + 1, t + 1, r - 1, b - 1, _IMG_RAD - 1, IMG_BG)
        if self._drop:
            self._drop = False
            if self._token is not None:
                rc.destroy_bitmap(self._token)
                self._token = None
        if self.layer is None:
            self._draw_empty(rc)
            return
        iw, ih = int(self.layer.arr.shape[1]), int(self.layer.arr.shape[0])
        if self._token is None:
            self._token = rc.upload_bitmap(iw, ih, self.layer.pixels)
        dl, dt, dr, db = self._geom()
        self._dst = (dl, dt, dr, db)
        il, it, ir, ib = self._inner()
        vl, vt = max(dl, il), max(dt, it)
        vr, vb = min(dr, ir), min(db, ib)
        if vr > vl and vb > vt:          # 用源矩形裁掉视口外部分（渲染器无裁剪区）
            sl = (vl - dl) / (dr - dl) * iw
            sr = (vr - dl) / (dr - dl) * iw
            st = (vt - dt) / (db - dt) * ih
            sb = (vb - dt) / (db - dt) * ih
            rc.draw_bitmap(self._token, (sl, st, sr, sb), (vl, vt, vr, vb),
                           1.0, 1)
        if self._cursor is not None:
            self._draw_cursor(rc, il, it, ir, ib)
        if self.zoom > 1.001:
            txt = f'{self.zoom:.1f}×'
            tw = S.text_width(txt, S.FS_SMALL) + 10
            S.round_rect(rc, ir - tw - 6, ib - 20, ir - 6, ib - 6, 4,
                         color(10, 14, 20, 170))
            rc.draw_text(txt, ir - tw - 6, ib - 20, ir - 6, ib - 6,
                         S.FS_SMALL, color(226, 233, 242), valign=1, halign=1)

    def _draw_empty(self, rc):
        il, it, ir, ib = self._inner()
        lines = S.wrap_text(self.empty_text, S.FS_BODY, max(80.0, ir - il - 40))
        h = len(lines) * 18
        y = (it + ib) / 2 - h / 2
        for ln in lines:
            rc.draw_text(ln, il + 16, y, ir - 16, y + 18, S.FS_BODY,
                         color(150, 163, 180), valign=1, halign=1)
            y += 18

    def _draw_cursor(self, rc, il, it, ir, ib):
        u, v = self._cursor
        dl, dt, dr, db = self._dst
        px = dl + u * (dr - dl)
        py = dt + v * (db - dt)
        if not (il < px < ir and it < py < ib):
            return
        line = color(235, 240, 246, 90)
        rc.fill_rect(px, it, px + 1, ib, line)
        rc.fill_rect(il, py, ir, py + 1, line)
        iw, ih = int(self.layer.arr.shape[1]), int(self.layer.arr.shape[0])
        col = min(iw - 1, int(u * iw))
        row = min(ih - 1, int(v * ih))
        val = float(self.layer.arr[row, col])
        if self.kind == 'R':
            txt = f'R = {val:.3f}'
        else:
            txt = f'N_eff = {val:.1f}'
        h, w = int(self.layer.shape[0]), int(self.layer.shape[1])
        txt += f'    x={int(u * w)} y={int(v * h)}'
        tw = S.text_width(txt, S.FS_SMALL) + 16
        bx = il + 8
        by = it + 8
        S.round_rect(rc, bx, by, bx + tw, by + 22, 5, color(10, 14, 20, 200))
        rc.draw_text(txt, bx + 8, by, bx + tw - 8, by + 22, S.FS_SMALL,
                     color(235, 240, 246), valign=1)

    # ---- 交互 ----
    def on_press(self, x, y) -> bool:
        if self.layer is None or not self.contains(x, y):
            return False
        p = self._pick(x, y)
        if p is None:
            return False
        self._drag = (x, y)
        return True

    def on_drag(self, x, y):
        if self._drag is None or self.layer is None:
            return
        dx = x - self._drag[0]
        dy = y - self._drag[1]
        self._drag = (x, y)
        dl, dt, dr, db = self._dst
        self.cx -= dx / max(1e-6, dr - dl)
        self.cy -= dy / max(1e-6, db - dt)
        self._clamp()
        self._dst = self._geom()
        p = self._pick(x, y)
        self._cursor = p
        if self.on_cursor is not None:
            self.on_cursor(*(p if p is not None else (None, None)))

    def on_release(self, x, y):
        self._drag = None

    def on_move(self, x, y):
        super().on_move(x, y)
        p = self._pick(x, y)
        if p != self._cursor:
            self._cursor = p
            if self.on_cursor is not None:
                self.on_cursor(*(p if p is not None else (None, None)))

    def on_leave(self):
        super().on_leave()
        self._drag = None
        if self._cursor is not None:
            self._cursor = None
            if self.on_cursor is not None:
                self.on_cursor(None, None)

    def on_wheel(self, x, y, delta) -> bool:
        if self.layer is None or not self.contains(x, y):
            return False
        p = self._pick(x, y)
        if p is None:
            return False
        il, it, ir, ib = self._inner()
        u, v = p
        z = self.zoom * (1.15 ** (delta / 120.0))
        self.zoom = min(MAX_ZOOM, max(MIN_ZOOM, z))
        self._clamp()
        dl, dt, dr, db = self._geom()
        dw, dh = dr - dl, db - dt
        self.cx = ((il + ir) / 2 - (x - u * dw)) / max(1e-6, dw)
        self.cy = ((it + ib) / 2 - (y - v * dh)) / max(1e-6, dh)
        self._clamp()
        self._dst = self._geom()
        p2 = self._pick(x, y)
        self._cursor = p2
        if self.on_cursor is not None:
            self.on_cursor(*(p2 if p2 is not None else (None, None)))
        return True


# ---------------------------------------------------------------- 页面
class MapsPage:
    """评价页：左 R 场、右 N_eff 场，共用一套十字读数"""

    def __init__(self):
        self.x = self.top = self.bot = self.width = 0.0
        self.datasets = []
        self.index = 0
        self.loading = False
        self._jobs = []
        self._q = queue.Queue()
        self._thread = None
        self._hdr_right = 0.0
        self.views = [MapView('R'), MapView('N')]
        for v in self.views:
            v.on_cursor = self._set_cursor
        self.btn_reload = Button('读取…', 'small', on_click=self.pick_files)
        self.btn_reload.tip = '打开已有的 <名>_R.npy 或 <名>_neff.npy'
        self.btn_reset = Button('复位视图', 'small', on_click=self.reset_view)
        self.btn_switch = Button('下一组', 'small', on_click=self.next_dataset)
        self.btn_switch.tip = '多滤镜时切换数据集'
        self.buttons = [self.btn_reload, self.btn_reset, self.btn_switch]
        self.widgets = list(self.buttons) + list(self.views)

    # ---- 数据入口 ----
    def load_result(self, res: dict) -> None:
        """一次叠加完成后自动载入：单组取 res，多组取 groups"""
        out_dir = res.get('out_dir') or ''
        groups = res.get('groups') or []
        jobs = []
        if groups:
            for g in groups:
                jobs.append({'label': g.get('filter') or g.get('stem', ''),
                             'stem': g.get('stem', ''),
                             'out_dir': out_dir,
                             'n': g.get('n'),
                             'shape': g.get('shape')})
        else:
            stem = res.get('stem') or ''
            if stem:
                jobs.append({'label': Path(stem).name, 'stem': stem,
                             'out_dir': out_dir, 'n': res.get('n'),
                             'shape': res.get('shape')})
        self._start([j for j in jobs if j['stem'] and j['out_dir']])

    def pick_files(self) -> None:
        """手动打开已有的场文件（多为上次运行的结果）"""
        try:
            import tkinter
            from tkinter import filedialog
            root = tkinter.Tk()
            root.withdraw()
            path = filedialog.askopenfilename(
                title='选择 R 场或 N_eff 场',
                filetypes=[('DWT 场文件', '*_R.npy *_neff.npy'),
                           ('NumPy 数组', '*.npy')])
            root.destroy()
        except Exception:
            return
        if not path:
            return
        p = Path(path)
        name = p.name
        if name.endswith('_R.npy'):
            stem = name[:-6]
        elif name.endswith('_neff.npy'):
            stem = name[:-9]
        else:
            stem = p.stem
        self._start([{'label': stem, 'stem': stem,
                      'out_dir': str(p.parent), 'n': None, 'shape': None}])

    def _start(self, jobs) -> None:
        if not jobs:
            return
        self._jobs = list(jobs)
        self.datasets = [None] * len(jobs)
        self.index = 0
        self.loading = True
        self._q = queue.Queue()
        self._thread = _Loader(self._jobs, self._q)
        self._thread.start()
        self._sync_views()

    def poll(self) -> bool:
        """主线程定时器里取回解析结果；返回是否有变化"""
        got = False
        while True:
            try:
                msg = self._q.get_nowait()
            except queue.Empty:
                break
            got = True
            if msg[0] == 'ok':
                self.datasets[msg[1]] = msg[2]
            elif msg[0] == 'err':
                job = self._jobs[msg[1]] if msg[1] < len(self._jobs) else {}
                self.datasets[msg[1]] = {
                    'label': job.get('label') or job.get('stem') or '',
                    'stem': job.get('stem') or '', 'n': job.get('n'),
                    'R': None, 'N': None, 'error': msg[2]}
            else:
                self.loading = False
        if got:
            self._sync_views()
        return got

    def current(self):
        if 0 <= self.index < len(self.datasets):
            return self.datasets[self.index]
        return None

    def _sync_views(self) -> None:
        d = self.current()
        for view, key, fname in ((self.views[0], 'R', '_R.npy'),
                                 (self.views[1], 'N', '_neff.npy')):
            view.set_layer(d.get(key) if d else None)
            if view.layer is not None:
                continue
            if d is None:
                view.empty_text = ('正在解析 R / N_eff…' if self.loading
                                   else '运行一次叠加后显示')
            elif d.get('error'):
                view.empty_text = d['error']
            else:
                view.empty_text = f'未找到对应的 {fname}'

    def next_dataset(self) -> None:
        if len(self.datasets) > 1:
            self.index = (self.index + 1) % len(self.datasets)
            self._sync_views()

    def reset_view(self) -> None:
        for v in self.views:
            v.reset_view()

    def _set_cursor(self, u, v) -> None:
        for view in self.views:
            view.set_cursor(u, v)

    # ---- 布局 ----
    def layout(self, x, top, bot, width) -> None:
        self.x, self.top, self.bot, self.width = x, top, bot, width
        right = x + width
        self.btn_reset.visible = bool(self.datasets)
        self.btn_switch.visible = len(self.datasets) > 1
        self.btn_reload.visible = True
        bx = right
        for b in (self.btn_reload, self.btn_reset, self.btn_switch):
            if not b.visible:
                b.set_rect(0, 0, 0, 0)
                continue
            bw = b.desired_w(20)
            b.set_rect(bx - bw, top + 6, bx, top + 32)
            bx -= bw + 8
        self._hdr_right = bx - 4
        cards_top = top + _HDR_H
        ch = max(120.0, bot - cards_top)
        cw = (width - _CARD_GAP) / 2
        for i, v in enumerate(self.views):
            l = x + i * (cw + _CARD_GAP)
            v.set_rect(l + _CARD_PAD, cards_top + _INFO_H,
                       l + cw - _CARD_PAD, cards_top + ch - _LEG_H)

    # ---- 绘制 ----
    def render(self, rc) -> None:
        self._draw_header(rc)
        cards_top = self.top + _HDR_H
        ch = max(120.0, self.bot - cards_top)
        cw = (self.width - _CARD_GAP) / 2
        d = self.current()
        for i in (0, 1):
            l = self.x + i * (cw + _CARD_GAP)
            r = l + cw
            self._draw_card(rc, l, cards_top, r, cards_top + ch, i, d)

    def _draw_header(self, rc) -> None:
        x, top = self.x, self.top
        title = '叠加评价'
        tw = S.text_width(title, S.FS_TITLE)
        rc.draw_text(title, x, top, x + tw + 4, top + 28, S.FS_TITLE,
                     S.TEXT, valign=1)
        d = self.current()
        lab = ''
        if self._jobs:
            job = self._jobs[self.index] if self.index < len(self._jobs) else {}
            lab = (d or {}).get('label') or job.get('label') or ''
            n = (d or {}).get('n') or job.get('n')
            if lab and n:
                lab = f'{lab} · {n} 帧'
        if lab:
            rc.draw_text(S.elide_middle(lab, S.FS_TITLE2,
                                        max(80.0, self._hdr_right - x - tw - 24)),
                         x + tw + 14, top, self._hdr_right, top + 28,
                         S.FS_TITLE2, S.TEXT2, valign=1)
        layer = (d or {}).get('R') or (d or {}).get('N')
        if layer is not None:
            sub = (f'R 取块内最大值 · N_eff 取块内均值（{layer.factor}× 降采样）'
                   ' · 滚轮缩放 · 拖动平移 · 悬停读数')
        elif self.loading:
            sub = '正在解析 R 场与 N_eff 场，请稍候…'
        else:
            sub = '运行一次叠加后自动载入；也可用右上「读取…」打开已有的场文件'
        rc.draw_text(S.elide_middle(sub, S.FS_SMALL,
                                    max(120.0, self._hdr_right - x)),
                     x, top + 27, self._hdr_right, top + 43,
                     S.FS_SMALL, S.HINT, valign=1)
        for b in self.buttons:
            if b.visible:
                b.render(rc)

    def _draw_card(self, rc, l, t, r, b, i, d) -> None:
        S.shadow(rc, l, t, r, b, 14, alpha=14)
        S.round_rect_border(rc, l, t, r, b, 14, S.BORDER, S.CARD)
        layer = (d or {}).get('R' if i == 0 else 'N')
        if i == 0:
            title = '分区权重场 R'
            lines = self._r_lines(layer)
            lut, left, mid, right = LUT_R, '0 逆方差', '0.5', '1 最锐'
        else:
            title = '等效帧数 N_eff'
            lines = self._n_lines(layer)
            lut, left, mid, right = LUT_N, '0', '', ''
            if layer is not None:
                mid = f'{layer.vmax / 2:g}'
                right = f'{layer.vmax:g}'
        rc.draw_text(title, l + _CARD_PAD, t + 8, r - _CARD_PAD, t + 30,
                     S.FS_TITLE2, S.TEXT, valign=1)
        yy = t + 30
        for k, ln in enumerate(lines):
            rc.draw_text(S.elide_middle(ln, S.FS_SMALL, r - l - 2 * _CARD_PAD),
                         l + _CARD_PAD, yy, r - _CARD_PAD, yy + 17,
                         S.FS_SMALL, S.TEXT2 if k == 0 else S.HINT, valign=1)
            yy += 17
        self.views[i].render(rc)
        self._draw_legend(rc, l + _CARD_PAD, b - 36, r - _CARD_PAD, b - 26,
                          lut, layer is not None, left, mid, right)

    @staticmethod
    def _r_lines(layer):
        if layer is None:
            return ['—', '—']
        st = layer.stats
        return [f'均值 {st["mean"]:.3f} · 最大 {st["max"]:.2f}',
                f'>0.5 细节区 {st["gt50"] * 100:.2f}% · '
                f'<0.1 朦胧区 {st["lt10"] * 100:.1f}%']

    @staticmethod
    def _n_lines(layer):
        if layer is None:
            return ['—', '—']
        st = layer.stats
        det = '—' if st['det_med'] is None else f'{st["det_med"]:.1f}'
        amb = '—' if st['amb_med'] is None else f'{st["amb_med"]:.1f}'
        cap = '' if abs(layer.vmax - layer.n) <= 0.5 else f'（满值 {layer.n}）'
        return [f'中位 {st["median"]:.1f} / {layer.n} 帧 · P10 {st["p10"]:.1f}'
                f' · 最小 {st["min"]:.1f}',
                f'细节区中位 {det} · 朦胧区中位 {amb}'
                f' · 色标 0–{layer.vmax:g}{cap}']

    @staticmethod
    def _draw_legend(rc, l, t, r, b, lut, active, left, mid, right) -> None:
        n = 64
        for i in range(n):
            c = lut[int(i * 255 / (n - 1))]
            col = (c[0] / 255.0, c[1] / 255.0, c[2] / 255.0, 1.0)
            x0 = l + (r - l) * i / n
            x1 = l + (r - l) * (i + 1) / n
            rc.fill_rect(x0, t, x1 + 0.5, b, col if active else
                         color(226, 231, 238))
        rc.draw_text(left, l, b + 2, l + 100, b + 18, S.FS_SMALL, S.HINT,
                     valign=1)
        if mid:
            rc.draw_text(mid, (l + r) / 2 - 50, b + 2, (l + r) / 2 + 50, b + 18,
                         S.FS_SMALL, S.HINT, valign=1, halign=1)
        if right:
            rc.draw_text(right, r - 120, b + 2, r, b + 18, S.FS_SMALL, S.HINT,
                         valign=1, halign=2)
