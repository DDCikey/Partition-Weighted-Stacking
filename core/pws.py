# 本文件以 MIT 许可证发布，全文见 LICENSE，授权范围见 README.md。
"""分区权重叠加法（Partition-Weighted Stacking，PWS）引擎

参数由 core/pws_params.py 的 PwsParams 读入。
输入前提：已校准（暗/平/偏）、已对齐（旋转+平移）的帧；本软件不做校准与对齐。
输出：线性 Float32 灰度（全程只有线性运算：曝光归一常数、天空加性平移、
    加权平均），不做任何拉伸 / 归一 / 截断。

三类权重按式 W[i,r] = C_i^(A·R_r) · S_i^(B·(1−R_r)) 融合，
再逐像素归一使 mean_i(W) = 1：

    C_i  第 i 帧的清晰度权重（帧级标量）  = FWHM_中位 / FWHM_i   值越大表示该帧越锐利
    S_i  第 i 帧的信噪比权重（帧级标量）  = σ_中位 / σ_i          值越大表示该帧信噪比越高
    R_r  像素 r 处的分区权重（逐像素连续场）∈ [0,1]   1 = 细节区   0 = 朦胧区

C、S 为帧级标量，R 为唯一空间场：
      · R → 1 时 W ∝ C^A，权重只由清晰度决定
      · R → 0 时 W ∝ S^B，权重只由信噪比决定，朦胧区退化为逆方差加权
      · 指数上 A·R 与 B·(1−R) 随 R 线性插值，W 随 R 连续变化，无台阶与拼接痕迹
      · R 由亮度归一包络定义，径向轮廓只含 r/σ_psf，与星点亮度无关
      · σ_psf = FWHM 中位 / 2.3548，过渡位置由 σ_psf 定义而非绝对对比度阈值

    B = 2 由逆方差最优性给出（加权平均方差最小 ⇔ W ∝ 1/σ²），无需标定；
    方法参数中仅 A 需经验标定。σ_i、σ_psf 与包络窗均由图像自身统计量定标，
    不依赖目标绝对亮度、曝光、增益与滤镜，换目标无需重新标定。

依赖：
    photutils.detection.DAOStarFinder   星点检测
    photutils.psf.fit_fwhm              高斯 PSF 拟合 FWHM（CircularGaussianPRF）
    scipy.ndimage                       滤波
"""
from __future__ import annotations

import argparse
import contextlib
import io
import math
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy import ndimage

# core/pws.py → 仓库根是上一级；xisf_io.py / DWT_DetailCore.py 都在仓库根，
#   pws_params.py 与 pws.py 同目录（作为顶层模块导入，避免依赖包结构）
_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parent
for _p in (str(ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from DWT_DetailCore import EXPTIME_REF, read_frame                      # noqa: E402
from pws_params import PARAMS, PwsParams, cli_flag                      # noqa: E402

# ---------------------------------------------------------------------------
# 方法参数
# ---------------------------------------------------------------------------
# A：细节区清晰度幂上限（论文式 6，R=1 处 W = C^A）。论文正文载明：方法参数中仅 A
#   需经验标定，取值 20。
A_SHARP = 20.0
# B：朦胧区信噪比幂上限（论文式 6，R=0 处 W = S^B）。论文正文由逆方差最优性给出
#   B = 2（加权平均方差最小 ⇔ W ∝ 1/σ²），无需标定。
B_SNR = 2.0
# R 的映射取亮度归一包络（见 _r_envelope，论文式 8）。式 9、式 10 表明其径向轮廓
#   只含 r/σ_psf、与星点亮度无关，故换目标无需重新标定。
# 包络归一的局部极大窗：该倍数 × 2 × σ_psf（约 4σ_psf 见方）。
ENV_WIN_MULT = 2.0
# 包络分母所加的噪声项：该倍数 × σ（σ 为参考像 MAD 估计，论文式 8 的 εσ 项）。
ENV_EPS = 8.0
# 包络形状幂（论文式 8 的 η）。d/(M+εσ) 已随 r 快速衰减，取 1.0。
ENV_P = 1.0
# R 的全局下限：R_eff = R_FLOOR + (1−R_FLOOR)·R（R_FLOOR = 0 时 R_eff = R）。
#   取正值时，空白天区亦保留一部分清晰度偏好，则糊帧在全图被压低，细节区与外围的
#   点扩散函数差异缩小，抑制外围亮环；代价为空白天区等效帧数 N_eff 下降。0 为关闭。
R_FLOOR = 0.0
# 结构 RMS 的空间窗：该倍数 × σ_psf。
R_WIN_MULT = 2.0
# 逐像素排异阈值（论文式 11）：|D_{i,r} − a_r| > κ·s_r + m·R_r·max(a_r − b0, 0) 时剔除第 i
#   帧在该像素的取值。κ 以 σ 为单位，s_r = 1.4826·MAD_i(D_{i,r} − a_r)。
REJ_K = 3.5
# 少数派闸门（论文式 11 之后）：若被剔帧数超过该比例 × 总帧数，判为非孤立离群，该像素不排异。
REJ_MAX_FRAC = 1 / 3
# 固定星表星数（论文 3.5 节，取峰值降序前 N 颗）。
N_STAR = 120
# 参考像（论文 3.4 节）= 最锐的 k 帧的等权中位。
REF_SHARP_N = 8

# 各阶段的并行进程上限（实际进程数另受"核数−1"与内存预算约束，见 plan_workers）
#   叠加：每进程持一条带的临时量，内存随进程数线性增长，上限从保守取值
#   读帧：受磁盘带宽约束，增加进程无收益
#   测星点：仅读取星点小窗（每帧约 0.5 MiB），内存占用极低，上限可放宽至核数级
CAP_WORKERS = 8
CAP_FWHM = 12


# ---------------------------------------------------------------------------
# 引擎与界面之间的三个钩子（引擎本体不依赖界面代码；未绑定时输出至 stdout）
# ---------------------------------------------------------------------------

class Cancelled(Exception):
    """取消信号：在检查点抛出，由调用方清理并提示"""


_LOG_SINK: List = [None]        # callable(str)            → 界面追加日志
_PROGRESS: List = [None]        # callable(阶段, 百分比, 说明) → 界面进度
_CANCEL: List = [None]          # callable() -> bool       → 取消探针

# 阶段 → 全局百分比区间（单调递增；界面仅显示换算后的全局百分比）
_PHASE_SPAN = (
    ('读帧', 0.00, 0.25),
    ('星表', 0.25, 0.30),
    ('测星点', 0.30, 0.45),
    ('R 场', 0.45, 0.52),
    ('叠加', 0.52, 0.95),
    ('验收', 0.95, 1.00),
)
_SPAN = {name: (lo, hi) for name, lo, hi in _PHASE_SPAN}


def log(msg: str = '') -> None:
    """引擎唯一输出口：绑定日志钩子后写为日志行，未绑定则输出至 stdout（钩子异常时亦退回）"""
    sink = _LOG_SINK[0]
    if sink is None:
        print(msg, flush=True)
        return
    try:
        sink(msg)
    except Exception:
        print(msg, flush=True)


def _emit(phase: str, frac: float, text: str = '') -> None:
    """上报进度：frac 为本阶段内的完成度，此处换算为全局百分比后交由界面显示"""
    fn = _PROGRESS[0]
    if fn is None:
        return
    lo, hi = _SPAN.get(phase, (0.0, 1.0))
    try:
        fn(phase, lo + (hi - lo) * min(1.0, max(0.0, float(frac))), text)
    except Exception:
        pass


def _check_cancel() -> None:
    """取消检查点：读帧逐帧、测 C_i 逐帧、叠加逐条带各设一处"""
    fn = _CANCEL[0]
    if fn is not None and fn():
        raise Cancelled('用户终止')


def set_hooks(on_log=None, on_progress=None, should_cancel=None) -> None:
    """绑定或解绑三个钩子（传 None 即解绑）"""
    _LOG_SINK[0] = on_log
    _PROGRESS[0] = on_progress
    _CANCEL[0] = should_cancel


def parse_center(text: str) -> Optional[Tuple[int, int]]:
    """解析裁剪中心 'y,x'；留空返回 None，由 resolve_center 自动定位延展源"""
    t = (text or '').strip()
    if not t:
        return None
    parts = [v for v in t.replace('，', ',').split(',') if v.strip()]
    if len(parts) < 2:
        raise SystemExit(f'裁剪中心须写成 y,x 两个整数（收到 {text!r}）')
    return (int(float(parts[0])), int(float(parts[1])))


# ---------------------------------------------------------------------------
# 阶段 0：帧加载与帧级归一
# ---------------------------------------------------------------------------

def resolve_center(photos: Path, crop: int,
                   center: Optional[Tuple[int, int]]) -> Optional[Tuple[int, int]]:
    """裁剪中心留空时自动定位延展源（调用核心层 locate_extended_source，以 32 px 分块中值
    法定位最亮延展团）。中心不写死坐标：若中心固定，换目标时可能裁到空白天区，致 R 场与
    星表失效。全幅（crop=0）时返回 None。
    """
    if not crop or crop <= 0 or center is not None:
        return center
    from DWT_DetailCore import locate_extended_source
    files = sorted(photos.glob('*.xisf')) or sorted(photos.glob('*.fit*'))
    with contextlib.redirect_stdout(io.StringIO()):
        d0, _ = read_frame(files[0])
    cy, cx = locate_extended_source(np.asarray(d0, dtype=np.float32), bin_px=32)
    del d0
    log(f'[裁剪] 自动定位延展源中心 (y,x)=({cy},{cx}) → {crop}² 窗')
    return (int(cy), int(cx))


def pick_frames_dir(need_bytes: int, want: Optional[str] = None) -> Path:
    """为 memmap 帧文件选择磁盘：要求可用空间为需求量的 1.25 倍。

    全幅 166 帧约需 40 GiB 临时空间；若写入过程中磁盘写满，将失败于已读取上百帧之后，
    因此在运行前按可用空间选盘，并输出所选目录。
      优先级（取第一个满足余量者）：
        ① 用户 --frames-dir  ② 项目盘 _DWT_frames  ③ 系统 TEMP  ④ 其余固定盘按可用空间降序
    """
    import shutil
    need = int(need_bytes * 1.25)
    head: List[Path] = []
    if want:
        head.append(Path(want))
    if not getattr(sys, 'frozen', False):
        # 打包版不向安装目录写入临时帧文件（安装目录可能只读）；仅源码运行时使用项目目录
        head.append(Path(__file__).resolve().parent / '_DWT_frames')
    tmp = os.environ.get('TEMP', '') or os.environ.get('TMP', '')
    if tmp:
        head.append(Path(tmp) / 'DWT_frames')

    def free_of(p: Path):
        try:
            anchor = p if p.exists() else p.parent
            if not anchor.exists():
                return None, 0
            return anchor, shutil.disk_usage(str(anchor)).free
        except OSError:
            return None, 0

    seen, head_ok, rest = set(), [], []
    for p in head:
        anchor, free = free_of(p)
        if anchor is None or str(anchor).lower() in seen:
            continue
        seen.add(str(anchor).lower())
        head_ok.append((p, free))
    for letter in 'CDEFGHIJKLMNOPQRSTUVWXYZ':
        root = Path(f'{letter}:\\')
        if not root.exists() or str(root).rstrip('\\').lower() in seen:
            continue
        seen.add(str(root).rstrip('\\').lower())
        try:
            rest.append((root / 'DWT_frames', shutil.disk_usage(str(root)).free))
        except OSError:
            continue
    rest.sort(key=lambda t: -t[1])                   # 其余盘按余量降序
    for p, free in head_ok + rest:                   # head 保序优先，其余按余量
        if free >= need:
            p.mkdir(parents=True, exist_ok=True)
            log(f'[memmap] 帧文件目录 {p}（该盘余量 {free / 2 ** 30:.1f} GiB，'
                f'需 {need / 2 ** 30:.1f} GiB）')
            return p
    raise SystemExit(f'没有任何盘能放下 {need / 2 ** 30:.1f} GiB 的 memmap 帧文件'
                     f'（需要 1.25× 余量）。请用 --frames-dir 指定空间足够的磁盘。')


class FrameSet:
    """帧集合。两种后端——文件支撑的 memmap（常规）与父进程内存 list（无可用磁盘时）——
    对外的数据形状均为 (n, h, w) 的 float32 数组，接口一致。

    memmap 后端：全幅单帧 6388×9576×4 B = 244 MiB，166 帧共 40.6 GiB，超出内存容量；
               而排异需按同一像素跨帧比较，条带内层必须是帧，无法以"帧外层循环 + 累加器"
               规避随机访问，故落盘为 memmap，按条带随机读取。
               采用文件支撑的 memmap：页文件支撑的共享段在 Windows 上按整段大小计入系统
               提交量，多个工作进程各自映射十余 GiB 的段会失败于 WinError 1450/1455
               （系统资源不足 / 页面文件太小）；文件支撑的映射其脏页由文件本身承载、不计入
               提交量，且文件页驻留于系统缓存，读写速度与内存一致。
    父进程内存 list 仅用于"所有磁盘均无法容纳"的情形：其不可跨进程（spawn 建子进程需将
               全部帧 pickle 传入），该后端因此退化为串行，见 prepare_frames。

    天空加性平移（off_i）统一在读出时相减，两种后端一致：
               memmap：盘上数据即"曝光归一后"，off_i 于读出时相减；
               内存 list：建集时原位烘焙（将 off 减入数据并置 0），读出时不再相减。
               两后端均只做 D − 常数。
    """

    def __init__(self, n: int, shape: Tuple[int, int], resident: bool,
                 arrays: Optional[List[np.ndarray]] = None,
                 mm: Optional[np.ndarray] = None, path: Optional[Path] = None):
        self.n = int(n)
        self.shape = (int(shape[0]), int(shape[1]))
        self.resident = bool(resident)
        self.arrays = arrays       # 内存 list 后端（父进程内存，不可跨进程，仅串行）
        self.mm = mm               # memmap 视图（可跨进程）
        self.path = path           # memmap 的文件路径（内存 list 后端为 None）
        self.skies = np.zeros(n, dtype=np.float64)
        self.sigma = np.zeros(n, dtype=np.float64)
        self.off = np.zeros(n, dtype=np.float64)
        self.sky0 = 0.0            # 平移后的公共天空水平（排异信号项的零点）
        self.metas: List[Dict] = []

    def __len__(self) -> int:
        return self.n

    def frame(self, i: int) -> np.ndarray:
        """第 i 帧全幅（曝光归一、天空平移后）"""
        if self.mm is None:
            return self.arrays[i]
        return self.mm[i] - np.float32(self.off[i])

    def strip(self, i: int, y0: int, y1: int) -> np.ndarray:
        """第 i 帧的 [y0,y1) 行条带"""
        if self.mm is None:
            return self.arrays[i][y0:y1]
        return self.mm[i, y0:y1] - np.float32(self.off[i])

    def stack_strips(self, y0: int, y1: int) -> np.ndarray:
        """将条带内所有帧堆叠为 (n, y1−y0, W)——排异与加权平均的唯一输入"""
        return np.stack([self.strip(i, y0, y1) for i in range(self.n)])

    def fwhm(self, i: int, stars: List[Tuple[int, int]]) -> float:
        """第 i 帧在固定星表上的 FWHM 中位，仅读取星点小窗，不物化整帧"""
        if self.mm is None:
            return measure_fwhm(self.arrays[i], stars)
        return measure_fwhm_frame(self.mm, i, self.off, stars)

    @property
    def parallelable(self) -> bool:
        """是否可跨进程交给工作进程（memmap 可以，内存 list 不可以）"""
        return self.mm is not None

    @property
    def reader(self) -> Tuple[object, tuple]:
        """工作进程打开本帧集所用的 (initializer, initargs)"""
        return _worker_open, (str(self.path), self.n, self.shape[0], self.shape[1], self.off)

    @property
    def writer(self) -> Tuple[object, tuple]:
        """读帧阶段（需写入帧数据）的 (initializer, initargs)"""
        return _worker_open_rw, (str(self.path), self.n, self.shape[0], self.shape[1])

    @property
    def resident_bytes(self) -> int:
        """帧数据占用的系统内存量，用于计算叠加阶段的条带高度所需内存预算。

        帧在盘上时该部分内存由系统缓存承载，但与工作进程争夺内存，故仍自预算中扣除
        （条带取低而不取高，以避免缓存被挤出后反复读盘）。退回父进程内存 list 时，
        此值记为该进程自身的占用。
        """
        return self.n * self.shape[0] * self.shape[1] * 4 if self.resident else 0

    def close(self, keep: bool = False) -> None:
        if self.mm is not None:
            self.mm.flush()
            del self.mm
            self.mm = None
        self.arrays = None
        if not keep and self.path is not None and self.path.exists():
            try:
                self.path.unlink()
                log(f'[memmap] 已删除临时帧文件 {self.path}')
            except OSError as e:
                log(f'[memmap] 删除失败（可手动删）：{self.path}  {e}')


def prepare_frames(photos: Path, crop: int, center: Optional[Tuple[int, int]],
                   limit: Optional[int] = None, frames_dir: str = '',
                   mem_frac: float = 0.55, keep_frames: bool = False) -> FrameSet:
    """读帧 → 裁剪 → float32 → 曝光归一（×EXPTIME_REF/t_i），并逐帧测量天空中位与噪声
    尺度；按内存预算决定驻留内存或落 memmap。

    曝光归一须最先行：输入帧允许曝光时间不同，归一后各帧等价于同一曝光时长，σ_i 方可
    直接跨帧比较（S_i 的前提）。
    天空中位与 σ_i 在同一遍历中一并计算：memmap 后端若事后再算，须将 40 GiB 自盘上重读。
    """
    files = sorted(photos.glob('*.xisf')) or sorted(photos.glob('*.fit*'))
    if limit:
        files = files[:limit]
    if not files:
        raise SystemExit(f'{photos} 下没有帧')
    n = len(files)
    log(f'[帧] {n} 帧  目录 {photos}')

    with contextlib.redirect_stdout(io.StringIO()):
        probe, _ = read_frame(files[0])
    fh, fw = probe.shape[:2]
    del probe
    if crop and crop > 0:
        h = w = min(int(crop), fh, fw)
    else:
        h, w = fh, fw
    if crop and crop > 0:
        cy, cx = (h // 2, w // 2) if center is None else (int(center[0]), int(center[1]))
        half = crop // 2
        y0 = max(0, min(cy - half, fh - crop))
        x0 = max(0, min(cx - half, fw - crop))
    else:
        y0 = x0 = 0

    need = n * h * w * 4
    try:
        import psutil
        avail = int(psutil.virtual_memory().available)
    except Exception:
        avail = 8 * 2 ** 30

    # 一律落盘 memmap（见 FrameSet 文档：页文件支撑的共享段在 Windows 上按整段大小计入
    #   提交量，装不下即 1450/1455；文件支撑的映射不计入提交量，其页仍驻留系统缓存）。
    #   仅当"所有磁盘均无法容纳"时退回父进程内存 list——该后端退化为串行。
    #   内存是否足够仅用于提示：可装入内存时，盘上数据基本全程驻留于缓存。
    resident = need <= int(avail * mem_frac)
    mm = None
    mmpath = None
    arrays: Optional[List[np.ndarray]] = None
    try:
        mmpath = pick_frames_dir(need, frames_dir or None) / f'pws_frames_{h}x{w}_{n}.dat'
        mm = np.memmap(str(mmpath), dtype=np.float32, mode='w+', shape=(n, h, w))
    except SystemExit as e:
        log(f'[帧] 没有盘能放下 {need / 2 ** 30:.2f} GiB 的帧文件 → 退回父进程内存'
            f'（该组数据将串行）：{e}')
        mmpath, mm, resident = None, None, True
        arrays = []
    log(f'[帧] 形状 ({h},{w})  曝光归一到 {EXPTIME_REF:g}s  需 {need / 2 ** 30:.2f} GiB  '
        f'可用 {avail / 2 ** 30:.1f} GiB → '
        f'{"父进程内存（串行）" if mm is None else "memmap 流式（并行）"}')

    fs = FrameSet(n, (h, w), resident, arrays=arrays, mm=mm, path=mmpath)
    t0 = time.perf_counter()
    # 逐帧相互独立（各自读取源文件、各自写入槽位），可分派给多进程。
    #   工作进程各自只读打开同一帧文件，直接写入所属槽位，
    #   父进程仅收回三个标量（帧号、天空中位、σ）。
    #   每进程内存按"整帧 × 3"估计：read_frame 的整帧缓冲 + float32 裁剪副本
    #   + median 内部副本（np.median 对浮点数组会先复制一份）。
    ini_rw, ini_rw_args = fs.writer
    nw = 0 if (not fs.parallelable or _no_parallel()) \
        else plan_workers(fh * fw * 4 * 3, '读帧')
    try:
        if nw > 1:
            fs.metas = [None] * n
            ex = ProcessPoolExecutor(max_workers=nw, initializer=ini_rw,
                                     initargs=ini_rw_args)
            try:
                jobs = ((i, str(files[i]), EXPTIME_REF, y0, x0, h, w) for i in range(n))
                for i, med, sig, name, exp in ex.map(_task_read_frame, jobs, chunksize=1):
                    _check_cancel()
                    fs.skies[i] = med
                    fs.sigma[i] = sig
                    fs.metas[i] = {'name': name, 'exptime': exp}
                    if (i + 1) % 20 == 0:
                        log(f'  读帧 {i + 1}/{n}  {time.perf_counter() - t0:.0f}s')
                    _emit('读帧', (i + 1) / max(n, 1), f'{i + 1}/{n} 帧')
            finally:
                ex.shutdown(wait=True, cancel_futures=True)
        else:
            for i, f in enumerate(files):
                _check_cancel()                     # 读帧阶段亦须可终止
                with contextlib.redirect_stdout(io.StringIO()):
                    data, meta = read_frame(f)
                d = np.ascontiguousarray(data[y0:y0 + h, x0:x0 + w]).astype(np.float32)
                del data
                d *= np.float32(EXPTIME_REF / float(meta['exptime']))
                med = float(np.median(d))
                fs.skies[i] = med
                fs.sigma[i] = 1.4826 * float(np.median(np.abs(d - med)))
                if mm is not None:
                    mm[i] = d
                else:
                    arrays.append(d)
                del d
                fs.metas.append({'name': f.name, 'exptime': float(meta['exptime'])})
                if (i + 1) % 20 == 0:
                    log(f'  读帧 {i + 1}/{n}  {time.perf_counter() - t0:.0f}s')
                _emit('读帧', (i + 1) / max(n, 1), f'{i + 1}/{n} 帧')
    except BaseException:
        # 读帧中途失败（取消 / 工作进程异常 / 磁盘写满）均须清除未完成的临时帧文件，
        #   否则一块数十 GiB 的文件将持续占用磁盘；清除后再将异常上抛
        fs.close(keep=keep_frames)
        raise

    # 天空加性平移：锚帧为天空最接近全批中位的那一帧。两种后端均只记 off_i，
    #   于读出时相减（见 FrameSet.strip）——仅内存 list 后端需就地烘焙。
    anchor = int(np.argmin(np.abs(fs.skies - np.median(fs.skies))))
    sky0 = float(fs.skies[anchor])
    fs.off = fs.skies - sky0
    fs.sky0 = sky0
    if arrays is not None:
        for i in range(n):
            arrays[i] -= np.float32(fs.off[i])
        fs.off[:] = 0.0            # 已就地烘焙，读出时不再相减
    if mmpath is not None:
        mm.flush()
    span = float(fs.skies.max() - fs.skies.min())
    log(f'[帧] 耗时 {time.perf_counter() - t0:.1f}s  '
        f'{"父进程内存 " + f"{fs.resident_bytes / 2 ** 30:.2f} GiB" if mm is None else f"盘上 {need / 2 ** 30:.2f} GiB（{mmpath.parent}）"}')
    log(f'[归一] 天空 {fs.skies.min():.4e}~{fs.skies.max():.4e}  锚帧 #{anchor}={sky0:.4e}  '
        f'平移极差 {span:.3e}（天空的 {span / max(sky0, 1e-30):.2%}）')
    return fs


def frame_sigma(fs: FrameSet) -> np.ndarray:
    """逐帧噪声尺度 σ_i = 1.4826 × MAD（天空背景主导，稳健，且不受星点与目标影响）。
    已在 prepare_frames 的读帧遍历中量得，此处直接取用。
    """
    return fs.sigma


# ---------------------------------------------------------------------------
# 阶段 A：C_i（星点 FWHM）
# ---------------------------------------------------------------------------

def build_star_table(ref: np.ndarray, n_star: int = N_STAR,
                     iso_mult: float = 5.0) -> List[Tuple[int, int]]:
    """固定星表：DAOStarFinder 检测 → 未饱和 → 孤立 → 取最亮 n_star 颗。

    须固定星表、各帧测量同一批位置：逐帧检测时阈值为 5×该帧噪声，模糊帧检出偏少，
              将使"帧质量"混入"星点强度"，导致模糊帧被系统性高估。
    """
    import photutils
    if hasattr(photutils, 'future_column_names'):
        photutils.future_column_names = True
    from photutils.detection import DAOStarFinder

    sky = float(np.median(ref))
    nz = 1.4826 * float(np.median(np.abs(ref - sky)))
    fwhm0 = 6.0
    tbl = DAOStarFinder(fwhm=fwhm0, threshold=5.0 * nz, exclude_border=True)(ref - sky)
    if tbl is None or len(tbl) == 0:
        return []
    names = tbl.colnames
    xk = 'x_centroid' if 'x_centroid' in names else 'xcentroid'
    yk = 'y_centroid' if 'y_centroid' in names else 'ycentroid'
    x = np.asarray(tbl[xk], float)
    y = np.asarray(tbl[yk], float)
    pk = np.asarray(tbl['peak'], float)

    vmax = float(np.max(ref))
    keep = pk < 0.8 * vmax
    x, y, pk = x[keep], y[keep], pk[keep]
    order = np.argsort(pk)[::-1]
    x, y, pk = x[order], y[order], pk[order]

    # 孤立判据：与已选星的最小距离 ≥ iso_mult × 5.0 px（默认 25 px），确保窗口内无混星
    from scipy.spatial import cKDTree
    tree = cKDTree(np.column_stack([x, y]))
    sel: List[Tuple[int, int]] = []
    h, w = ref.shape
    margin = 26
    for i in range(len(x)):
        xi, yi = int(round(x[i])), int(round(y[i]))
        if xi < margin or yi < margin or xi >= w - margin or yi >= h - margin:
            continue
        if sel:
            nb = tree.query((x[i], y[i]), k=2)[0][1]
            if nb < iso_mult * 5.0:
                continue
        sel.append((xi, yi))
        if len(sel) >= n_star:
            break
    return sel


def _fwhm_each(kut, stars: List[Tuple[int, int]], fwhm_guess: float,
               half: int, fit_shape: int) -> np.ndarray:
    """逐星测量高斯 PSF 的 FWHM；kut(xi, yi) 返回单颗星的小窗（越界返回 None）。

    小窗有两种来源（整帧切片 / 帧后端直接读取），数值路径相同。
    """
    from photutils.psf import fit_fwhm
    out = np.full(len(stars), np.nan, dtype=np.float64)
    for j, (xi, yi) in enumerate(stars):
        cut = kut(xi, yi)
        if cut is None:
            continue
        p = np.asarray(cut).astype(np.float64)
        yy, xx = np.mgrid[-half:half + 1, -half:half + 1]
        rr = np.hypot(xx, yy)
        p -= float(np.median(p[(rr >= half - 3) & (rr <= half)]))
        try:
            v = float(np.atleast_1d(
                fit_fwhm(p, xypos=[(half, half)], fwhm=fwhm_guess, fit_shape=fit_shape))[0])
        except Exception:
            continue
        if np.isfinite(v) and 0.5 < v < 40.0:
            out[j] = v
    return out


def measure_fwhm_each(img: np.ndarray, stars: List[Tuple[int, int]],
                      fwhm_guess: float = 6.0, half: int = 16,
                      fit_shape: int = 29) -> np.ndarray:
    """逐星测量高斯 PSF 的 FWHM，返回与 stars 等长的数组（失败或越界为 NaN）。

    以高斯 PSF 拟合求 FWHM：拟合口径不受翼部流量影响，是星点锐度的稳定度量。
    逐星返回，与 stars 等长。
    """
    h, w = img.shape

    def kut(xi: int, yi: int):
        if xi - half < 0 or yi - half < 0 or xi + half >= w or yi + half >= h:
            return None
        return img[yi - half:yi + half + 1, xi - half:xi + half + 1]

    return _fwhm_each(kut, stars, fwhm_guess, half, fit_shape)


def measure_fwhm_frame(mm: np.ndarray, i: int, off: np.ndarray,
                       stars: List[Tuple[int, int]], fwhm_guess: float = 6.0,
                       half: int = 16, fit_shape: int = 29) -> float:
    """自帧后端（memmap）直接读取星点小窗，测量 FWHM 中位——不物化整帧。

    单帧 244 MiB，而 120 颗星的小窗合计约 0.5 MiB；若先物化整帧再切片，
    则该步需为 0.5 MiB 读写 244 MiB。直接读取小窗可将该步开销降至可忽略。
    """
    offi = np.float32(off[i])
    _n, h, w = mm.shape

    def kut(xi: int, yi: int):
        if xi - half < 0 or yi - half < 0 or xi + half >= w or yi + half >= h:
            return None
        return mm[i, yi - half:yi + half + 1, xi - half:xi + half + 1] - offi

    v = _fwhm_each(kut, stars, fwhm_guess, half, fit_shape)
    v = v[np.isfinite(v)]
    if len(v) < 5:
        return float('nan')
    return float(np.median(v))


def measure_fwhm(img: np.ndarray, stars: List[Tuple[int, int]],
                 fwhm_guess: float = 6.0, half: int = 16,
                 fit_shape: int = 29) -> float:
    """固定星表上的 FWHM 中位（成品验收以此数值为准）"""
    v = measure_fwhm_each(img, stars, fwhm_guess, half, fit_shape)
    v = v[np.isfinite(v)]
    if len(v) < 5:
        return float('nan')
    return float(np.median(v))


# ---------------------------------------------------------------------------
# 阶段 B：R_r（分区权重，逐像素连续场）
# ---------------------------------------------------------------------------

def _r_envelope(sref: np.ndarray, sigma_psf: float, env_win_mult: float = 2.0,
                env_eps: float = 8.0, env_p: float = 1.0) -> np.ndarray:
    """R = 亮度归一包络（论文式 8）：E = (I − sky) / (local_max(I − sky, 窗 ≈ 4σ_psf) + εσ)。

    R 在约 4σ_psf 内自 1 降至 0，须同时满足：
        · 无亮度平台：过渡落在 2~4σ_psf 内时平台降幅回到基线，落到 5σ_psf 之外则出现平台；
        · 无噪声环：ν 须在 r_bright 之前回到 1（r_bright ≈ 4.6σ_psf / 4.1σ_psf）；
        · 保留锐化：R 在核心须确实达到 1。

    E 的性质（论文式 9、式 10）：对高斯星点，窗内极大即峰值 I0，于是
        E(r) = I(r)/I0 = exp(−r²/2σ²)（r 小于半个窗时）；
        更一般地 E(r) = I(r)/I(max(0, r − 2σ)) = exp(−2r/σ + 2)。
      该式只含 r/σ_psf，与峰值亮度无关，故不同亮度的星点得到同一条径向曲线，
        换目标无需重新标定。
      数值上：核心 1.0、2σ_psf 处 0.135、3σ_psf 处 0.011，过渡于 3σ_psf 内完成。
      空白天区：I − sky ≈ 噪声，local_max ≈ 3~4σ 的噪声极大，故 E ≈ 0.2~0.3，
        再经 εσ 项压缩至接近 0，朦胧区即满权按信噪比加权。
      平滑亮星云：local_max ≈ 自身，故 E ≈ 1，该区按全局清晰度加权。

    参数均无量纲：env_win_mult（窗 = 该倍数 × 2 × σ_psf）、env_eps（天空抑制量，以 σ 为
    单位）、env_p（形状幂 η）。
    """
    d = sref.astype(np.float64)
    sky = float(np.median(d))
    sig = 1.4826 * float(np.median(np.abs(d - sky)))
    dm = np.maximum(d - sky, 0.0)
    half = int(max(1, round(float(env_win_mult) * max(float(sigma_psf), 0.5))))
    win = 2 * half + 1
    loc = ndimage.maximum_filter(dm, size=win, mode='nearest')
    E = dm / (loc + float(env_eps) * max(sig, 1e-30))
    E = np.clip(E, 0.0, 1.0)
    if env_p and abs(float(env_p) - 1.0) > 1e-9:
        E = np.power(E, float(env_p))
    return E


def build_r_field(sref: np.ndarray, sigma_psf: float,
                  win_mult: float = R_WIN_MULT, r_floor: float = R_FLOOR,
                  env_win_mult: float = ENV_WIN_MULT, env_eps: float = ENV_EPS,
                  env_p: float = ENV_P) -> Tuple[np.ndarray, Dict]:
    """分区权重 R ∈ [0,1]：该像素属"细节型"或"朦胧型"。

    R 由亮度归一包络（_r_envelope）给出，其径向轮廓只含 r/σ_psf，换目标无需重新标定。
    另有边缘强度 T = 局部 RMS(|∇Sref|) / 其全图中位，仅用于报表诊断（info 中的 T_p50…），
    不参与 R 的计算。T 除以其全图中位后为无量纲量（天空占多数像素，故中位即天空水平，
    T_sky ≡ 1），与目标的绝对亮度、曝光、增益及窄带/宽带无关。
    """
    win = int(max(3, round(float(sigma_psf) * win_mult)))
    win |= 1                                        # 奇数窗，中心对齐
    gy, gx = np.gradient(sref.astype(np.float64))
    g2 = gx * gx + gy * gy
    rms = np.sqrt(np.maximum(
        ndimage.uniform_filter(g2, size=win, mode='nearest'), 0.0))
    sig_hp = float(np.median(rms))
    T = rms / max(sig_hp, 1e-30)

    # 包络映射不经 T：直接采用亮度归一包络，径向轮廓只含 r/σ_psf
    R = _r_envelope(sref, sigma_psf, env_win_mult, env_eps, env_p)

    # 全局下限：R_eff = floor + (1 − floor)·R，作用见 R_FLOOR 常量。
    if r_floor and r_floor > 0:
        f = min(float(r_floor), 0.95)
        R = f + (1.0 - f) * R
    R = R.astype(np.float32)
    info = {
        'sigma_psf': float(sigma_psf), 'win': int(win), 'sig_hp': float(sig_hp),
        'T_p50': float(np.median(T)), 'T_p90': float(np.percentile(T, 90)),
        'T_p99': float(np.percentile(T, 99)), 'T_max': float(T.max()),
        'R_mean': float(R.mean()),
        'R_gt50': float((R > 0.5).mean()), 'R_gt90': float((R > 0.9).mean()),
    }
    return R, info


# ---------------------------------------------------------------------------
# 阶段 C：融合权重 + 排异 + 唯一一次加权平均
# ---------------------------------------------------------------------------

def strip_weights(C: np.ndarray, S: np.ndarray, R: np.ndarray,
                  A: float, B: float) -> np.ndarray:
    """W[i,r] = C_i^(A·R_r) · S_i^(B·(1−R_r))（论文式 6），并逐像素归一使 mean_i(W) = 1。

    融合式取该形式的依据（论文 3.3 节）：
        · 幂律乘法：两端行为明确——R = 1 时仅由清晰度决定，R = 0 时仅由信噪比决定；
        · 指数上线性插值（A·R 与 B·(1−R)）：R 连续变化时 W 连续变化，无台阶与拼接痕迹；
        · B = 2 由逆方差最优性给出：加权平均方差最小 ⇔ W ∝ 1/σ² ⇔ S²（S = σ_中位/σ_i）；
        · A 为唯一需经验标定的参数，决定细节区以多少信噪比换取解析力。
    逐像素归一 mean_i(W) = 1 保证加权平均不改变总曝光量（成品背景水平与等权叠加一致），
              并使权重仅有相对意义、与帧数无关。
    按条带调用，不实体化全幅 (n, H, W) 权重场：166 帧全幅时为 166 × 244 MB ≈ 40 GiB，
              条带内计算完毕即用掉。
    """
    n = len(C)
    a = (A * R)[None, ...]
    b = (B * (1.0 - R))[None, ...]
    lc = np.log(np.maximum(C, 1e-12)).reshape(n, *([1] * R.ndim))
    ls = np.log(np.maximum(S, 1e-12)).reshape(n, *([1] * R.ndim))
    W = np.exp(a * lc + b * ls)
    W /= np.maximum(W.mean(axis=0), 1e-30)[None]
    return W.astype(np.float32)


def stack_strip(D: np.ndarray, W: np.ndarray, R: Optional[np.ndarray] = None,
                sky: float = 0.0, rej_k: float = REJ_K,
                rej_max_frac: float = REJ_MAX_FRAC
                ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """排异与唯一一次加权平均（论文式 11、式 12）：S = Σ W·D / Σ W。

    豁免余量逐条带自数据实测真实分歧的上界（论文式 11 的 m）：
        m = median_i  P99_{R>0.5 像素}( |D_i − anchor| / (anchor − sky) )
    即每帧在细节像素上"偏差/信号"的 P99（伪迹只占极少数像素，不影响 P99），再跨帧取
    中位数（个别帧上的伪迹与坏像素不影响中位数）。m 即该条带实测的真实分歧上界，
    含视场梯度、混星、PSF 形状误差等真实效应，随数据自动更新。
        阈值(r) = κ·1.4826·MAD(跨帧|偏差|) + m·R_r·(anchor − sky)
        · 天空与朦胧区 R ≈ 0：退化为纯 κσ clipping（κ 以 σ 为单位，MAD·1.4826 为 σ 估计）；
          中位数锚不受少数污染帧干扰。
        · 细节区：偏差 ≤ m·信号 者为真实分歧（按构造豁免，星点无损伤）；
          超出者为伪迹（偏差/信号 ≫ m）→ 被剔除，星核上的伪迹亦可剔除。
        · 少数派闸门：超额被剔数（扣除噪声期望误排 n·erfc(κ/√2)）> rej_max_frac × n
          → 该像素不排异（多数帧共有结构、配准黑边）；期望修正使闸门与 κ 解耦。
    R 未传入时退化为单段 κ·MAD。
      排异掩膜仅依赖各帧数值，不依赖权重。
      被剔除的 (帧, 像素) 权重置 0 后归一，仍为同一次加权平均。
    """
    n = D.shape[0]
    if rej_k > 0:
        anchor = np.median(D, axis=0)
        dev = np.abs(D - anchor[None])
        mad = 1.4826 * np.median(dev, axis=0).astype(np.float32)
        if R is None:
            thr = rej_k * mad
        else:
            sig = np.maximum(anchor - sky, 0.0)
            core = (R > 0.5) & (sig > 0)
            if bool(core.any()):
                # 实测真实分歧上界（论文式 11 的 m）：每帧细节像素 |偏差|/信号 的 P99，再跨帧取中位数
                ratio = np.percentile(dev[:, core] / sig[core][None], 99, axis=1)
                m = float(np.median(ratio))
            else:
                m = 0.0
            thr = rej_k * mad + m * (R * sig)
        bad = dev > thr[None]
        nbad = bad.sum(axis=0)
        expected = n * math.erfc(rej_k / 2.0 ** 0.5)   # 噪声期望误排数（κ 以 σ 为单位）
        bad &= (nbad - expected <= max(1.0, rej_max_frac * n))[None]     # 少数派闸门
        rej_rate = float(bad.mean())
        W = np.where(bad, np.float32(0.0), W)
    else:
        rej_rate = 0.0
    sw = W.sum(axis=0, dtype=np.float32)
    safe = np.where(sw <= 0, np.float32(1e-30), sw)
    S = (W * D).sum(axis=0, dtype=np.float32) / safe
    sw2 = (W.astype(np.float64) ** 2).sum(axis=0)
    neff = np.where(sw > 0, (sw.astype(np.float64) ** 2) / np.maximum(sw2, 1e-30), 0.0)
    return S.astype(np.float32), neff.astype(np.float32), np.float32(rej_rate)


def _avail_bytes() -> int:
    """当前可用物理内存（取不到时按 8 GiB 保守估计）"""
    try:
        import psutil
        return int(psutil.virtual_memory().available)
    except Exception:
        return 8 * 2 ** 30


def plan_strip(n_frames: int, width: int, frames_bytes: int,
               workers: int = 1, logf=log) -> int:
    """按"扣除已驻留帧之后的可用内存"反推条带高度。

    一条带内的临时量：W、D、dev、|dev| 各 (n, strip, w) float32/float64，
               外加 bad(bool) 与若干同尺寸中间量 → 按每元素 24 字节 × n 估计（取上界）。
               帧数据本身已在内存（frames_bytes），须先从可用量中扣除，否则 166 帧全幅
               会令条带过大而内存耗尽。
    workers：并行时每个工作进程各持**一条**条带，故预算按进程数均分，
               否则 N 条带同时在算即 N 倍内存。
    """
    avail = _avail_bytes()
    budget = max(1 * 2 ** 30, int(avail - frames_bytes) // 2)
    per_row = max(1.0, n_frames * width * 24.0)
    rows = int(budget // max(1, workers) // per_row)
    strip = max(1, min(rows, 512))
    logf(f'[并行] 帧已占 {frames_bytes / 2 ** 30:.2f} GiB，可用 {avail / 2 ** 30:.1f} GiB '
         f'→ 预算 {budget / 2 ** 30:.2f} GiB → {workers} 进程 × 条带 {strip} 行')
    return strip


# ---------------------------------------------------------------------------
# 并行执行：叠加按**条带**切分，测星点按**帧**切分，均为多进程
# ---------------------------------------------------------------------------
# 两处均可并行：每一块仅依赖自身输入，互不引用。
#   · 叠加   一条带只读该数行帧数据，结果写回 Stk/neff 互不重叠的行段；
#   · 测星点 第 i 帧的 FWHM 仅由第 i 帧决定。
# 并行只改变计算主体，不改变计算结果。
# 顺序亦须保持：Stk/neff 的行段、fwhms 的下标，以及排异率与最大权重
#   两个浮点累加，均按提交顺序落位/相加，故采用保序的 map。
#
# 帧数据不复制：工作进程由 initializer 各自**只读打开**同一临时帧文件，
#   真正占用内存的仅为"一条带"的临时量。故进程数由内存预算反推（见 plan_strip、
#   plan_workers）。
# 设 DWT_NO_PARALLEL=1 可强制串行（排查用）。


def _no_parallel() -> bool:
    """串行开关：**每次现读**环境变量，便于运行中途切换，故不做成常量"""
    return bool(os.environ.get('DWT_NO_PARALLEL'))


_MEM: Dict[str, object] = {}      # 工作进程内：帧后端访问，由 _worker_open* 建立


def plan_workers(per_worker_bytes: int, what: str = '', cap: int = CAP_WORKERS) -> int:
    """进程数 = min(核数 − 1, cap, 可用内存 ÷ 每进程需求)

    cap 按阶段给定（见 CAP_* 常数）：
               内存型阶段（叠加）受内存约束，cap 较小；算力型阶段（测星点，仅读取星点小窗）
               内存占用极低，cap 可放宽至核数级。
               内存不足时自动退回更少进程，退至 1 即串行。
    """
    avail = _avail_bytes()
    per = max(1, int(per_worker_bytes))
    nw = max(1, min((os.cpu_count() or 4) - 1, cap, int(avail // per)))
    if what:
        log(f'[并行] {what} {nw} 进程（可用 {avail / 2 ** 30:.1f} GiB，'
            f'每进程约 {per / 2 ** 20:.0f} MiB）')
    return nw


def _worker_open(path: str, n: int, h: int, w: int, off: np.ndarray) -> None:
    """只读打开临时帧文件（每个进程仅打开一次，之后各自切片）"""
    _MEM['mm'] = np.memmap(path, dtype=np.float32, mode='r', shape=(n, h, w))
    _MEM['off'] = off


def _task_fwhm(job) -> Tuple[int, float]:
    """一帧的星点 FWHM 中位（帧号一并返回，父进程按下标落位）

    自帧后端仅读取星点小窗，不物化整帧——单帧 244 MiB，而小窗合计约 0.5 MiB，
            故该步开销可忽略。
    """
    i, stars = job
    return int(i), measure_fwhm_frame(_MEM['mm'], i, _MEM['off'], stars)


def _task_strip(job):
    """一条带的权重、排异与加权平均（三段调用同序）"""
    y0, y1, Rr, C, S, A, B, rej_k, sky0, rej_gate = job
    W = strip_weights(C, S, Rr, A, B)
    mm, off = _MEM['mm'], _MEM['off']
    D = np.stack([mm[i, y0:y1] - np.float32(off[i]) for i in range(len(C))])
    s, ne, rr = stack_strip(D, W, Rr, sky0, rej_k=rej_k, rej_max_frac=rej_gate)
    return s, ne, rr, float(W.max(axis=0).mean())


def _worker_open_rw(path: str, n: int, h: int, w: int) -> None:
    """读帧阶段的工作进程：以 r+ 打开临时帧文件

    此处的进程须**写入**帧数据。各进程映射同一文件，映射共享同一份页缓存，
            故互不重叠地各写各的帧号是安全的；
            但**不在工作进程内 flush**——刷盘与关闭统一由父进程在全部读完后执行。
    """
    _MEM['mmw'] = np.memmap(path, dtype=np.float32, mode='r+', shape=(n, h, w))


def _task_read_frame(job):
    """读一帧 → 裁剪 → float32 → 曝光归一 → 帧级天空中位与噪声尺度

    结果写入自身对应的槽位，标量回传由父进程按下标落位。
    """
    i, fname, exptime_ref, y0, x0, h, w = job
    with contextlib.redirect_stdout(io.StringIO()):
        data, meta = read_frame(Path(fname))
    d = np.ascontiguousarray(data[y0:y0 + h, x0:x0 + w]).astype(np.float32)
    del data
    d *= np.float32(exptime_ref / float(meta['exptime']))
    med = float(np.median(d))
    sig = 1.4826 * float(np.median(np.abs(d - med)))
    _MEM['mmw'][i] = d
    del d
    return int(i), med, sig, Path(fname).name, float(meta['exptime'])


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def save_linear_xisf(path: Path, arr: np.ndarray, metas: List[Dict], mode_text: str) -> None:
    """导出线性态成品：全程只有线性运算（曝光归一常数、天空加性平移、
    Σ W·D/Σ W 加权平均），不做任何拉伸/归一/截断，导出前仅 float32 连续化。
    """
    from xisf_io import XISFImage, write_xisf
    a = np.ascontiguousarray(arr, dtype=np.float32)
    img = XISFImage()
    img.data = a
    img.geometry = (a.shape[1], a.shape[0], 1)      # XISF geometry = 宽:高:通道
    img.sample_format = 'Float32'
    img.color_space = 'Gray'
    img.bounds = (0.0, 1.0)
    img.pixel_storage = 'Planar'
    img.fits_keywords = [
        {'name': 'TOTALEXP', 'value': round(sum(m['exptime'] for m in metas), 1),
         'comment': 'total exposure seconds of combined frames'},
        {'name': 'NCOMBINE', 'value': len(metas), 'comment': 'frames combined'},
        {'name': 'STACKMODE', 'value': mode_text,
         'comment': 'partition-weighted stacking (linear output, no stretch)'},
    ]
    with contextlib.redirect_stdout(io.StringIO()):
        write_xisf(str(path), img, compression=None)
    log(f'[输出] 线性态 XISF {path.name}  {a.shape[1]}×{a.shape[0]}  '
        f'值域 {float(np.nanmin(a)):.3e}~{float(np.nanmax(a)):.3e}')


def run(p: PwsParams, on_log=None, on_progress=None, should_cancel=None) -> Dict:
    """PWS 引擎的对外唯一入口（界面与命令行均调用此函数）

    三个钩子均可选：不传即纯命令行行为（日志输出至 stdout、无进度、不可终止）。
        运行结束后自动解绑，避免界面回调被后续调用意外触发。
    """
    set_hooks(on_log, on_progress, should_cancel)
    try:
        return _run(p)
    finally:
        set_hooks()


def _run(p: PwsParams) -> Dict:
    """引擎本体：PWS 的完整数值流程（读帧 → C → R → 叠加 → 验收 → 导出）"""
    photos = Path(p.photos)
    if not photos.is_dir():
        raise SystemExit(f'素材目录不存在：{photos}')
    if not (p.out or '').strip():
        raise SystemExit('请选择输出目录（不放在素材目录里，避免成品被当成帧）')
    out_dir = Path(p.out)
    # 参数别名：算法体统一用短名引用 PwsParams 的字段
    A, B = float(p.A), float(p.B)
    r_floor = float(p.r_floor)
    env_win_mult, env_eps, env_p = float(p.env_win_mult), float(p.env_eps), float(p.env_p)
    rej_k, n_star, tag = float(p.rej_k), int(p.n_star), (p.tag.strip() or 'stack')
    rej_gate = float(p.rej_gate)                  # 少数派闸门（推荐默认 1/3，可设）
    limit = int(p.limit) or None
    crop, keep_frames, frames_dir = int(p.crop), bool(p.keep_frames), p.frames_dir
    save_xisf = True

    out_dir.mkdir(parents=True, exist_ok=True)
    t_start = time.perf_counter()
    _emit('读帧', 0.0, '准备')

    # ---- 阶段 0：读帧 + 帧级归一（自动选驻留 / memmap）----
    center = resolve_center(photos, crop, parse_center(p.center))
    fs = prepare_frames(photos, crop, center, limit, frames_dir, keep_frames=keep_frames)
    n = len(fs)
    metas = fs.metas
    skies = fs.skies

    # S_i = σ_中位 / σ_i（归一化后测 → 曝光差异已被吸收，可直接跨帧比）
    sig = fs.sigma
    S = np.median(sig) / np.maximum(sig, 1e-30)
    log(f'[S] 单帧信噪比权重（σ_中位/σ_i）{S.min():.3f}~{S.max():.3f}  中位 {np.median(S):.3f}')
    log(f'[σ] 逐帧噪声 {sig.min():.4e}~{sig.max():.4e}（极差 {sig.max() / sig.min():.2f}×）')

    # ---- 阶段 A：C_i（星点 FWHM，fit_fwhm）----
    # 先用最干净的一帧建固定星表（星表位置对全部帧共用）
    seed = int(np.argmax(S))
    t1 = time.perf_counter()
    _emit('星表', 0.1, '检测星点')
    stars = build_star_table(fs.frame(seed), n_star=n_star)
    log(f'[星表] 帧 #{seed}（S 最高）上 DAOStarFinder 选出 {len(stars)} 颗'
        f'（未饱和、孤立、最亮）  {time.perf_counter() - t1:.1f}s')
    if len(stars) < 5:
        raise SystemExit('星表不足 5 颗，无法测 C_i')
    _emit('星表', 1.0, f'{len(stars)} 颗')

    # 逐帧相互独立、互不引用 → 可分派给多进程；结果按下标落位。
    #   仅读取星点小窗 → 每进程内存占用极低，进程数上限可放宽（CAP_FWHM）
    fwhms = np.empty(n, dtype=np.float64)
    ini, ini_args = fs.reader
    nw = 0 if (not fs.parallelable or _no_parallel()) \
        else min(plan_workers(32 * 2 ** 20, '测星点', cap=CAP_FWHM), n)
    if nw > 1:
        ex = ProcessPoolExecutor(max_workers=nw, initializer=ini, initargs=ini_args)
        try:
            for i, v in ex.map(_task_fwhm, ((i, stars) for i in range(n)), chunksize=1):
                _check_cancel()
                fwhms[i] = v
                _emit('测星点', (i + 1) / max(n, 1), f'{i + 1}/{n} 帧')
        finally:
            ex.shutdown(wait=True, cancel_futures=True)
    else:
        for i in range(n):
            _check_cancel()
            fwhms[i] = fs.fwhm(i, stars)
            _emit('测星点', (i + 1) / max(n, 1), f'{i + 1}/{n} 帧')
    ok = np.isfinite(fwhms)
    fmed = float(np.median(fwhms[ok]))
    C = np.where(ok, fmed / np.maximum(fwhms, 1e-9), 1.0)
    log(f'[C] 逐帧星点 FWHM（fit_fwhm，{len(stars)} 颗中位）'
        f' {np.nanmin(fwhms):.2f}~{np.nanmax(fwhms):.2f}px  中位 {fmed:.2f}px  '
        f'{time.perf_counter() - t1:.1f}s')
    log(f'[C] 清晰度权重（FWHM_中位/FWHM_i）{C.min():.3f}~{C.max():.3f}')
    order = np.argsort(fwhms)
    log('[C] 最锐利 3 帧 ' + '  '.join(
        f'#{int(i)}({fwhms[int(i)]:.2f}px)' for i in order[:3]))
    log('[C] 最模糊 3 帧 ' + '  '.join(
        f'#{int(i)}({fwhms[int(i)]:.2f}px)' for i in order[-3:]))

    sigma_psf = fmed / 2.3548

    # ---- 阶段 B：R 场 ----
    _emit('R 场', 0.0, '建参考像')
    k = max(1, min(REF_SHARP_N, n))
    idx = np.argsort(np.nan_to_num(fwhms, nan=1e9))[:k]
    sref = np.median(np.stack([fs.frame(int(i)) for i in idx], axis=0),
                     axis=0).astype(np.float32)
    R, rinfo = build_r_field(sref, sigma_psf, r_floor=r_floor,
                             env_win_mult=env_win_mult,
                             env_eps=env_eps, env_p=env_p)
    log(f'[R] 参考像 = 最锐 {k} 帧等权中位  映射=包络(亮度归一)  '
        f'下限={r_floor:g}  σ_psf={rinfo["sigma_psf"]:.2f}px  '
        f'窗={rinfo["win"]}px  T 尺度={rinfo["sig_hp"]:.4e}')
    log(f'[R] 对比度 T 中位 {rinfo["T_p50"]:.2f}  P90 {rinfo["T_p90"]:.2f}  '
        f'P99 {rinfo["T_p99"]:.2f}  最大 {rinfo["T_max"]:.1f}')
    log(f'[R] 分区权重 R 均值 {rinfo["R_mean"]:.3f}  '
        f'R>0.5 占 {rinfo["R_gt50"]:.2%}  R>0.9 占 {rinfo["R_gt90"]:.2%}')
    del sref
    _emit('R 场', 1.0, f'R>0.5 占 {rinfo["R_gt50"]:.2%}')

    # ---- 阶段 C：逐条带（权重 + 排异 + 唯一一次加权平均）----
    h, w = R.shape
    ini, ini_args = fs.reader
    nw = 0 if (not fs.parallelable or _no_parallel()) \
        else plan_workers(512 * 2 ** 20, '叠加', cap=CAP_WORKERS)
    strip = plan_strip(n, w, fs.resident_bytes, workers=max(1, nw), logf=log)
    Stk = np.empty((h, w), np.float32)
    neff = np.empty((h, w), np.float32)
    t2 = time.perf_counter()
    rej_num = rej_den = 0.0
    wmax = []
    n_strip = int(np.ceil(h / strip))
    _emit('叠加', 0.0, f'0/{n_strip} 条带')
    try:
        if nw > 1:
            # 每条带仅读取自身数行、写回互不重叠的行段 → 可分派给多进程；
            #   map 保序 → 行段落位与两个浮点累加均按提交顺序
            ex = ProcessPoolExecutor(max_workers=nw, initializer=ini, initargs=ini_args)
            try:
                jobs = ((y0, min(h, y0 + strip), R[y0:y0 + strip], C, S, A, B, rej_k,
                         fs.sky0, rej_gate)
                        for y0 in range(0, h, strip))
                for k, (s, ne, rr, wm) in enumerate(ex.map(_task_strip, jobs, chunksize=1)):
                    _check_cancel()
                    y0 = k * strip
                    y1 = min(h, y0 + strip)
                    Stk[y0:y1] = s
                    neff[y0:y1] = ne
                    cnt = float(s.size)
                    rej_num += float(rr) * cnt
                    rej_den += cnt
                    wmax.append(wm)
                    del s, ne
                    _emit('叠加', (k + 1) / n_strip, f'{k + 1}/{n_strip} 条带')
            finally:
                ex.shutdown(wait=True, cancel_futures=True)
        else:
            for k, y0 in enumerate(range(0, h, strip)):
                _check_cancel()
                y1 = min(h, y0 + strip)
                W = strip_weights(C, S, R[y0:y1], A, B)
                D = fs.stack_strips(y0, y1)
                s, ne, rr = stack_strip(D, W, R[y0:y1], fs.sky0, rej_k=rej_k,
                                        rej_max_frac=rej_gate)
                Stk[y0:y1] = s
                neff[y0:y1] = ne
                cnt = float(s.size)
                rej_num += float(rr) * cnt
                rej_den += cnt
                wmax.append(float(W.max(axis=0).mean()))
                del W, D, s, ne, rr
                _emit('叠加', (k + 1) / n_strip, f'{k + 1}/{n_strip} 条带')
    finally:
        # 正常完成与中途终止均须清理临时帧文件
        fs.close(keep=keep_frames)
    rej_rate = rej_num / max(rej_den, 1.0)
    log(f'[W] A={A:g} B={B:g}（B=2 为逆方差最优）  条带 {strip} 行 × '
        f'{int(np.ceil(h / strip))} 条  归一后 mean_i(W)=1')
    log(f'[W] 单帧最大权重（条带均值）{np.mean(wmax):.3f}  '
        f'等权份额 = {1.0:.3f} → 最大独占约 {np.mean(wmax):.1f}× 份额')
    log(f'[叠加] 完成 {time.perf_counter() - t2:.1f}s  排异剔除率 {rej_rate:.3%}')
    log(f'[叠加] N_eff 中位 {np.median(neff):.2f}/{n}  '
        f'P10 {np.percentile(neff, 10):.2f}  最小 {neff.min():.2f}')

    # 分区分读：细节区 vs 朦胧区的 N_eff 与噪声
    det = R > 0.5
    amb = R < 0.1
    sky_m = Stk < np.percentile(Stk, 50)
    for name, msk in (('细节区 R>0.5', det), ('朦胧区 R<0.1', amb), ('天空(下半亮度)', sky_m)):
        if msk.sum() == 0:
            continue
        sd = 1.4826 * float(np.median(np.abs(Stk[msk] - np.median(Stk[msk]))))
        log(f'[读数] {name:<18} 占 {msk.mean():6.2%}  N_eff 中位 '
            f'{np.median(neff[msk]):5.2f}  σ {sd:.4e}')

    # 成品自身的星点 FWHM（同一星表、同一口径）→ 与单帧/参照栈直接可比
    _emit('验收', 0.3, '量成品星点')
    f_out = measure_fwhm(Stk, stars)
    log(f'[验收] 成品星点 FWHM {f_out:.2f}px（单帧中位 {fmed:.2f}px，'
        f'改善 {100 * (1 - f_out / fmed):.1f}%）')

    res = {
        'n': n, 'shape': Stk.shape, 'fwhms': fwhms, 'C': C, 'S': S, 'sigma': sig,
        'fwhm_med': fmed, 'fwhm_out': f_out, 'sigma_psf': sigma_psf,
        'R_info': rinfo, 'neff': neff, 'rej_rate': float(rej_rate),
        'A': A, 'B': B, 'rej_k': rej_k, 'stars': stars,
        'metas': metas, 'skies': skies,
    }

    if save_xisf:
        p_out = out_dir / f'stack_{tag}.xisf'
        _emit('验收', 0.7, '导出成品')
        save_linear_xisf(p_out, Stk, metas,
                         f'PWS A={A:g} B={B:g} rej_k={rej_k:g}')
        np.save(out_dir / f'neff_{tag}.npy', neff)
        np.save(out_dir / f'R_{tag}.npy', R)
        np.savetxt(out_dir / f'weights_{tag}.txt',
                   np.column_stack([np.arange(n), fwhms, C, sig, S]),
                   header='idx fwhm_px C_sharp sigma S_snr', comments='# ', fmt='%.6g')
        log(f'[输出] 逐帧权重表 weights_{tag}.txt / R 场 R_{tag}.npy / N_eff neff_{tag}.npy')

    log(f'[时] 总计 {time.perf_counter() - t_start:.1f}s')
    _emit('验收', 1.0, '完成')
    res['stack'] = Stk
    return res


def build_parser() -> argparse.ArgumentParser:
    """命令行参数表由 pws_params.PARAMS 生成"""
    ap = argparse.ArgumentParser(
        prog='DWT',
        description='DWT - Partition-Weighted Stacking（PWS 分区加权叠加）')
    base = PwsParams()
    for prm in PARAMS:
        flag = cli_flag(prm.key)
        # argparse 会对 help 做一次 % 插值，说明里的"2~4%"必须写成"2~4%%"
        text = f'{prm.desc}。{prm.usage}'.replace('%', '%%')
        default = getattr(base, prm.key)
        if prm.kind == 'check':
            ap.add_argument(flag, action='store_true', default=default, help=text)
        elif prm.kind == 'float':
            ap.add_argument(flag, type=float, default=default, help=text)
        elif prm.kind == 'int':
            ap.add_argument(flag, type=int, default=default, help=text)
        elif prm.kind == 'choice':
            ap.add_argument(flag, default=default,
                            choices=[v for _, v in prm.choices], help=text)
        else:
            ap.add_argument(flag, default=default, help=text)
    return ap


def main(argv=None):
    """命令行入口：批处理"""
    a = build_parser().parse_args(argv)
    p = PwsParams(**{prm.key: getattr(a, prm.key) for prm in PARAMS})
    try:
        run(p)
    except Cancelled:
        log('已终止')
    return 0


if __name__ == '__main__':
    main()
