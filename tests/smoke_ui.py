# 20261005 DWT 界面冒烟自检（WevvMold 版，不弹窗口、不跑消息循环）
#
# 验四件事，前三条必须在没有数据的机器上通过：
#   ① 窗口能建起来，三档参数的控件与 PARAMS **一一对应**（多一个少一个都算失败）
#   ② 界面 ↔ PwsParams 的读写是闭环（写进去再读出来必须相等）
#   ③ 进度回调与日志着色不出错（引擎报的阶段都能找到中文说明，标签都能取到）
#   ④ 加 --run 时真跑一次小裁剪，验后台线程 / 队列 / 进度 / 收尾（需要 photos 数据）
#
# 用法：python DWT/tests/smoke_ui.py
#       python DWT/tests/smoke_ui.py --run
#       python DWT/tests/smoke_ui.py --run --shot preview.png
from __future__ import annotations

import argparse
import ctypes
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _p in (str(ROOT), str(ROOT / 'DWT' / 'core'), str(ROOT / 'DWT' / 'ui'),
           str(ROOT / 'WevvMoldGUI_for_python' / 'python')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from pws_params import GROUP_ORDER, PARAMS, PwsParams, params_of  # noqa: E402
import app as ui                                                  # noqa: E402
from logview import tag_of                                        # noqa: E402

fails: list = []


def check(cond: bool, what: str) -> None:
    print(f'  [{"ok" if cond else "!!"}] {what}')
    if not cond:
        fails.append(what)


def ui_diffs(x, y):
    """界面读回的参数与 PwsParams 比较。
    浮点按 spinbox 的精度网格（dec=2 → ±0.005）对齐：
    如 rej_gate 默认 1/3，界面只能表示 0.33，这不是闭环缺陷。"""
    out = []
    for p in PARAMS:
        va, vb = getattr(x, p.key), getattr(y, p.key)
        if isinstance(va, float) and isinstance(vb, float):
            if abs(va - vb) > 5e-3:
                out.append((p.key, va, vb))
        elif va != vb:
            out.append((p.key, va, vb))
    return out


def capture_window(hwnd: int, path: str) -> bool:
    """PrintWindow 抓客户区存 PNG（需要 PIL）。hwnd 无效时按标题兜底查找。"""
    try:
        from PIL import Image
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        if not user32.IsWindow(hwnd):
            hwnd = user32.FindWindowW(None, 'DWT · Partition-Weighted Stacking')
        if not hwnd:
            return False

        class RECT(ctypes.Structure):
            _fields_ = [('l', ctypes.c_long), ('t', ctypes.c_long),
                        ('r', ctypes.c_long), ('b', ctypes.c_long)]

        class BIH(ctypes.Structure):
            _fields_ = [('size', ctypes.c_uint32), ('w', ctypes.c_int32),
                        ('h', ctypes.c_int32), ('planes', ctypes.c_uint16),
                        ('bitcnt', ctypes.c_uint16), ('comp', ctypes.c_uint32),
                        ('imgsize', ctypes.c_uint32), ('xppm', ctypes.c_int32),
                        ('yppm', ctypes.c_int32), ('used', ctypes.c_uint32),
                        ('imp', ctypes.c_uint32)]

        rc = RECT()
        user32.GetClientRect(hwnd, ctypes.byref(rc))
        w, h = rc.r - rc.l, rc.b - rc.t
        if w <= 0 or h <= 0:
            return False
        hdc = user32.GetWindowDC(hwnd)
        mem = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        gdi32.SelectObject(mem, bmp)
        ok = user32.PrintWindow(hwnd, mem, 2)      # PW_RENDERFULLCONTENT
        pitch = (w * 3 + 3) // 4 * 4
        buf = ctypes.create_string_buffer(pitch * h)
        bi = BIH(size=40, w=w, h=h, planes=1, bitcnt=24, comp=0)
        gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
        img = Image.frombytes('RGB', (w, h), buf.raw, 'raw', 'BGR', pitch, -1)
        img.save(path)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(hwnd, hdc)
        return bool(ok) and Path(path).exists()
    except Exception:
        import traceback
        traceback.print_exc()
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description='DWT 界面冒烟自检')
    ap.add_argument('--run', action='store_true', help='额外真跑一次小裁剪（需要 photos 数据）')
    ap.add_argument('--shot', default='', help='把窗口截图存成 PNG（需配合 --run）')
    ap.add_argument('--photos', default=str(ROOT / 'photos' / '6888max'))
    ap.add_argument('--crop', type=int, default=512)
    ap.add_argument('--limit', type=int, default=4)
    a = ap.parse_args()

    # 从干净默认值起步：settings.json 里可能留着上一次真跑的参数，
    # 与"默认值即定稿工作点"的断言无关 → 把设置路径指到不存在的临时文件；
    # 自检过程本身也不回写（_save_settings 置空）。
    ui.settings_path = lambda: Path(tempfile.gettempdir()) / 'dwt_smoke_no_settings.json'

    win = ui.MainWindow(hidden=True)
    win._save_settings = lambda: None

    print('[冒烟] ① 控件与 PARAMS 一一对应')
    seen = set()
    for group in GROUP_ORDER:
        want = {p.key for p in params_of(group)}
        got = set(win.panel.editors.get(group, {}).keys())
        seen |= got
        check(got == want, f'{group}：界面 {len(got)} 项 == 定义 {len(want)} 项'
                           f'{"（差集 " + str(got ^ want) + "）" if got != want else ""}')
    check(seen == {p.key for p in PARAMS}, f'三档合计 {len(seen)} 项 == PARAMS {len(PARAMS)} 项')

    print('[冒烟] ② 界面 ↔ PwsParams 读写闭环')
    d = ui_diffs(win.panel.values(), PwsParams())
    check(not d, '默认值即定稿工作点（读出来 == PwsParams()）'
                 f'{"；" + "; ".join(f"{k}: 期望 {va!r} 实得 {vb!r}" for k, va, vb in d) if d else ""}')
    probe = PwsParams(photos='/tmp/x', out='/tmp/y', tag='probe', A=12.5, rej_k=0.0,
                      limit=7, keep_frames=True, crop=1024, center='100,200',
                      B=1.5, env_win_mult=3.5, env_eps=4.0,
                      env_p=1.5, r_floor=0.2, n_star=200, frames_dir='/tmp/z')
    win.panel.set_values(probe)
    back = win.panel.values()
    d = ui_diffs(back, probe)
    check(not d, '非默认值写进去再读出来相等（浮点按界面精度）'
                 f'{"；" + "; ".join(f"{k}: 期望 {va!r} 实得 {vb!r}" for k, va, vb in d) if d else ""}')

    print('[冒烟] ③ 进度、日志与状态')
    win.panel.set_values(PwsParams())
    for phase in ('读帧', '星表', '测星点', 'R 场', '叠加', '验收'):
        check(phase in ui.PHASE_TEXT, f'阶段「{phase}」有中文说明')
    win._t0 = time.perf_counter() - 12.3
    win._prog = ('叠加', 0.75, '20/26 条带')
    win._flush_progress(time.perf_counter())
    check(win._pct_text() == '75%', f'百分比显示 {win._pct_text()!r}')
    check('逐条带' in win._phase_text(), f'阶段显示 {win._phase_text()!r}')
    check('已用' in win._detail_text(), f'说明显示 {win._detail_text()!r}')
    win.log.append('[叠加] 完成 12.3s  排异剔除率 0.4%')
    win.log.append('  读帧 20/166  41s')
    check(tag_of('[叠加] 完成') == '叠加', '日志标签解析')
    check(tag_of('  读帧 20/166') == '', '无标签行解析为空')
    check(any('完成 12.3s' in ln for _s, ln, _c in win.log.lines), '日志行已入模型')

    win._set_state('err', '错误')
    check(win.chip.state == 'err', '状态胶囊属性可切换')

    if a.run:
        print('[冒烟] ④ 真跑一次（小裁剪）驱动 worker / 队列 / 收尾')
        # 用临时目录当输出，跑完即弃；设置文件里不落任何东西
        out = Path(tempfile.mkdtemp(prefix='dwt_smoke_'))
        win.panel.set_values(PwsParams(photos=a.photos, out=str(out),
                                      crop=int(a.crop), limit=int(a.limit),
                                      tag='smoke'))
        win.start()
        t_end = time.time() + 300
        while win.chip.state == 'run' and time.time() < t_end:
            win._on_tick(150)
            time.sleep(0.05)
        for _ in range(20):                       # 把队列里剩下的消息送完
            win._on_tick(150)
            time.sleep(0.01)
        check(win.chip.state == 'done', f'跑完状态 = {win.chip.state!r}')
        check(win.bar.frac == 1.0, f'进度条到 100%（{win.bar.frac}）')
        check(win._pct_text() == '100%', f'百分比 {win._pct_text()!r}')
        check(win.btn_run.enabled and not win.btn_stop.enabled, '按钮已复位')
        check(win.panel.editors['基础']['photos'].enabled, '参数面板已解锁')
        check((out / 'PWS_smoke.xisf').exists(), '成品已落盘')

        if a.shot:
            # 所见即所得：真窗口（含刚跑出的进度与日志）抓成 PNG
            ticks = [0]
            orig = win._on_event

            def ev(e):
                orig(e)
                if e.get('type') == ui.WevvMoldWindow.EVENT['TIMER_TICK'] \
                        and e.get('timer_id') == ui._TIMER_ID:
                    ticks[0] += 1
                    if ticks[0] == 6:
                        capture_window(win.win.handle, a.shot)
                        win.win.request_close()

            win.win.on_event = ev
            win.win.show()
            win.win.start_timer(ui._TIMER_ID, 150)
            win.win.run()
            check(Path(a.shot).exists(), f'截图已存 {a.shot}')

        print('[冒烟] ⑤ 终止路径（读帧阶段就掐）')
        out2 = Path(tempfile.mkdtemp(prefix='dwt_smoke_cancel_'))
        win.log.clear()
        win.panel.set_values(PwsParams(photos=a.photos, out=str(out2),
                                       crop=int(a.crop), limit=0, tag='cancel'))
        win.start()
        t0 = time.time()
        while time.time() - t0 < 0.8:                 # 跑起来再掐，确保是真终止而非空跑
            win._on_tick(150)
            time.sleep(0.05)
        win.cancel()
        t_end = time.time() + 120
        while win.chip.state == 'run' and time.time() < t_end:
            win._on_tick(150)
            time.sleep(0.05)
        for _ in range(20):
            win._on_tick(150)
            time.sleep(0.01)
        check(win.chip.text == '已终止', f'终止后状态胶囊 = {win.chip.text!r}')
        check(win.btn_run.enabled and not win.btn_stop.enabled, '终止后按钮已复位')
        check(win.panel.editors['基础']['photos'].enabled, '终止后参数已解锁')
        check(not (out2 / 'PWS_cancel.xisf').exists(), '终止后没有成品')
        check(any('终止' in ln for _s, ln, _c in win.log.lines),
              '日志里有终止记录')
        print()

    print()
    if fails:
        print(f'[冒烟] 失败 {len(fails)} 项')
        return 1
    print('[冒烟] 通过')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
