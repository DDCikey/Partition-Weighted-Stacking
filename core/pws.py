"""DWT：Partition-Weighted Stacking（PWS，分区加权叠加）引擎

本文件是 **DWT 软件的引擎**，由 test_tools/DWT_stack_v2.py 原样抽取而来。
    相对 v2 只多三样东西，**算法数值一律未动**：日志回调、进度回调、取消检查。
    参数从 core/pws_params.py 的 PwsParams 读入 —— 界面、命令行、引擎共用那一份定义。
输入前提：已校准（暗/平/偏）、已对齐（旋转+平移）的帧；本软件不做校准与对齐。
输出：线性 Float32 灰度（全程只有线性运算：曝光归一常数、天空加性平移、
    加权平均），不做任何拉伸 / 归一 / 截断。

用户定稿的方案（见对话整理稿）：三类权重、一个融合式。

    W[i,r] = C_i^(A·R_r) · S_i^(B·(1−R_r))         再逐像素归一 mean_i(W) = 1

    C_i  单帧清晰度权重（帧间标量）  = FWHM_中位 / FWHM_i        越大越清晰
    S_i  单帧信噪比权重（帧间标量）  = σ_中位 / σ_i               越大越干净
    R_r  分区权重（逐像素连续场）    ∈ [0,1]   1=细节型  0=朦胧型

与 v1（DWT_stack_proto.py，44 个 CLI 参数）的区别——**删掉的不是功能，是补丁**：

    v1 的 q 是"逐帧 × 逐像素"的场，为了让这个场不出事，先后加了：判据带多档 + 自动选带、
    净化（去星）、可测性闸门 gate、星足迹中性化 t_min/羽化、权重上限 w_cap、
    等效帧数下限 neff_floor、星点层 star_layer/star_cap/star_kappa …… 每个都是为治一个症状。
    它们互相打架的实测后果：6888 星核区 80.3% 像素被闸门判"测不到"→ q≡1 → 等权，
    糊帧在星核满权重，成品星点 FWHM 7.13px，比"不选帧的全叠加"6.59px 还粗
    （单帧 8.19px，而硬删帧的细帧叠加 5.65px）。

    v2 把 C、S 降为**帧级标量**（复杂度少一个维度），R 是唯一的空间场。于是：
      · 闸门不需要了  —— R→0 时 W→S^B，天然趋等权，空白天区自动吃满信噪比
      · 星足迹中性化不需要了 —— R 在星周连续衰减，星核 R≈1、星周 R 平滑下降
      · 净化不需要了  —— R 用"边缘强度 / 自身噪声尺度"定义，星点天然是最强的细节型
      · 判据带不需要了 —— R 的尺度直接取实测 PSF（σ_psf = FWHM/2.3548），自动跟随目标
      · w_cap/neff_floor 不需要了 —— 分级强度由唯一的 A 决定，靠标定而非限幅
    再把"实测未通过、只作对照"的旧映射与旧判据口径彻底删掉（R 只剩包络一种），
      见下方常数区注释。留着就是死代码，下次改算法时会误导。

    B 不用标定：加权平均方差最小的解就是逆方差加权 W ∝ 1/σ²，即 B = 2。
    跨目标自适应：σ_i、T 的分母、σ_psf、包络窗全部来自图像自身统计量，
    换目标不改任何数值、也不改含义。

开源依赖（均为可商用许可，非自造轮子）：
    photutils.detection.DAOStarFinder   星点检测（DAOFIND 标准实现，BSD-3）
    photutils.psf.fit_fwhm              高斯 PSF 拟合 FWHM（CircularGaussianPRF，BSD-3）
    scipy.ndimage                       高斯/均值滤波
"""
from __future__ import annotations

import argparse
import contextlib
import io
import math
import os
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy import ndimage

# DWT/core/pws.py → 仓库根是上两级；xisf_io.py / DWT_DetailCore.py 都在仓库根，
#   pws_params.py 与 pws.py 同目录（作为顶层模块导入，避免依赖包结构）
_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parent.parent
for _p in (str(ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from DWT_DetailCore import EXPTIME_REF, read_frame                      # noqa: E402
from pws_params import PARAMS, PwsParams, cli_flag                      # noqa: E402

# ---------------------------------------------------------------------------
# 全部可调参数就这些（v1 是 44 个）
# ---------------------------------------------------------------------------
A_SHARP = 20.0       # 细节区清晰度幂上限：R=1 处 W = C^A
                     # 四轮标定（6888/24帧 + 7331/166帧，_tmp_v2_verify_wp.py）：
                     #   现工作点（亮度归一包络映射），A=20：
                     #     6888 FWHM 5.51（无分区 7.12）  平台降幅 24.3%（基线 23.3%）
                     #     7331 FWHM 3.99（无分区 5.12）  平台降幅 20.3%（基线 21.3%）
                     #     ν@r_bright 1.03/1.00、ν 回 1 半径 5.5/3.5px（r_bright 14.2/9.2）
                     #     shelf 0.269/0.724 —— 比无分区基线的 0.311/0.768 还低
                     #   A=12 更保守（FWHM 5.68/4.13，天空 σ 只涨 3.8%/2.3%）；
                     #   A=30 更锐（5.36/3.88）但天空 σ 涨到 6.9%/6.3%、N_eff细节 掉到 3.93
B_SNR = 2.0          # 朦胧区信噪比幂上限：R=0 处 W = S^B。**B=2 = 逆方差最优，不标定**
# R 的映射**只有一种**：亮度归一包络（见 _r_envelope）。它的径向轮廓只含
#   r/σ_psf，与目标亮度无关 → 过渡恒在 ~2σ_psf 内，换目标不改参数也不改含义。
# 曾用"对比度绝对阈值"的另外几种映射（余弦距离衰减 / 对数斜坡 / S 型）
#   与另一种判据口径（带通 RMS / 空间裙边）实测均未通过，已连同其参数一并删除。
ENV_WIN_MULT = 2.0   # 包络归一的局部极大窗 = 该倍数×2×σ_psf（≈4σ_psf 见方）
ENV_EPS = 8.0        # 分母加的噪声项 = 该倍数×σ（压住空白天区，使 R→0）
                     # 标定：eps=3 时 R>0.5 占 3.13%/3.75%（6888/7331），天空 σ 涨
                     #   5.5%/4.1%；eps=8 收到 1.20%/1.71%，天空 σ 只涨 3.2%/1.8%，
                     #   而 FWHM 与平台降幅几乎不变（5.49→5.51、3.98→3.99；24.3%、20.3%）→ 取 8
ENV_P = 1.0          # 形状幂。E 已是 exp(−2r/σ) 量级，1.0 即够；>1 收得更紧
R_FLOOR = 0.0        # R 的全局下限：R_eff = R_FLOOR + (1−R_FLOOR)·R
                     # **这是用户报的"外圈亮环"的直接对策**。用户机理判断（已证实）：
                     #   "外圈亮环来自于其他粗星点单帧，星点保护范围只在星点周围…
                     #    外部非保护部分在其他帧依然能被叠加进来"。
                     #   量化：外环幅度 = 外围"全帧平均 PSF"与核内"锐帧加权 PSF"的翼部差。
                     #   7331 的 C 下限 0.522（最糊帧 FWHM 是最锐帧 2.7 倍）、166 帧里糊帧占多数
                     #   → 外围平均 PSF 翼部很肥 → 环明显；6888 的 C 下限 0.728、24 帧 → 环较轻。
                     #   **实测：把 R 的径向过渡从 6px 放宽到 13px，环几乎不变**
                     #   （暗环 −67.8%→−67.9%、FWHM 5.53→5.55）→ 摊宽过渡不是解法，
                     #   环的幅度只取决于两区的有效 PSF 差。故直接攻差值：
                     #   给 R 加全局下限 → 空白天区也保留一部分清晰度偏好 → 糊帧在全图被压
                     #   → 两区 PSF 差缩小 → 环变浅。代价是空白天区 N_eff 下降（信噪比）。
                     #   0 = 关闭（朦胧区严格满权重吃满信噪比）
R_WIN_MULT = 2.0     # 结构 RMS 的空间窗 = 该倍数 × σ_psf（4.0 会把点源能量摊薄）
REJ_K = 3.5          # 逐像素排异：|偏差| > κ×1.4826×MAD + m·R·信号 即剔（用户：排异是必然的）。
                     #   κ 以 σ 为单位（MAD·1.4826=σ 估计）；2 更严、1 会成批排清洁帧噪声尾
REJ_MAX_FRAC = 1 / 3  # 少数派闸门：超额被剔帧数 > 该比例 × 总帧数 → 判"不是孤立离群"，该像素不排异
N_STAR = 120         # 固定星表星数（测 C_i 用；中位数在 100 颗以上已稳定）
REF_SHARP_N = 8      # 参考像 = 最锐的这么多帧的等权中位（稳健且不必读两遍）

# 各阶段的并行进程上限（真正的进程数还受"核数−1"与内存预算约束，见 plan_workers）
#   叠加：每进程持一条条的临时量，内存按倍数涨 → 上限保守，防 OOM
#   读帧：受磁盘带宽约束，多开无益
#   测星点：只取星点小窗（每帧 ~0.5 MiB），内存几乎不占 → 放开到核数级别
CAP_WORKERS = 8
CAP_FWHM = 12


# ---------------------------------------------------------------------------
# 与界面之间的三个钩子（引擎本体不依赖任何界面代码，命令行下自动退回 stdout）
# ---------------------------------------------------------------------------

class Cancelled(Exception):
    """用户请求终止：在检查点抛出，由调用方清理并提示"""


_LOG_SINK: List = [None]        # callable(str)            → 界面 append 日志
_PROGRESS: List = [None]        # callable(阶段, 百分比, 说明) → 界面进度
_CANCEL: List = [None]          # callable() -> bool       → 取消探针

# 阶段 → 全局百分比区间（单调递增，界面只显示换算后的这一个数）
_PHASE_SPAN = (
    ('读帧', 0.00, 0.25),
    ('星表', 0.25, 0.30),
    ('测星点', 0.30, 0.45),
    ('R 场', 0.45, 0.52),
    ('叠加', 0.52, 0.95),
    ('验收', 0.95, 1.00),
)
_SPAN = {name: (lo, hi) for name, lo, hi in _PHASE_SPAN}

# 20261006 多滤镜分组：_GROUP = [组号, 总组数, 日志前缀标签]。
#   单组时 = [0, 1, '']——_emit 不缩放、log 不加前缀，行为与旧版逐位一致。
_GROUP: List = [0, 1, '']


def _set_group(gi: int, gn: int, label: str = '') -> None:
    """进入某滤镜组：进度换算缩到该组的全局区间，日志行加 [滤镜] 前缀"""
    _GROUP[0], _GROUP[1] = gi, max(1, gn)
    _GROUP[2] = f'[{label}] ' if label else ''


def log(msg: str = '') -> None:
    """引擎唯一输出口：界面接管后写成日志行，未接管则打 stdout（回调异常也退回）。
    多滤镜分组时自动带组前缀（空行不加）——否则几十上百行日志分不清是哪组滤镜的。"""
    pre = _GROUP[2]
    if pre and msg.strip():
        msg = f'{pre}{msg}'
    sink = _LOG_SINK[0]
    if sink is None:
        print(msg, flush=True)
        return
    try:
        sink(msg)
    except Exception:
        print(msg, flush=True)


def _emit(phase: str, frac: float, text: str = '') -> None:
    """报进度：frac 是**本阶段内**完成度，此处换算成全局百分比后交给界面。
    多滤镜分组时再缩进当前组的全局区间：(组号 + 组内值) / 总组数。"""
    fn = _PROGRESS[0]
    if fn is None:
        return
    lo, hi = _SPAN.get(phase, (0.0, 1.0))
    v = lo + (hi - lo) * min(1.0, max(0.0, float(frac)))
    gi, gn = _GROUP[0], _GROUP[1]
    if gn > 1:
        v = (gi + v) / gn
    try:
        fn(phase, v, text)
    except Exception:
        pass


def _check_cancel() -> None:
    """取消检查点：读帧每帧、测 C_i 每帧、叠加每条带各一处"""
    fn = _CANCEL[0]
    if fn is not None and fn():
        raise Cancelled('用户终止')


def set_hooks(on_log=None, on_progress=None, should_cancel=None) -> None:
    """绑定/解绑三个钩子（传 None 即解绑）"""
    _LOG_SINK[0] = on_log
    _PROGRESS[0] = on_progress
    _CANCEL[0] = should_cancel


def parse_center(text: str) -> Optional[Tuple[int, int]]:
    """解析裁剪中心 'y,x'（界面里就是一行文本）；留空返回 None → 自动定位延展源"""
    t = (text or '').strip()
    if not t:
        return None
    parts = [v for v in t.replace('，', ',').split(',') if v.strip()]
    if len(parts) < 2:
        raise SystemExit(f'裁剪中心要写成 y,x 两个整数（收到 {text!r}）')
    return (int(float(parts[0])), int(float(parts[1])))


# ---------------------------------------------------------------------------
# 阶段 0：帧加载与帧级归一
# ---------------------------------------------------------------------------

FRAME_SUFFIXES = ('.xisf', '.fit', '.fits', '.fts')


def collect_frames(photos_str: str) -> Tuple[List[Path], List[Path]]:
    """把素材声明展开成帧文件列表（多源：目录与单张可混用，用 ; 分隔）

    20261006 目录改为**递归收集**：深空数据常按 目标/日期/滤镜 分级存放，
    子文件夹里的帧也要收。XISF / FITS 混合树的取舍按**子目录**判断——
    与旧版单目录语义一致（同一目录里两种格式并存视为同一批数据的两种导出，
    只收 XISF，避免同帧重复叠加），不同子目录各用各的格式、互不影响。
    单张文件按扩展名直接收录。去重按规范路径（同一文件被两个来源重复声明
    只算一帧），最终按路径全名排序保证读取顺序确定。返回 (帧列表, 来源列表)。
    """
    sources: List[Path] = []
    seen_src = set()
    for raw in (photos_str or '').split(';'):
        raw = raw.strip().strip('"')
        if not raw:
            continue
        p = Path(raw)
        key = str(p.resolve()).lower()
        if key in seen_src:
            continue
        seen_src.add(key)
        sources.append(p)
    if not sources:
        raise SystemExit('请选择素材（目录或单张帧；多个来源用 ; 分隔）')

    files: List[Path] = []
    seen_file = set()
    for src in sources:
        if src.is_dir():
            by_dir: Dict[Path, List[Path]] = {}
            for f in src.rglob('*'):
                if f.is_file() and f.suffix.lower() in FRAME_SUFFIXES:
                    by_dir.setdefault(f.parent, []).append(f)
            batch: List[Path] = []
            for d in sorted(by_dir, key=lambda p: str(p).lower()):
                grp = sorted(by_dir[d], key=lambda f: str(f).lower())
                xisf = [f for f in grp if f.suffix.lower() == '.xisf']
                batch.extend(xisf or grp)
            if not batch:
                raise SystemExit(f'{src}（含子文件夹）里没有 XISF / FITS 帧')
        elif src.is_file():
            if src.suffix.lower() not in FRAME_SUFFIXES:
                raise SystemExit(f'不支持的帧格式：{src.name}（{src}）')
            batch = [src]
        else:
            raise SystemExit(f'素材不存在：{src}')
        for f in batch:
            key = str(f.resolve()).lower()
            if key not in seen_file:
                seen_file.add(key)
                files.append(f)
    if not files:
        raise SystemExit('素材里没有帧')
    files.sort(key=lambda f: str(f).lower())
    return files, sources


def group_frames_by_filter(files: List[Path]) -> Dict[str, List[Path]]:
    """20261006 按帧头滤镜名分组（分组叠加第一步）。

    只读文件头不解码像素（read_filter_hint：FITS 读 FILTER 卡，XISF 只读
    XML 元数据段），几百帧也就几秒。头里没有滤镜信息的帧归入 ''（单独成组
    照常叠加，日志点名）；同一滤镜名的帧合为一组。顺序 = 首次出现
    （files 已按路径排序，结果确定）。
    """
    from DWT_DetailCore import read_filter_hint
    groups: Dict[str, List[Path]] = {}
    t0 = time.perf_counter()
    for f in files:
        _check_cancel()
        name = ''
        try:
            name = read_filter_hint(f)
        except Exception:
            pass
        groups.setdefault(name, []).append(f)
    log(f'[分组] 读头 {len(files)} 帧 {time.perf_counter() - t0:.1f}s → '
        f'{len(groups)} 组：'
        + '  '.join(f'{k or "无滤镜信息"}×{len(v)}' for k, v in groups.items()))
    if '' in groups:
        log('[分组] 注意：以上帧的文件头没有滤镜信息（FILTER / Instrument:Filter:Name），'
            '已单独成组')
    return groups


def _safe_seg(s: str) -> str:
    """文件名段安全化：路径非法字符换下划线"""
    return re.sub(r'[\\/:*?"<>|]+', '_', (s or '').strip()).strip()


def _out_stem(tag: str, fname: str, multi: bool) -> str:
    """20261006 成品文件主名（用户定稿）：一律 PWS_ 开头。
    有成品名 → PWS_<成品名>，分组时再接滤镜段 → PWS_<成品名>_<滤镜>；
    无成品名 → PWS_<滤镜>（单组且无滤镜信息时退化为 PWS）。"""
    t, f = _safe_seg(tag), _safe_seg(fname)
    parts = ['PWS']
    if t:
        parts.append(t)
    if f and (multi or not t):
        parts.append(f)
    return '_'.join(parts)


def resolve_center(frames: List[Path], crop: int,
                   center: Optional[Tuple[int, int]]) -> Optional[Tuple[int, int]]:
    """裁剪中心留空时**自动定位延展源**（核心层 locate_extended_source，
    32px 分块中值法找最亮延展团）。默认值绝不能写死目标坐标：换目标时会静默用错中心，
    裁到空白天区，R 场与星表全废。全幅（crop=0）时返回 None。
    """
    if not crop or crop <= 0 or center is not None:
        return center
    from DWT_DetailCore import locate_extended_source
    with contextlib.redirect_stdout(io.StringIO()):
        d0, _ = read_frame(frames[0])
    cy, cx = locate_extended_source(np.asarray(d0, dtype=np.float32), bin_px=32)
    del d0
    log(f'[裁剪] 自动定位延展源中心 (y,x)=({cy},{cx}) → {crop}² 窗')
    return (int(cy), int(cx))


def pick_frames_dir(need_bytes: int, want: Optional[str] = None) -> Path:
    """给 memmap 帧文件挑盘：需要 1.25× 余量

    为什么必须挑盘：166 帧全幅 = 166×6388×9576×4B = 40.6 GiB，而项目所在
               K 盘只剩 35.6 GiB（实测）。直接写会中途 ENOSPC，且失败点在读完 100 多帧之后，
               浪费十几分钟。故开跑前就按余量选盘，并把选择打出来。
      优先级（取**第一个够用**的，不追求余量最大）：
        ① 用户 --frames-dir  ② 项目盘 _v2_frames（够用就不跑远路）
        ③ 系统 TEMP（临时文件的惯例位置，一般在系统盘）  ④ 其余固定盘按余量降序
    """
    import shutil
    need = int(need_bytes * 1.25)
    head: List[Path] = []
    if want:
        head.append(Path(want))
    if not getattr(sys, 'frozen', False):
        # 20261002 打包版（frozen）不再把临时帧写进安装目录（会落在 _internal 里，
        #   可能只读、且污染发布包）；仅源码运行时使用项目盘目录
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
                     f'（需要 1.25× 余量）。请用 --frames-dir 指到大盘。')


class FrameSet:
    """帧集合：**memmap 文件**（常规）与 **父进程内存 list**（无盘可落时的兜底）

    两种后端都是"一整块 (n,h,w) 的 float32 数组"，对外接口完全一致。

    为什么必须有 memmap 后端：全幅一帧 6388×9576×4B = 244 MiB，166 帧 = 40.6 GiB，
               可以把内存吃干净，而排异必须"同一像素跨帧比较"，条带内层必须是帧 →
               不能靠"帧外层循环 + 累加器"绕开随机访问。故落盘 memmap，按条带随机读。
    为什么不再把"装得进内存"的帧放进共享内存（shared_memory）：页文件支撑的共享段
               在 Windows 上要按**整段大小**记入系统提交量（本机页文件仅 26.5 GiB、
               提交上限 88.1 GiB），十几个工作进程各自映射 19.6 GiB 的段会直接
               失败在 WinError 1450/1455（系统资源不足 / 页面文件太小）。
               改成**文件支撑**的 memmap 后，脏页由文件本身兜底、不进提交量，
               而文件页待在系统缓存里，读写速度与内存一致 → 一律走这一条路。
    父进程内存 list 只是"一个盘都放不下"时的兜底：它不能跨进程（spawn 建子进程要
               把整份帧 pickle 过去），故该后端自动退化为串行。见 prepare_frames。

    天空加性平移（off_i）统一在**读出时减**，两种后端一致：
               memmap = 盘上即"曝光归一后"，off_i 读出时减。
               早先兜底后端是"建集时就地烘焙"（把 off 加进数据再置 0），
               那要多写一遍整块，而读出时减只是 D − 常数、代价为零 → 已取消烘焙。
               两种后端给出的数值逐位一致：都只做 D − 常数。
    """

    def __init__(self, n: int, shape: Tuple[int, int], resident: bool,
                 arrays: Optional[List[np.ndarray]] = None,
                 mm: Optional[np.ndarray] = None, path: Optional[Path] = None):
        self.n = int(n)
        self.shape = (int(shape[0]), int(shape[1]))
        self.resident = bool(resident)
        self.arrays = arrays       # 兜底后端（父进程内存，不可跨进程，仅串行）
        self.mm = mm               # memmap 视图（可跨进程）
        self.path = path           # memmap 的文件路径（兜底后端为 None）
        self.skies = np.zeros(n, dtype=np.float64)
        self.sigma = np.zeros(n, dtype=np.float64)
        self.off = np.zeros(n, dtype=np.float64)
        self.sky0 = 0.0            # 平移后的公共天空水平（排异信号项的零点）
        self.metas: List[Dict] = []

    def __len__(self) -> int:
        return self.n

    def frame(self, i: int) -> np.ndarray:
        """第 i 帧全幅（曝光归一 + 天空平移后）"""
        if self.mm is None:
            return self.arrays[i]
        return self.mm[i] - np.float32(self.off[i])

    def strip(self, i: int, y0: int, y1: int) -> np.ndarray:
        """第 i 帧的 [y0,y1) 行条带"""
        if self.mm is None:
            return self.arrays[i][y0:y1]
        return self.mm[i, y0:y1] - np.float32(self.off[i])

    def stack_strips(self, y0: int, y1: int) -> np.ndarray:
        """条带内所有帧堆成 (n, y1-y0, W) —— 排异与加权平均的唯一输入"""
        return np.stack([self.strip(i, y0, y1) for i in range(self.n)])

    def fwhm(self, i: int, stars: List[Tuple[int, int]]) -> float:
        """第 i 帧在固定星表上的 FWHM 中位 —— 只取星点小窗，不物化整帧"""
        if self.mm is None:
            return measure_fwhm(self.arrays[i], stars)
        return measure_fwhm_frame(self.mm, i, self.off, stars)

    @property
    def parallelable(self) -> bool:
        """能否跨进程交给工作进程（memmap 可以，兜底列表不行）"""
        return self.mm is not None

    @property
    def reader(self) -> Tuple[object, tuple]:
        """工作进程打开本帧集的 (initializer, initargs)"""
        return _worker_open, (str(self.path), self.n, self.shape[0], self.shape[1], self.off)

    @property
    def writer(self) -> Tuple[object, tuple]:
        """读帧阶段（要**写**帧数据）的 (initializer, initargs)"""
        return _worker_open_rw, (str(self.path), self.n, self.shape[0], self.shape[1])

    @property
    def resident_bytes(self) -> int:
        """帧数据占着**系统内存**的量 —— 用来给叠加阶段的条带高度算内存预算

        帧在盘上时这份内存由系统缓存兜着，但它确实与工作进程争内存，
            故照样从预算里扣（宁可条带矮一点，也不要因为缓存被挤掉而反复读盘）。
            盘都放不下、退回父进程内存 list 时，它记的是进程自己的占用。
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


def prepare_frames(frames: List[Path], label: str, crop: int,
                   center: Optional[Tuple[int, int]],
                   limit: Optional[int] = None, frames_dir: str = '',
                   mem_frac: float = 0.55, keep_frames: bool = False) -> FrameSet:
    """读帧 → 裁剪 → float32 → **曝光归一**（×EXPTIME_REF/t_i），
    同时逐帧量天空中位与噪声尺度；按内存预算自动决定驻留还是落 memmap。

    曝光归一必须在最前面：用户的定义里"曝光时间可以不同"，归一后所有帧
               等价于同一曝光时长，σ_i 才可以直接跨帧比较（S_i 的前提）。
    天空中位与 σ_i 在这一趟里顺带算掉：memmap 后端若事后再算，
               就要把 40 GiB 从盘上重读一遍。
    """
    files = list(frames)
    if limit:
        files = files[:limit]
    n = len(files)
    log(f'[帧] {n} 帧  来源 {label}')

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

    # 一律落盘 memmap（见 FrameSet 文档：页文件支撑的共享段在 Windows 上按整段大小
    #   记入提交量，装不下就 1450/1455；文件支撑的映射不进提交量，页仍在系统缓存里）。
    #   只有"一个盘都放不下"时才退回父进程内存 list —— 那会退化为串行。
    #   内存够不够只用于提示：装得进内存时盘上这份基本全程待在缓存里。
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
    # 逐帧独立（各读各的源文件、各写各的槽位）→ 可分给多进程。
    #   工作进程各自只读打开同一个帧文件，直接写自己那一格，
    #   父进程收回标量（帧号、天空中位、σ）+ 元数据字典（含 FITS 关键字与
    #   Property 原始元素，供成品透传；每帧几十 KB，pickle 代价可忽略）。
    #   每进程内存按"整帧 ×3"估：read_frame 的整帧缓冲 + float32 裁剪副本
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
                for i, med, sig, meta in ex.map(_task_read_frame, jobs, chunksize=1):
                    _check_cancel()
                    fs.skies[i] = med
                    fs.sigma[i] = sig
                    fs.metas[i] = meta
                    if (i + 1) % 20 == 0:
                        log(f'  读帧 {i + 1}/{n}  {time.perf_counter() - t0:.0f}s')
                    _emit('读帧', (i + 1) / max(n, 1), f'{i + 1}/{n} 帧')
            finally:
                ex.shutdown(wait=True, cancel_futures=True)
        else:
            for i, f in enumerate(files):
                _check_cancel()                     # 读帧阶段也要能终止
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
                fs.metas.append(meta)
                if (i + 1) % 20 == 0:
                    log(f'  读帧 {i + 1}/{n}  {time.perf_counter() - t0:.0f}s')
                _emit('读帧', (i + 1) / max(n, 1), f'{i + 1}/{n} 帧')
    except BaseException:
        # 读帧半途失败（终止 / 工作进程异常 / 盘满）都要把半成品临时帧文件清掉，
        #   否则一块几十 GiB 的文件会一直占着盘；清完再把异常往上抛
        fs.close(keep=keep_frames)
        raise

    # 天空加性平移：锚 = 天空最接近全批中位的那一帧。两种后端都只记 off_i，
    #   读出时减（见 FrameSet.strip）—— 兜底列表后端才需要就地烘焙。
    anchor = int(np.argmin(np.abs(fs.skies - np.median(fs.skies))))
    sky0 = float(fs.skies[anchor])
    fs.off = fs.skies - sky0
    fs.sky0 = sky0
    if arrays is not None:
        for i in range(n):
            arrays[i] -= np.float32(fs.off[i])
        fs.off[:] = 0.0            # 已就地烘焙，读出时不再减
    if mmpath is not None:
        mm.flush()
    span = float(fs.skies.max() - fs.skies.min())
    log(f'[帧] 耗时 {time.perf_counter() - t0:.1f}s  '
        f'{"父进程内存 " + f"{fs.resident_bytes / 2 ** 30:.2f} GiB" if mm is None else f"盘上 {need / 2 ** 30:.2f} GiB（{mmpath.parent}）"}')
    log(f'[归一] 天空 {fs.skies.min():.4e}~{fs.skies.max():.4e}  锚帧 #{anchor}={sky0:.4e}  '
        f'平移极差 {span:.3e}（天空的 {span / max(sky0, 1e-30):.2%}）')
    return fs


def frame_sigma(fs: FrameSet) -> np.ndarray:
    """逐帧噪声尺度 σ_i = 1.4826 × MAD（天空背景主导，稳健、抗星点/目标）
    已在 prepare_frames 的读帧趟里量好，这里直接取。
    """
    return fs.sigma


# ---------------------------------------------------------------------------
# 阶段 A：C_i（星点 FWHM，开源工具）
# ---------------------------------------------------------------------------

def build_star_table(ref: np.ndarray, n_star: int = N_STAR,
                     iso_mult: float = 5.0) -> List[Tuple[int, int]]:
    """固定星表：DAOStarFinder 检测 → 未饱和 → 孤立 → 取最亮 n_star 颗

    必须固定星表（v1 实测的根因）：逐帧检测时阈值 = 5×该帧噪声，糊帧检出得少，
               "帧质量"会混进"星点强度"，导致糊帧被系统性高估。固定星表后所有帧量同一批位置。
    用开源 DAOStarFinder 而不是自写峰值检测：用户明确要求
               "开源的内容能用就要用起来"，且自写检测的样本偏差已让一轮结论作废
               （曾据 21 颗手挑星判"只有 12 颗变粗"，实为样本偏差）。
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

    # 孤立判据：与已选星的最小距离 ≥ iso_mult × 拟合窗半宽，保证窗口内无混星
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
    """逐星量高斯 PSF FWHM 的**唯一实现**；kut(xi,yi) 给一颗星的小窗（越界给 None）

    小窗从哪来有两种来源（整帧切片 / 帧后端直接取），数值路径完全一样 ——
              所以"只取小窗"与"先物化整帧再切"逐位一致。
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
    """逐星量高斯 PSF FWHM，返回与 stars 等长的数组（失败/越界为 NaN）

    用 PSF 拟合而不是二阶矩：二阶矩对翼部极敏感（PI 的 FWHMEccentricity
               会把 r50 +0.2px / 翼部 +20% 放大成数 px），拟合口径才是"星点细不细"的稳定度量。
    拆出逐星版本的原因：配对比较必须逐星做（同一颗星在两个栈上的差），
               只给中位数会丢掉分布信息 —— 上一轮"只有 12 颗变粗"的误判正是栽在这里。
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
    """从帧后端（共享内存 / memmap）直接取星点小窗量 FWHM 中位 —— **不物化整帧**

    为什么值得单独写一条：一帧 244 MiB，而 120 颗星的小窗合起来才 ~0.5 MiB。
              先 mm[i] − off 物化整帧再切片，等于为了 0.5 MiB 去读写 244 MiB
              （memmap 后端就是每帧从盘上重读 244 MiB）。取小窗后这一步几乎免费。
    数值与 measure_fwhm(mm[i] − off, stars) 逐位一致：
              两种写法都是"先做一次 float32 减常数、再取同一片"，
              而逐元素减法与切片顺序无关。
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
    """固定星表上的 FWHM 中位（成品验收用这一个数）"""
    v = measure_fwhm_each(img, stars, fwhm_guess, half, fit_shape)
    v = v[np.isfinite(v)]
    if len(v) < 5:
        return float('nan')
    return float(np.median(v))


# ---------------------------------------------------------------------------
# 阶段 B：R_r（分区权重，逐像素连续场）
# ---------------------------------------------------------------------------

def _r_envelope(sref: np.ndarray, sigma_psf: float, env_win_mult: float = 2.0,
                env_eps: float = 3.0, env_p: float = 1.0) -> np.ndarray:
    """R = **亮度归一包络**：E = (I−sky) / local_max(I−sky, 窗≈4σ_psf)

    实测规格是唯一的 —— **R 必须在 ≲4σ_psf 内从 1 落到 0**：
        · 无亮度平台：过渡落在 2~4σ_psf 内则平台降幅回到基线，落到 5σ_psf 外就出现平台
        · 无噪声环：ν 要在 r_bright 前回到 1（r_bright = 4.6σ_psf / 4.1σ_psf）
        · 保留锐化：R 在核心必须真的到 1
      用"对比度的绝对阈值"定义 R 的几种旧写法做不到这一点：T ∝ 峰值/σ_psf ÷ 天空梯度，
      同一个阈值在不同目标上落到的 σ_psf 倍数会漂 → 换目标就得重标定，甚至锐化整个失效。
      那几种写法（对数斜坡 / 余弦距离衰减 / S 型）已删除，R 只剩本函数这一种。

    E 的关键性质：对高斯星点，窗内极大就是峰值 I0，于是
        E(r) = I(r)/I0 = exp(−r²/2σ²)（r 小于半个窗时）
        更一般地 E(r) = I(r)/I(max(0, r−2σ)) = exp(−2r/σ + 2)
      **只含 r/σ_psf，与峰值亮度无关** → 亮星暗星得到同一条径向曲线 → 换目标不漂。
      数值上：核心 1.0、2σ_psf 处 0.135、3σ_psf 处 0.011 → 过渡在 3σ_psf 内完成 ✓
      空白天区：I−sky ≈ 噪声，local_max ≈ 3~4σ 的噪声极大 → E ≈ 0.2~0.3，
        再被 env_eps·σ 项压到接近 0 → 朦胧区严格吃满信噪比 ✓
      平滑亮星云：local_max ≈ 自身 → E ≈ 1 → 该区退化为"全局清晰度加权"（经典做法）。
        这不是缺陷：那里 R 空间上近乎常数 → 无 λ' → 不产生平台/环，代价只是 N_eff。

    参数全部无量纲：env_win_mult（窗 = 该倍数×2×σ_psf）、env_eps（天空抑制，以 σ 为单位）、
    env_p（形状幂）。
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
    """分区权重 R ∈ [0,1]：此处是"细节型"还是"朦胧型"

    R 只剩一种口径，两处都是实测挑出来的，没有并列方案可切：
      **映射** = 亮度归一包络（_r_envelope）：径向轮廓只含 r/σ_psf，换目标不漂。
      **判据** = 边缘强度 T = 局部 RMS(|∇Sref|) / 其全图中位，见下方实现。
        高斯星点的 |∇I| 在 r≈σ_psf 处达峰、r≈3σ_psf 降到峰值的 1%，支撑天然贴合星点本身
        （≈1.3×FWHM），噪声台阶因此落在星光里而不是背景上；
        无结构的亮星云 |∇I|≈0 → R≈0，仍符合"朦胧型只需信噪比"的定义。
        （用带通 RMS 的旧口径会把星点变成"正核+负环"，负环把支撑撑到星点尺寸两倍以上，
         台阶落到纯背景 → 星周可见噪声环；已删。）
      无量纲化：T 一律除以自己的全图中位（天空占多数像素 → 中位就是天空水平，
      T_sky ≡ 1），与目标的绝对亮度、曝光、增益、窄带/宽带全部无关；
      σ_psf 与 win 都由实测 FWHM 得到 → 换目标不改数值也不改含义。
      T 现在只用于报表诊断（info 里的 T_p50…），不参与 R 的计算。
    """
    win = int(max(3, round(float(sigma_psf) * win_mult)))
    win |= 1                                        # 奇数窗，中心对齐
    gy, gx = np.gradient(sref.astype(np.float64))
    g2 = gx * gx + gy * gy
    rms = np.sqrt(np.maximum(
        ndimage.uniform_filter(g2, size=win, mode='nearest'), 0.0))
    sig_hp = float(np.median(rms))
    T = rms / max(sig_hp, 1e-30)

    # 包络映射不经过 T：直接用亮度归一包络，径向轮廓只含 r/σ_psf
    R = _r_envelope(sref, sigma_psf, env_win_mult, env_eps, env_p)

    # 全局下限：R_eff = floor + (1−floor)·R。见 R_FLOOR 常量的推导与实测依据。
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
    """W[i,r] = C_i^(A·R_r) · S_i^(B·(1−R_r))，并逐像素归一使 mean_i(W) = 1

    融合式的形状选择依据（用户整理稿第五节的 f）：
        · 幂律乘法 → 两端行为干净可解释：R=1 只看清晰度，R=0 只看信噪比
        · 指数上线性插值（A·R 与 B·(1−R)）→ R 连续变化时 W 连续变化，无台阶、无拼接痕迹
        · B=2 不是调出来的：加权平均方差最小 ⇔ W ∝ 1/σ² ⇔ S²（S = σ_中位/σ_i）
        · A 是唯一需要标定的数：它决定"细节区愿意用多少信噪比换解析力"
    逐像素归一 mean_i(W)=1 保证加权平均不改变总曝光量（成品背景水平与
               等权叠加一致），也让"权重"只有相对意义、与帧数无关。
    **按条带调用**，不实体化全幅 (n,H,W) 权重场：166 帧全幅时那是
               166×244MB ≈ 40 GiB。条带内算完立刻用掉。
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
    """排异 + 唯一一次加权平均：S = Σ W·D / Σ W

    20261001 v7 重构（用户：停止打补丁，找真正合适的排异）。核心思想：**豁免余量不靠
    任何经验常数或代理量，而是逐条带从数据里测出"真实分歧的上界"**：
        m = median_i  P99_{R>0.5 像素}( |D_i−anchor| / (anchor−sky) )
    即每帧在细节像素上"偏差/信号"的 P99（伪迹只占极少数像素，动不了 P99），再跨帧取
    中位数（个别帧上的伪迹/坏像素动不了中位数）。m 就是该条带实测的真实视宁度分歧上界
    ——天然包含视场梯度、混星、PSF 形状误差等一切真实效应，换数据自动跟随，零移植成本。
        阈值(r) = κ·1.4826·MAD(跨帧|偏差|) + m·R_r·(anchor−sky)
        · 天空/朦胧区 R≈0 → 纯 κσ clipping（κ 以 σ 为单位，MAD·1.4826=σ 估计）；
          中位数锚对多帧伪迹汇合免疫（6888 实测 6/32 帧污染一处不拉走锚）。
        · 细节区：偏差 ≤ m·信号 的是真实分歧（按构造豁免，星点零损伤）；
          超出的是伪迹（绝对量，偏差/信号 ≫ m）→ 被排——星核上的伪迹第一次可排。
        · 少数派闸门：超额被剔数（扣除噪声期望误排 n·erfc(κ/√2)）> rej_max_frac×n
          → 该像素不排异（多数帧共有结构、配准黑边）；期望修正使闸门与 κ 解耦
          （κ=1 实测案例：噪声尾误排顶穿旧闸门、把真伪迹裹挟成"多数派"而漏网）。
    历次失败模式在此结构下各自消失：masking（中位数锚）、削星核（m 按构造盖住真实分歧，
    6888 实测 144 星零损伤）、天空当信号（信号=anchor−sky 相对量）、视场梯度（m 实测）、
    κ 与闸门耦合（期望修正）。R 未传入时退化为单段 κ·MAD（单元测试等）。
      排异掩膜只依赖各帧数值、不依赖权重 → 权重均匀的像素上与无权排异逐位一致。
      被剔的 (帧,像素) 权重置 0，再归一，仍然是同一次加权平均。
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
                # 实测真实分歧上界：每帧细节像素 |偏差|/信号 的 P99，再跨帧中位数
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
    """当前可用物理内存（拿不到就按 8 GiB 保守处理）"""
    try:
        import psutil
        return int(psutil.virtual_memory().available)
    except Exception:
        return 8 * 2 ** 30


def plan_strip(n_frames: int, width: int, frames_bytes: int,
               workers: int = 1, logf=log) -> int:
    """按"扣掉已驻留帧之后的可用内存"反推条带高度

    一条带内的临时量：W、D、dev、|dev| 各 (n,strip,w) float32/float64，
               外加 bad(bool) 与几个同尺寸中间量 → 按每元素 24 字节 × n 估（宁可估大）。
               帧数据本身已在内存（frames_bytes），必须先从可用量里扣掉，否则 166 帧全幅
               会把条带算得过大直接 OOM。
    workers：并行时每个工作进程各持**一条**条带，故预算要按进程数均分，
               否则 N 条带同时在算 = N 倍内存。
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
# 并行执行：叠加按**条带**切，测星点按**帧**切，都是多进程
# ---------------------------------------------------------------------------
# 为什么这两处可以并行而不改变结果：两者的每一块都只看自己的输入，互不引用。
#   · 叠加   一条带只读那几行帧数据，结果写回 Stk/neff 互不重叠的行段；
#   · 测星点 第 i 帧的 FWHM 只由第 i 帧决定。
# 故并行只改"谁在算"，不改"算什么"，输出文件与串行版逐位一致。
# 顺序也必须保住：Stk/neff 的行段、fwhms 的下标、以及排异率与最大权重
#   这两个浮点累加，都按提交顺序落位/相加 —— 用 map（保序）而不是 as_completed。
#
# 帧数据不复制：工作进程由 initializer 各自**只读打开**同一个临时帧文件，
#   真正占内存的只是"一条带"的临时量。故进程数由内存预算反推（见 plan_strip、
#   plan_workers），而不是照抄 CPU 核数。
# 设 DWT_NO_PARALLEL=1 可强制串行（回归脚本与排查用）


def _no_parallel() -> bool:
    """串行开关：**每次现读**环境变量 —— 回归脚本要在运行中途指定它，故不做成常量"""
    return bool(os.environ.get('DWT_NO_PARALLEL'))


_MEM: Dict[str, object] = {}      # 工作进程内：帧后端访问，由 _worker_open* 建立


def plan_workers(per_worker_bytes: int, what: str = '', cap: int = CAP_WORKERS) -> int:
    """进程数 = min(核数−1, cap, 可用内存 ÷ 每进程需求)

    cap 按阶段给（见 CAP_* 常数），不再一刀切：
               内存型阶段（叠加）受内存约束、cap 小；算力型阶段（测星点，只取星点小窗）
               内存几乎不占，cap 就可以放开到核数级别。
               内存不够就自动退回更少进程，退到 1 即串行。
    """
    avail = _avail_bytes()
    per = max(1, int(per_worker_bytes))
    nw = max(1, min((os.cpu_count() or 4) - 1, cap, int(avail // per)))
    if what:
        log(f'[并行] {what} {nw} 进程（可用 {avail / 2 ** 30:.1f} GiB，'
            f'每进程约 {per / 2 ** 20:.0f} MiB）')
    return nw


def _worker_open(path: str, n: int, h: int, w: int, off: np.ndarray) -> None:
    """只读打开临时帧文件（每个进程只开一次，之后各自切片）"""
    _MEM['mm'] = np.memmap(path, dtype=np.float32, mode='r', shape=(n, h, w))
    _MEM['off'] = off


def _task_fwhm(job) -> Tuple[int, float]:
    """一帧的星点 FWHM 中位（帧号一并返回，父进程按下标落位）

    只从帧后端取星点小窗，不物化整帧 —— 一帧 244 MiB 而小窗合计 ~0.5 MiB，
            这一步因此从"每帧重读一整帧"变成"几乎免费"。
    """
    i, stars = job
    return int(i), measure_fwhm_frame(_MEM['mm'], i, _MEM['off'], stars)


def _task_strip(job):
    """一条带的权重 + 排异 + 加权平均（与串行的三段调用完全同序同式）"""
    y0, y1, Rr, C, S, A, B, rej_k, sky0, rej_gate = job
    W = strip_weights(C, S, Rr, A, B)
    mm, off = _MEM['mm'], _MEM['off']
    D = np.stack([mm[i, y0:y1] - np.float32(off[i]) for i in range(len(C))])
    s, ne, rr = stack_strip(D, W, Rr, sky0, rej_k=rej_k, rej_max_frac=rej_gate)
    return s, ne, rr, float(W.max(axis=0).mean())


def _worker_open_rw(path: str, n: int, h: int, w: int) -> None:
    """读帧阶段的工作进程：以 r+ 打开临时帧文件

    这里的进程要**写**帧数据。各进程映射的是同一个文件，
            映射共享同一份页缓存，故互不重叠地各写各的帧号是安全的；
            但**不在工作进程里 flush** —— 刷盘与关闭统一由父进程在全部读完后做。
    """
    _MEM['mmw'] = np.memmap(path, dtype=np.float32, mode='r+', shape=(n, h, w))


def _task_read_frame(job):
    """读一帧 → 裁剪 → float32 → 曝光归一 → 帧级天空中位与噪声尺度

    与串行循环里那一段逐字同式；结果写进自己那一格，标量与元数据字典回传由父进程按下标落位。
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
    return int(i), med, sig, meta


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

# 元数据透传的排除清单：成品自己写的关键字 + PI 逐帧测量关键字（只对单帧成立）
_META_SKIP_KW = {'TOTALEXP', 'NCOMBINE', 'STACKMODE'}
_META_SKIP_KW_PREFIX = ('PSF', 'NOISE')


def _aggregate_fits_keywords(metas: List[Dict]) -> List[Dict]:
    """跨帧聚合 FITS 关键字：全帧同值 → 透传；DATE-OBS → 最早、DATE-END → 最晚一帧结束；
    逐帧测量（PSF*/NOISE*，PI 口径只对单帧成立）与成品自身关键字不透传。"""
    n = len(metas)
    vals: Dict[str, List[str]] = {}
    order: List[str] = []
    for m in metas:
        for kw in m.get('fits_all', []):
            name = str(kw['name']).upper()
            # xisf_io 惯例：字符串值带单引号存储 → 比较与解析前剥离
            if name not in vals:
                vals[name] = []
                order.append(name)
            vals[name].append(str(kw['value']).strip("'\""))
    out: List[Dict] = []
    for name in order:
        v = vals[name]
        if len(v) != n or name in _META_SKIP_KW or name.startswith(_META_SKIP_KW_PREFIX):
            continue
        if name == 'DATE-OBS':
            from datetime import datetime, timedelta
            ts = sorted(datetime.fromisoformat(s) for s in v)
            out.append({'name': 'DATE-OBS', 'value': f"'{ts[0].isoformat()}'",
                        'comment': 'earliest frame start (UTC)'})
            out.append({'name': 'DATE-END',
                        'value': f"'{(ts[-1] + timedelta(seconds=float(np.median(
                            [float(m['exptime']) for m in metas])))).isoformat()}'",
                        'comment': 'last frame end (UTC)'})
            continue
        if len(set(v)) == 1:
            kw0 = next(k for k in metas[0]['fits_all'] if str(k['name']).upper() == name)
            out.append({'name': name, 'value': kw0['value'], 'comment': kw0.get('comment', '')})
    return out


def _aggregate_properties(metas: List[Dict]) -> List[Dict]:
    """跨帧聚合 XISF Property：全帧同值 → 原样透传（base64 向量等不变形）；
    数值型逐帧略异 → 取中位数；Observation:Time:Start/End → 最早/最晚。
    PCL:/PixInsight:（逐帧处理签名与测量，只对单帧成立）不透传。"""
    n = len(metas)
    by_id: Dict[str, List[Dict]] = {}
    for m in metas:
        for p in m.get('props_raw', []):
            by_id.setdefault(p['id'], []).append(p)
    raws: List[Dict] = []
    for pid, ps in by_id.items():
        if len(ps) != n or pid.startswith(('PCL:', 'PixInsight:')):
            continue
        same = len({(p['type'],
                     tuple(sorted((k, v) for k, v in p['attributes'].items() if k != 'id')),
                     p['text']) for p in ps}) == 1
        p0 = ps[0]
        if same:
            raws.append(p0)
            continue
        vt = p0['type']
        vv = [p['attributes'].get('value') for p in ps]
        if vt in ('Float32', 'Float64', 'Int8', 'Int16', 'Int32', 'Int64',
                  'UInt8', 'UInt16', 'UInt32', 'UInt64'):
            try:
                med = float(np.median([float(x) for x in vv]))
                raws.append({'id': pid, 'type': vt,
                             'attributes': {'id': pid, 'type': vt, 'value': repr(med)},
                             'text': None})
                continue
            except (TypeError, ValueError):
                pass
        if vt == 'TimePoint':
            pick = min(vv) if pid.endswith(':Start') else (max(vv) if pid.endswith(':End') else None)
            if pick is not None:
                raws.append({'id': pid, 'type': vt,
                             'attributes': {'id': pid, 'type': vt, 'value': pick},
                             'text': None})
            continue
        # 其余逐帧不同的（字符串等）：跳过
    return raws


def _pws_props(diag: Dict) -> List[Dict]:
    """引擎诊断量 → PWS: 命名空间标量 Property（成品自身的星点/噪声/参数读数）。
    String 类型走 text 内容，数值走 value 属性（与 XISF 1.0 规范一致）。"""
    items: List[Tuple[str, str, object]] = [('PWS:Software', 'String',
                                             'DWT · Partition-Weighted Stacking')]
    for k, t in (('A', 'Float64'), ('B', 'Float64'), ('RejK', 'Float64'),
                 ('RejGate', 'Float64'), ('ScaleOut', 'Float64'), ('RejRate', 'Float64'),
                 ('StarCount', 'Int32'), ('FwhmFrameMedian', 'Float64'),
                 ('FwhmOutput', 'Float64'), ('NeffMedian', 'Float64'),
                 ('SigmaFrameMedian', 'Float64'), ('SigmaSky', 'Float64')):
        if diag.get(k) is not None:
            items.append((f'PWS:{k}', t, diag[k]))
    out: List[Dict] = []
    for pid, t, v in items:
        if t == 'String':
            out.append({'id': pid, 'type': t, 'attributes': {'id': pid, 'type': t}, 'text': str(v)})
        else:
            out.append({'id': pid, 'type': t,
                        'attributes': {'id': pid, 'type': t, 'value': str(v)}, 'text': None})
    return out


def save_linear_xisf(path: Path, arr: np.ndarray, metas: List[Dict], mode_text: str,
                     diag: Optional[Dict] = None) -> None:
    """导出线性态成品：全程只有线性运算（曝光归一常数、量纲回乘常数、
    天空加性平移、Σ W·D/Σ W 加权平均），不做任何拉伸/归一/截断，
    导出前仅 float32 连续化。
    元数据：逐帧 FITS 关键字与 XISF Property 跨帧聚合透传（观测/仪器信息
      不因叠加丢失），并写入 PWS: 命名空间的成品诊断量（星点数、逐帧/成品
      FWHM、σ、参数、排异率、量纲回乘）。
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
    ] + _aggregate_fits_keywords(metas)
    prop_raws = _aggregate_properties(metas) + _pws_props(diag or {})
    if prop_raws:
        # 有 Property → 走 xisf_io 的原始元素回写路径（base64 向量等逐字节保真）
        img.original_property_info = prop_raws
    else:
        # 素材无任何 Property（纯 FITS 素材）→ PWS 诊断量走标量字典路径
        img.other_properties = {
            p['id']: (p['text'] if p['text'] is not None else p['attributes'].get('value'))
            for p in _pws_props(diag or {})
        }
    with contextlib.redirect_stdout(io.StringIO()):
        write_xisf(str(path), img, compression=None)
    log(f'[输出] 线性态 XISF {path.name}  {a.shape[1]}×{a.shape[0]}  '
        f'值域 {float(np.nanmin(a)):.3e}~{float(np.nanmax(a)):.3e}  '
        f'元数据：关键字 {len(img.fits_keywords)}  Property {len(prop_raws)}')


def run(p: PwsParams, on_log=None, on_progress=None, should_cancel=None) -> Dict:
    """PWS 引擎的对外唯一入口（界面与命令行都调它）

    三个钩子都可选：不传就是纯命令行行为（日志走 stdout、无进度、不可终止）。
        跑完自动解绑，避免界面回调被后续调用意外触发。
    """
    set_hooks(on_log, on_progress, should_cancel)
    try:
        return _run(p)
    finally:
        set_hooks()


def _run(p: PwsParams) -> Dict:
    """引擎入口（数值路径与 test_tools/DWT_stack_v2.py **逐位一致**，
    由 DWT/tests/check_equiv.py 核对）。

    20261006 分组叠加：展开素材后按帧头滤镜名自动分组（group_frames_by_filter），
    逐组独立走一遍完整流程（读帧→星表→R 场→叠加→导出）。各组之间不共用任何
    中间量——不同滤镜的星点亮度/天空水平差异大，星表、σ、权重必须组内自洽。
    只有一种滤镜时只有一组，行为与旧版逐位一致。
    """
    photos = (p.photos or '').strip()
    if not photos:
        raise SystemExit('请选择素材（目录或单张帧）')
    frames, sources = collect_frames(photos)
    if not (p.out or '').strip():
        raise SystemExit('请选择输出目录（不放在素材目录里，避免成品被当成帧）')
    out_dir = Path(p.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    _emit('读帧', 0.0, '读头分组')
    groups = group_frames_by_filter(frames)
    multi = len(groups) > 1
    t_start = time.perf_counter()
    res: Dict = {}
    summaries: List[Dict] = []
    for gi, (fname, gframes) in enumerate(groups.items()):
        stem = _out_stem(p.tag, fname, multi)
        # 多组时每组日志都带前缀区分（无滤镜信息的组用「未知」）
        _set_group(gi, len(groups), (fname or '未知') if multi else '')
        log(f'—— 组 {gi + 1}/{len(groups)}：{fname or "无滤镜信息"}，'
            f'{len(gframes)} 帧 ——')
        res = _run_group(gframes, sources, p, out_dir, stem)
        summaries.append({
            'filter': fname, 'n': int(res['n']), 'shape': res['shape'],
            'fwhm_med': float(res['fwhm_med']), 'fwhm_out': float(res['fwhm_out']),
            'rej_rate': float(res['rej_rate']),
            'stem': stem, 'file': str(out_dir / f'{stem}.xisf'),
        })
        if multi:
            res['stack'] = None      # 成品已落盘；别把每组成品都攒在内存里
    _set_group(0, 1, '')
    if multi:
        log(f'[时] 全部 {len(groups)} 组总计 {time.perf_counter() - t_start:.1f}s')
        res['groups'] = summaries
    return res


def _run_group(frames: List[Path], sources: List[Path], p: PwsParams,
               out_dir: Path, stem: str) -> Dict:
    """单滤镜组的叠加本体（原 _run 的数值路径原样搬入）：只把「展开素材 /
    建输出目录」上提到 _run，tag 换成成品文件主名 stem，算法逐行未动。"""
    # 参数别名：下面整段算法体保持 v2 原样，只把入参换成 p.xxx
    A, B = float(p.A), float(p.B)
    r_floor = float(p.r_floor)
    env_win_mult, env_eps, env_p = float(p.env_win_mult), float(p.env_eps), float(p.env_p)
    rej_k, n_star = float(p.rej_k), int(p.n_star)
    rej_gate = float(p.rej_gate)                  # 少数派闸门（推荐默认 1/3，可设）
    limit = int(p.limit) or None
    crop, keep_frames, frames_dir = int(p.crop), bool(p.keep_frames), p.frames_dir
    save_xisf = True
    t_start = time.perf_counter()

    _emit('读帧', 0.0, '准备')

    # ---- 阶段 0：读帧 + 帧级归一（自动选驻留 / memmap）----
    center = resolve_center(frames, crop, parse_center(p.center))
    fs = prepare_frames(frames, ';'.join(str(s) for s in sources), crop, center,
                        limit, frames_dir, keep_frames=keep_frames)
    n = len(fs)
    metas = fs.metas
    skies = fs.skies
    # 量纲回乘：内部一切计算都在 EXPTIME_REF 基准上（权重/排异全是比值，不受影响），
    #   但成品若留在该量纲，数值 = 原始×(基准/曝光)，跨软件对比必踩量纲坑
    #   （900s 素材的读数只有原始的 1/3）。故导出前把成品乘回"曝光中位/基准"：
    #   全部同曝光 → 逐位回到输入帧量纲；混合曝光 → 中位曝光量纲（约定明确）。
    #   σ 的展示/落盘同步该缩放：均匀缩放不改 S/σ 比值，权重数学零变化。
    t_med = float(np.median([float(m['exptime']) for m in metas]))
    scale_out = t_med / EXPTIME_REF

    # S_i = σ_中位 / σ_i（归一化后测 → 曝光差异已被吸收，可直接跨帧比）
    sig = fs.sigma
    S = np.median(sig) / np.maximum(sig, 1e-30)
    sig_out = sig * scale_out          # 展示与落盘量纲 = 成品量纲（内部仍用 sig 算比值）
    log(f'[S] 单帧信噪比权重（σ_中位/σ_i）{S.min():.3f}~{S.max():.3f}  中位 {np.median(S):.3f}')
    log(f'[σ] 逐帧噪声 {sig_out.min():.4e}~{sig_out.max():.4e}'
        f'（极差 {sig_out.max() / sig_out.min():.2f}×，量纲=素材）')

    # ---- 阶段 A：C_i（星点 FWHM，开源 fit_fwhm）----
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

    # 逐帧独立、互不引用 → 可分给多进程；结果按下标落位，与串行逐位一致。
    #   只取星点小窗 → 每进程几乎不占内存，进程数可以放开（CAP_FWHM）
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
    log(f'[C] 逐帧星点 FWHM（开源 fit_fwhm，{len(stars)} 颗中位）'
        f' {np.nanmin(fwhms):.2f}~{np.nanmax(fwhms):.2f}px  中位 {fmed:.2f}px  '
        f'{time.perf_counter() - t1:.1f}s')
    log(f'[C] 清晰度权重（FWHM_中位/FWHM_i）{C.min():.3f}~{C.max():.3f}')
    order = np.argsort(fwhms)
    log('[C] 最锐 3 帧 ' + '  '.join(
        f'#{int(i)}({fwhms[int(i)]:.2f}px)' for i in order[:3]))
    log('[C] 最糊 3 帧 ' + '  '.join(
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
    log(f'[R] 参考像 = 最锐 {k} 帧等权中位  映射=包络(亮度归一)  口径=边缘强度  '
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
            # 每条带只看自己那几行、写回互不重叠的行段 → 可分给多进程；
            #   map 保序 → 行段落位与两个浮点累加都与串行同序
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
        # 正常跑完与中途终止都要清理临时帧文件
        fs.close(keep=keep_frames)
    # 条带全部落位后一次回乘（串行/并行在此汇合，只乘一次；元素级乘法与切分无关）
    Stk *= np.float32(scale_out)
    log(f'[量纲] 成品回乘 ×{scale_out:g}（曝光中位 {t_med:g}s / 归一基准 {EXPTIME_REF:g}s），'
        f'量纲与素材一致')
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
    sky_sd = None
    for name, msk in (('细节区 R>0.5', det), ('朦胧区 R<0.1', amb), ('天空(下半亮度)', sky_m)):
        if msk.sum() == 0:
            continue
        sd = 1.4826 * float(np.median(np.abs(Stk[msk] - np.median(Stk[msk]))))
        if name.startswith('天空'):
            sky_sd = float(sd)
        log(f'[读数] {name:<18} 占 {msk.mean():6.2%}  N_eff 中位 '
            f'{np.median(neff[msk]):5.2f}  σ {sd:.4e}')

    # 成品自身的星点 FWHM（同一星表、同一开源口径）→ 与单帧/参照栈直接可比
    _emit('验收', 0.3, '量成品星点')
    f_out = measure_fwhm(Stk, stars)
    log(f'[验收] 成品星点 FWHM {f_out:.2f}px（单帧中位 {fmed:.2f}px，'
        f'改善 {100 * (1 - f_out / fmed):.1f}%）')

    res = {
        'n': n, 'shape': Stk.shape, 'fwhms': fwhms, 'C': C, 'S': S, 'sigma': sig_out,
        'fwhm_med': fmed, 'fwhm_out': f_out, 'sigma_psf': sigma_psf,
        'R_info': rinfo, 'neff': neff, 'rej_rate': float(rej_rate),
        'A': A, 'B': B, 'rej_k': rej_k, 'stars': stars,
        'metas': metas, 'skies': skies, 'scale_out': float(scale_out),
    }

    if save_xisf:
        p_out = out_dir / f'{stem}.xisf'
        _emit('验收', 0.7, '导出成品')
        save_linear_xisf(p_out, Stk, metas,
                         f'PWS A={A:g} B={B:g} rej_k={rej_k:g}',
                         diag={'A': A, 'B': B, 'RejK': rej_k, 'RejGate': rej_gate,
                               'ScaleOut': float(scale_out), 'RejRate': float(rej_rate),
                               'StarCount': len(stars), 'FwhmFrameMedian': float(fmed),
                               'FwhmOutput': float(f_out),
                               'NeffMedian': float(np.median(neff)),
                               'SigmaFrameMedian': float(np.median(sig_out)),
                               'SigmaSky': sky_sd})
        np.save(out_dir / f'{stem}_neff.npy', neff)
        np.save(out_dir / f'{stem}_R.npy', R)
        np.savetxt(out_dir / f'{stem}_weights.txt',
                   np.column_stack([np.arange(n), fwhms, C, sig_out, S]),
                   header='idx fwhm_px C_sharp sigma S_snr', comments='# ', fmt='%.6g')
        log(f'[输出] 逐帧权重表 {stem}_weights.txt / R 场 {stem}_R.npy / '
            f'N_eff {stem}_neff.npy')

    log(f'[时] 总计 {time.perf_counter() - t_start:.1f}s')
    _emit('验收', 1.0, '完成')
    res['stack'] = Stk
    return res


def build_parser() -> argparse.ArgumentParser:
    """命令行参数表由 pws_params.PARAMS 生成：与界面同源，加参数只改一处"""
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
    """命令行入口：批处理，以及与 test_tools/DWT_stack_v2.py 做等价性回归"""
    a = build_parser().parse_args(argv)
    p = PwsParams(**{prm.key: getattr(a, prm.key) for prm in PARAMS})
    try:
        run(p)
    except Cancelled:
        log('已终止')
    return 0


if __name__ == '__main__':
    main()
