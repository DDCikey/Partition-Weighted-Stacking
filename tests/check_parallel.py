# DWT 并行执行的一致性自检（纯合成数据，秒级，不需要真实帧）
#
# 引擎把「叠加」按条带、「测星点」按帧分给多个工作进程。这两处的每一块都只看
#   自己的输入、互不引用，所以并行只该改变"谁在算"，不该改变"算什么"——
#   输出必须与串行**逐位相同**（这个不变量是论文与实验数据成立的前提）。
#
# 本脚本用合成数据把两种路径各跑一遍，逐位比对：
#   ① 读帧     帧数据槽位 + 帧级天空中位/噪声尺度 + 元数据（合成 FITS 小帧）
#   ② 测星点   逐帧 FWHM
#   ③ 叠加     成品像素 + N_eff 场 + 排异累加
#   ④ 兜底后端 父进程内存 list 后端（无盘可落时）与 memmap 后端逐位相同
#
# 用法：python tests/check_parallel.py
from __future__ import annotations

import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / 'core'), str(ROOT / 'ui')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pws                                                            # noqa: E402
from DWT_DetailCore import EXPTIME_REF, read_frame                    # noqa: E402

N, H, W = 12, 300, 400          # 帧数 / 高 / 宽：小到秒级，又足够覆盖多条带
STRIP = 37                      # 条高不整除 H，最后一条是残条（要照样对齐）


def main() -> int:
    rng = np.random.default_rng(20261001)
    dat = Path(tempfile.gettempdir()) / '_dwt_check_parallel.dat'
    mm = np.memmap(str(dat), dtype=np.float32, mode='w+', shape=(N, H, W))
    mm[:] = (rng.normal(0, 1, (N, H, W)) * 0.01).astype(np.float32)
    mm.flush()

    fs = pws.FrameSet(N, (H, W), resident=False, mm=mm, path=dat)
    fs.off = rng.uniform(0, 1e-2, N)      # 非零天空平移：走"读出时减 off"的 memmap 分支
    fs.sky0 = 5e-3                        # 非零公共天空：排异信号项零点也进回归
    C = rng.uniform(0.5, 1.5, N)
    S = rng.uniform(0.4, 2.0, N)
    R = rng.uniform(0, 1, (H, W)).astype(np.float32)
    A, B, rej_k = 20.0, 2.0, 3.0
    rej_gate = 0.4                        # 非默认值：把少数派闸门的参数链也进回归

    # ---- 串行：引擎当前的等效路径 ----
    s_stk = np.empty((H, W), np.float32)
    s_ne = np.empty((H, W), np.float32)
    s_rej = 0.0
    for y0 in range(0, H, STRIP):
        y1 = min(H, y0 + STRIP)
        Wt = pws.strip_weights(C, S, R[y0:y1], A, B)
        D = fs.stack_strips(y0, y1)
        s, ne, rr = pws.stack_strip(D, Wt, R[y0:y1], fs.sky0, rej_k=rej_k,
                                    rej_max_frac=rej_gate)
        s_stk[y0:y1] = s
        s_ne[y0:y1] = ne
        s_rej += float(rr) * float(s.size)

    # ---- 并行：与引擎里完全相同的分发方式（保序 map + 同一条带切分）----
    p_stk = np.empty((H, W), np.float32)
    p_ne = np.empty((H, W), np.float32)
    p_rej = 0.0
    with ProcessPoolExecutor(max_workers=4, initializer=pws._worker_open,
                             initargs=(str(dat), N, H, W, fs.off)) as ex:
        jobs = ((y0, min(H, y0 + STRIP), R[y0:y0 + STRIP], C, S, A, B, rej_k, fs.sky0,
                 rej_gate)
                for y0 in range(0, H, STRIP))
        for k, (s, ne, rr, _wm) in enumerate(ex.map(pws._task_strip, jobs, chunksize=1)):
            y0 = k * STRIP
            y1 = min(H, y0 + STRIP)
            p_stk[y0:y1] = s
            p_ne[y0:y1] = ne
            p_rej += float(rr) * float(s.size)

    # ---- 测星点：合成星场，并行与串行逐帧比对 ----
    def synth(seed: int) -> np.ndarray:
        f = np.zeros((H, W), np.float32)
        r = np.random.default_rng(seed)
        yy, xx = np.mgrid[-20:21, -20:21]
        for _ in range(14):
            xi, yi = int(r.integers(40, W - 40)), int(r.integers(40, H - 40))
            sig = 2.0 + r.uniform(-0.4, 0.4)
            g = np.exp(-(xx ** 2 + yy ** 2) / (2 * sig ** 2))
            f[yi - 20:yi + 21, xi - 20:xi + 21] += g.astype(np.float32)
        return f

    for i in range(N):
        mm[i] = (synth(i) + rng.normal(0, 0.004, (H, W))).astype(np.float32)
    mm.flush()
    stars = [(x, y) for y in range(40, H - 40, 40) for x in range(40, W - 40, 40)][:20]

    ser = np.array([pws.measure_fwhm(fs.frame(i), stars) for i in range(N)])
    with ProcessPoolExecutor(max_workers=4, initializer=pws._worker_open,
                             initargs=(str(dat), N, H, W, fs.off)) as ex:
        par = np.array([v for _i, v in ex.map(
            pws._task_fwhm, ((i, stars) for i in range(N)), chunksize=1)])

    fails = []

    def check(cond: bool, what: str) -> None:
        print(f'  [{"ok" if cond else "!!"}] {what}')
        if not cond:
            fails.append(what)

    # ---- ① 读帧：合成 FITS 小帧（逐帧不同曝光 → 走曝光归一与裁剪分支）----
    print('[并行一致性] ① 读帧（按帧切分）')
    from astropy.io import fits

    nr, hr, wr = 5, 200, 300
    y0r, x0r = 20, 10                       # 非零裁剪原点：验证 y0/x0 路径
    src = Path(tempfile.mkdtemp(prefix='dwt_check_read_'))
    files = []
    r2 = np.random.default_rng(7)
    for i in range(nr):
        arr = (r2.normal(0.05, 0.01, (240, 320)) + (i + 1) * 1e-3).astype(np.float32)
        hdu = fits.PrimaryHDU(arr)
        hdu.header['EXPTIME'] = 300.0 + 10 * i
        p = src / f'f{i}.fits'
        hdu.writeto(p, overwrite=True)
        files.append(p)

    ser_slots = np.zeros((nr, hr, wr), np.float32)
    ser_sky = np.zeros(nr)
    ser_sig = np.zeros(nr)
    ser_meta = []
    for i, f in enumerate(files):
        data, meta = read_frame(f)
        d = np.ascontiguousarray(data[y0r:y0r + hr, x0r:x0r + wr]).astype(np.float32)
        del data
        d *= np.float32(EXPTIME_REF / float(meta['exptime']))
        med = float(np.median(d))
        ser_sky[i] = med
        ser_sig[i] = 1.4826 * float(np.median(np.abs(d - med)))
        ser_slots[i] = d
        ser_meta.append({'name': f.name, 'exptime': float(meta['exptime'])})

    rd = Path(tempfile.gettempdir()) / '_dwt_check_parallel_rw.dat'
    mmr = np.memmap(str(rd), dtype=np.float32, mode='w+', shape=(nr, hr, wr))
    mmr[:] = 0
    mmr.flush()
    with ProcessPoolExecutor(max_workers=4, initializer=pws._worker_open_rw,
                             initargs=(str(rd), nr, hr, wr)) as ex:
        jobs = ((i, str(files[i]), EXPTIME_REF, y0r, x0r, hr, wr) for i in range(nr))
        got = list(ex.map(pws._task_read_frame, jobs, chunksize=1))
    mmr.flush()
    par_slots = np.array(mmr)
    par_sky = np.array([g[1] for g in got])
    par_sig = np.array([g[2] for g in got])
    par_meta = [{'name': g[3]['name'], 'exptime': float(g[3]['exptime'])}
                for g in got]          # 20261006 引擎的 _task_read_frame 现回传完整 meta 字典

    check(np.array_equal(ser_slots, par_slots),
          f'帧数据槽位逐位相同（最大差 {np.abs(ser_slots - par_slots).max():.3e}）')
    check(np.array_equal(ser_sky, par_sky), '帧级天空中位逐位相同')
    check(np.array_equal(ser_sig, par_sig), '帧级噪声尺度逐位相同')
    check(ser_meta == par_meta, '元数据（文件名 / 曝光）逐项相同')

    mmr = None
    rd.unlink(missing_ok=True)
    for p in files:
        p.unlink(missing_ok=True)
    src.rmdir()

    # ---- ② 测星点：合成星场 ----
    print('[并行一致性] ② 测星点（按帧切分）')
    check(np.array_equal(ser, par), f'逐帧 FWHM 逐位相同（{int(np.isfinite(ser).sum())}/{N} 帧有效）')
    check(np.isfinite(ser).any(), '合成星场确实测出了 FWHM（不是全 NaN 的假通过）')

    # ---- ③ 叠加：合成帧堆 ----
    print('[并行一致性] ③ 叠加（条带切分）')
    check(np.array_equal(s_stk, p_stk),
          f'成品逐位相同（最大差 {np.abs(s_stk - p_stk).max():.3e}）')
    check(np.array_equal(s_ne, p_ne),
          f'N_eff 场逐位相同（最大差 {np.abs(s_ne - p_ne).max():.3e}）')
    check(s_rej == p_rej, '排异累加相同（浮点累加顺序一致）')

    # ---- ④ 父进程内存兜底后端：一个盘都放不下帧文件时才走它 ----
    # 它不能跨进程（spawn 建子进程要整份复制帧），故自动退化串行；这里验的是它与
    #   memmap 后端**数值逐位相同**（off 一个读出时减、一个就地烘焙），以及
    #   测星点的两条实现（只取小窗 ↔ 物化整帧）给出同一个 FWHM。
    print('[并行一致性] ④ 父进程内存兜底后端（无盘可落时）：与 memmap 后端逐位相同')
    arrs = [np.array(mm[i], dtype=np.float32) for i in range(N)]
    for i in range(N):
        arrs[i] -= np.float32(fs.off[i])       # 兜底后端的口径：建集时就地烘焙
    fs_ram = pws.FrameSet(N, (H, W), resident=True, arrays=arrs, path=None)
    fs_ram.sky0 = fs.sky0                      # 排异信号项的零点，两种后端同值
    check(not fs_ram.parallelable, '兜底后端不可跨进程（自动退化串行）')
    check(np.array_equal(np.array([fs_ram.fwhm(i, stars) for i in range(N)]), ser),
          '兜底后端与 memmap 后端 FWHM 逐位相同（物化整帧 ↔ 只取星点小窗）')

    # 对照参考必须按**当前** mm 数据重算：③ 的 s_* 是在第 94-96 行合成星场**覆盖 mm 之前**
    #   用初始随机数据算出的，拿它比会必然不等（那不是后端差异，是数据换了）。
    base_stk = np.empty((H, W), np.float32)
    base_ne = np.empty((H, W), np.float32)
    base_rej = 0.0
    for y0 in range(0, H, STRIP):
        y1 = min(H, y0 + STRIP)
        Wt = pws.strip_weights(C, S, R[y0:y1], A, B)
        D = fs.stack_strips(y0, y1)
        s, ne, rr = pws.stack_strip(D, Wt, R[y0:y1], fs.sky0, rej_k=rej_k,
                                    rej_max_frac=rej_gate)
        base_stk[y0:y1] = s
        base_ne[y0:y1] = ne
        base_rej += float(rr) * float(s.size)

    q_stk = np.empty((H, W), np.float32)
    q_ne = np.empty((H, W), np.float32)
    q_rej = 0.0
    for y0 in range(0, H, STRIP):
        y1 = min(H, y0 + STRIP)
        Wt = pws.strip_weights(C, S, R[y0:y1], A, B)
        D = fs_ram.stack_strips(y0, y1)
        s, ne, rr = pws.stack_strip(D, Wt, R[y0:y1], fs_ram.sky0, rej_k=rej_k,
                                    rej_max_frac=rej_gate)
        q_stk[y0:y1] = s
        q_ne[y0:y1] = ne
        q_rej += float(rr) * float(s.size)
    check(np.array_equal(base_stk, q_stk) and np.array_equal(base_ne, q_ne)
          and base_rej == q_rej,
          f'兜底后端与 memmap 后端 成品/N_eff/排异 逐位相同'
          f'（最大差 {np.abs(base_stk - q_stk).max():.3e}）')

    fs.mm = None            # 先松开父进程的映射，Windows 才允许删文件
    del mm
    dat.unlink(missing_ok=True)

    print(f'\n结论：{"全部通过" if not fails else "★ " + str(len(fails)) + " 项不一致"}')
    return 1 if fails else 0


if __name__ == '__main__':
    raise SystemExit(main())