# DWT 引擎等价性回归
#
# 同一份数据、同一组参数，分别跑：
#     · DWT/core/pws.py            新引擎（界面与命令行共用这一份）
#     · test_tools/DWT_stack_v2.py 定稿基准
# 断言两者的 R 场 / N_eff / 逐帧权重表 / 成品像素 **逐位一致**（最大差 = 0）。
#
# 为什么必须有这一条：pws.py 是从 v2 抽出来的，抽取时改了导入路径、加了日志/进度/取消
# 三个钩子、把 C_i 的列表推导改成显式循环 —— 任何一处不小心都会静默改数值，
# 而"数值变了"在成品上未必看得出来。所以用逐位相等把口子堵死。
#
# 用法：python DWT/tests/check_equiv.py            # 默认 6888max / 裁剪 1024 / 6 帧
#       python DWT/tests/check_equiv.py --crop 0 --limit 0   # 全幅全帧（慢）
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import os
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / 'DWT' / 'core'
TOOLS = ROOT / 'test_tools'
for _p in (str(ROOT), str(CORE), str(TOOLS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pws                                                    # noqa: E402
from pws_params import PwsParams                              # noqa: E402

# 需要逐位比对的标量 / 数组（两边都会放在返回字典里）
ARR_KEYS = ('fwhms', 'C', 'S', 'sigma', 'neff', 'stack')
NUM_KEYS = ('fwhm_med', 'fwhm_out', 'sigma_psf', 'rej_rate')


def load_v2():
    """从文件路径加载 v2 基准（它不在包结构里，只能这样取）"""
    path = TOOLS / 'DWT_stack_v2.py'
    spec = importlib.util.spec_from_file_location('DWT_stack_v2_base', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cmp_array(name: str, a, b, fails: list) -> None:
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    if a.shape != b.shape:
        fails.append(f'{name}: 形状不同 {a.shape} vs {b.shape}')
        return
    d = np.abs(a - b)
    mx = float(np.nanmax(d)) if d.size else 0.0
    print(f'  {name:<12} 形状 {a.shape}  逐像素最大差 {mx:g}')
    if not (mx == 0.0 or (np.isnan(a).all() and np.isnan(b).all())):
        fails.append(f'{name}: 最大差 {mx:g} ≠ 0')


def cmp_num(name: str, a, b, fails: list) -> None:
    same = (a == b) or (isinstance(a, float) and isinstance(b, float)
                        and np.isnan(a) and np.isnan(b))
    print(f'  {name:<12} {a!r} vs {b!r}')
    if not same:
        fails.append(f'{name}: {a!r} ≠ {b!r}')


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='DWT 引擎等价性回归（pws.py vs DWT_stack_v2.py）')
    ap.add_argument('--photos', default=str(ROOT / 'photos' / '6888max'))
    ap.add_argument('--crop', type=int, default=1024, help='裁剪边长；0 = 全幅')
    ap.add_argument('--limit', type=int, default=6, help='帧数上限；0 = 全部')
    a = ap.parse_args(argv)

    photos = Path(a.photos)
    limit = int(a.limit) or 0
    tmp = Path(tempfile.mkdtemp(prefix='dwt_equiv_'))
    out_new, out_base = tmp / 'new', tmp / 'base'
    print(f'[等价性] 数据 {photos}  裁剪 {a.crop}  帧数 {limit or "全部"}')
    print(f'[等价性] 临时输出 {tmp}')

    # 本检查验的是**算法等价性**，两边必须走同一种条带切分：并行会把条带切细
    #   （每条带交给一个进程），而排异剔除率是"按条带求均值再累加"的量，
    #   切分不同会让它的浮点末位不同。故这里强制串行 —— 并行一致性由
    #   check_parallel.py 独立校验，不靠这一条覆盖。
    os.environ['DWT_NO_PARALLEL'] = '1'

    # 同一组参数：只走两边的默认工作点（默认值就是定稿工作点）
    with contextlib.redirect_stdout(io.StringIO()):
        print('[等价性] 跑新引擎 DWT/core/pws.py …')
        res_new = pws.run(PwsParams(photos=str(photos), out=str(out_new),
                                    crop=int(a.crop), limit=limit, tag='eq'))
    v2 = load_v2()
    with contextlib.redirect_stdout(io.StringIO()):
        print('[等价性] 跑基准 test_tools/DWT_stack_v2.py …')
        res_base = v2.run(photos, out_base, crop=int(a.crop),
                          limit=int(a.limit) or None, tag='eq')

    fails: list = []
    print('[等价性] 数值比对')
    for k in ARR_KEYS:
        cmp_array(k, res_new[k], res_base[k], fails)
    for k in NUM_KEYS:
        cmp_num(k, res_new[k], res_base[k], fails)
    cmp_num('stars 数', len(res_new['stars']), len(res_base['stars']), fails)
    for k, v in res_new['R_info'].items():
        cmp_num(f'R_info.{k}', v, res_base['R_info'][k], fails)

    # 附带文件也逐位比（R 场只在这里落盘）
    print('[等价性] 附带文件比对')
    for fname in ('R_eq.npy', 'neff_eq.npy', 'weights_eq.txt'):
        fa, fb = out_new / fname, out_base / fname
        if not (fa.exists() and fb.exists()):
            fails.append(f'{fname}: 缺文件（新 {fa.exists()} / 基准 {fb.exists()}）')
            continue
        if fname.endswith('.npy'):
            cmp_array(fname, np.load(fa), np.load(fb), fails)
        else:
            ta = fb_ = None
            ta = fa.read_text(encoding='utf-8')
            fb_ = fb.read_text(encoding='utf-8')
            print(f'  {fname:<12} 文本一致 {ta == fb_}')
            if ta != fb_:
                fails.append(f'{fname}: 文本不一致')

    print()
    if fails:
        print('[等价性] 失败：')
        for f in fails:
            print(f'  · {f}')
        return 1
    print('[等价性] 通过：所有比对项逐位相等')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
