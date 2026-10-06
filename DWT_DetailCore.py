#!/usr/bin/env python
# -*- coding: utf-8 -*-
# DontWasteTime 核心层 v3：延展结构细节需求分析
#
# 目的
# 判断一幅深空画面中，延展结构（星系核、旋臂、尘埃带、云气）的哪些区域
# 细节丰富（必须靠锐利帧保住），哪些区域本身平滑（模糊帧可全量参与，
# 白换 √N 的信噪比收益）。
#
# 两个层次严格分离
# 星点层：规则已定，永远只用最锐利子集，不参与"细节高低"的判定；
# 结构层：本模块的判据对象，只针对延展结构。
#
# 为什么用"倍频程档 + 截尾幅度"而不是"星点掩膜 + 填充 + FFT"
# v2 实测失败：星核挖掉后留下亮环，且饱和星被检测器拒绝而漏掩，
# 填充还会在掩膜边缘注入假高频。掩膜+填充这条路本质脆弱。
# 改为：用高斯差分构造倍频程档（等价于频域分档，但在空间域完成），
# 每块每档用"截尾方差"度量真实幅度——星点是稀疏极值，天然被截掉，
# 延展结构是遍布整块的能量，完整保留。无需掩膜、无需填充、无边缘伪影。
# 噪声不再靠估计底，而是用高斯核的解析增益严格扣除（更严谨且更快）。
#
# 本文件以 MIT 许可证发布，全文见 DWT/LICENSE，授权范围见 DWT/README.md。
# 依赖许可：numpy/scipy/astropy/photutils 均为 BSD 系，可商用
# XISF 读取使用本工程自研 xisf_io.py（PyPI 的 xisf 包为 GPL-3，禁止引入）

import numpy as np
import warnings
import struct            # 20261006 read_filter_hint：XISF 固定头的 header_length 解析
import hashlib           # 缓存指纹
import json              # 缓存里的标量元数据
import math              # 截尾因子解析式（erf）
import os                # 自动决定并行进程数
import tempfile          # 缓存默认目录
import time              # 阶段耗时日志
from concurrent.futures import ProcessPoolExecutor, as_completed  # 逐帧并行
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from astropy.io import fits
from astropy.stats import sigma_clipped_stats
from scipy import ndimage
from scipy.spatial import cKDTree

FITS_SUFFIXES = {'.fits', '.fit', '.fts'}
EXPTIME_REF = 300.0  # 曝光归一化基准（秒）

# 倍频程档表：(σa, σb, decim)；档中心尺度(px) = sqrt(σa·σb)×2.3548，
# 即 5 / 10 / 20 / 40 / 80 / 113 / 226 px。113 与 226 两档为 753 这类
# 暗弱云气而加：它的结构在那些粗档上才显著高出邻域纹理（探针实测）。
# decim 与 test_tools/DWT_scale_probe.py 的 SCALES 对齐（同一物理尺度、
# 同一抽样口径），保证探针/原型与核心层的读数直接可比。
BAND_TABLE = ((1.5, 3.0, 1), (3.0, 6.0, 1), (6.0, 12.0, 2), (12.0, 24.0, 4),
              (24.0, 48.0, 8), (34.0, 68.0, 16), (68.0, 136.0, 16))
STRUCTURE_MIN_PX = 8.0   # 小于该尺度由星点/PSF 核心主导，不参与判据
PSF_CORE_PX = 3.5        # 由实测得到的 PSF 核心宽度
PSF_SIG_PX = PSF_CORE_PX / 2.3548   # PSF 高斯等效 σ（派生掩膜半径用）
CLIP_MAD = 3.0           # 截尾阈值（单位 MAD），用于剔除星点等稀疏极值
GATE_FLOOR = 3.0         # 连续场口径的可测性闸门：E ≥ 该倍数 × 同管线经验噪声底
SUBSAMPLE = 4            # 估计块内 MAD 的抽样步长（提速用）
BLOCK_SCALE_RATIO = 4.0  # 块边长至少是所判尺度的该倍数，否则块内样本不足

# 星点移除路线一：固定星表 → 逐档亮度派生半径掩膜 → 归一化均值填充
# 实测（7331，探针 _tmp_mask_radius）：填充把热点块 (11,7)/(9,4) 从
# prom 2.25/1.87 压到 0.00/0.38，同时星系核块 (6,6) 保住并成为最热，
# 说明星点在带通域被真正移除、延展结构原样保留。
# 但它有硬上限：掩膜覆盖率必须低。密集星场（753 实测 10967 颗/3072²，
# 峰值 99 分位 801σ）在 80px 档上并集覆盖 99.2%，铺满即等于把结构一起抹掉，
# 因此覆盖率超阈值时改用路线二（中值去星 + 星点斜率闸门，753 已验证口径）。
FILL_K = 3.0             # 星响应降到该倍数 × σ_band 以下即不再排除
FILL_RMAX_MULT = 3.0     # 排除半径上限 = 该倍数 × σb
FILL_WIN_MULT = 2.5      # 填充窗 = 该倍数 × 半径上限（保证窗内仍有未掩像元）
FILL_SMEAR_PX = 1.5      # 降采样填充时附加半径（覆盖分箱涂抹，降采样像素）
FILL_COV_MAX = 0.45      # 掩膜并集覆盖率上限；超过则该目标改走中值去星路线

# 密集星场的两道补救（753 实测暴露的问题）
# 1) 星点闸门：某块的功率必须有 (1 − STAR_FRAC_MAX) 以上无法用
# "该块星点数"解释，读数才算数。这比"与全图分布比"稳健：
# 全图分布型闸门在稀疏目标（只有星系亮）和密集星场两侧都会失效（实测）。
# 2) 尺度自适应：某档功率若与分块星点数强相关，说明该档量到的是星场而非云气
STAR_FRAC_MAX = 0.5      # 星点可解释的功率占比上限（超过即该档该块作废）
STAR_R_MAX = 0.45        # 与星点密度的相关系数超过该值的档，视为被星场主导
HP_SIGMA = 8.0           # 星点计数前的空间高通 sigma（px）：滤掉延展光，只留点源

# 星点层子集边界：集中度排序上的"自然断档"最小倍数
# 固定比例（30%）会把同一清晰等级的帧切一半：7331select 27 帧实测排名
# 14(0.3420)/15(0.1924) 之间有 1.78 倍断层，30% 只取 8 帧，丢掉 6 帧同等锐利的
# 帧；星核方向分散因此变差（叠加后星点拖线方向一致度 0.94，14 帧纯细帧为 0.81，
# 越小越圆）。改按断档取，无断档时回退固定比例。
SHARP_GAP_MIN = 1.25

# 星点移除：判据之前先把点源从数据里抹掉（753 实测后的根本修法）
# 星点是点源：在数学上必然占满所有细档功率，而暗弱云气的结构尺度远大于 PSF，
# 细档功率天然就低。只靠"统计扣除星点贡献"无法扭转这一点——扣完之后
# 温度图仍会把星场标成高细节、把云气标成低细节，与需求正好相反。
# 改为中值滤波移除：窗 ≈ 2×PSF 核心（7px），点源整体消失，
# 而 ≥10px 的延展结构保留（实测：7px 窗后 10px 档剩真值的 1.2 倍，
# 9px 窗就只剩 1/3，故取 7 不取 9）。不依赖星表、不拟合、不伪造内容。
EXT_MED_PX = 7           # 星点移除的中值窗边长（px）
FLOOR_PATCH = 2048       # 量噪声底用的合成噪声边长（px）

BAND_KW = '_dwt_bands'


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _nanmedian(a: np.ndarray, axis: int = 0) -> np.ndarray:
    """忽略全 NaN 切片告警的中位数"""
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)
        return np.nanmedian(a, axis=axis)


def band_table() -> List[Dict]:
    """倍频程档定义表（含抽样倍率与噪声增益），由 BAND_TABLE 展开"""
    out = []
    for sa, sb, d in BAND_TABLE:
        sa_d, sb_d = sa / d, sb / d
        # 白噪声经该带通核后的方差增益（两个归一化高斯核之差的 L2 范数平方）
        gain = (1.0 / (4.0 * np.pi * sa_d ** 2)
                + 1.0 / (4.0 * np.pi * sb_d ** 2)
                - 2.0 / (2.0 * np.pi * (sa_d ** 2 + sb_d ** 2)))
        out.append({
            'sigma_a': sa, 'sigma_b': sb, 'decim': d,
            'center_px': float(np.sqrt(sa * sb) * 2.3548),
            'noise_gain': float(gain),
        })
    return out


BANDS = band_table()


def band_floor_sigma(sigma: float, sa: float, sb: float, d: int) -> float:
    """该档带通图上白噪声的 σ（解析增益，单位与数据同）

    降采样 d 后噪声方差 σ²/d²，再乘该核的增益（与 band_table 同式）。
    """
    sa_d, sb_d = sa / d, sb / d
    gain = (1.0 / (4.0 * np.pi * sa_d ** 2) + 1.0 / (4.0 * np.pi * sb_d ** 2)
            - 2.0 / (2.0 * np.pi * (sa_d ** 2 + sb_d ** 2)))
    return float(np.sqrt(max(sigma ** 2 / (d ** 2) * gain, 1e-300)))


def derived_radii(peaks: np.ndarray, sa: float, sb: float, d: int,
                  sigma: float, k: float = FILL_K,
                  rmax_mult: float = FILL_RMAX_MULT) -> np.ndarray:
    """数据派生排除半径：星在带通域的响应 ≈ pk·(σp/σb)²（峰处）

    令 pk·(σp/σb)²·e^{-ρ²/(2σb²)} = k·σ_band，解出 ρ = σb·√(2·ln(比值))；
               比值 ≤ 1（星太暗，响应本就在噪声里）→ 0（不排除）。
               实测（探针）：k=3、上限 3σb 时 7331 两块假热斑压掉而真结构保留；
               更保守的 k=1.5/4σb 反而让 (11,8) 反超星系核块，故取 k=3 / 3σb。
    """
    sig_band = band_floor_sigma(sigma, sa, sb, d)
    ratio = np.asarray(peaks, dtype=np.float64) * (PSF_SIG_PX / sb) ** 2 \
        / max(k * sig_band, 1e-30)
    r = np.zeros_like(ratio)
    ok = ratio > 1.0
    r[ok] = sb * np.sqrt(2.0 * np.log(ratio[ok]))
    return np.minimum(r, rmax_mult * sb)


def _fill_geometry_for(band: Dict, peaks: np.ndarray, xs: np.ndarray, ys: np.ndarray,
                       shape, sigma: float):
    """计算某档的填充几何；覆盖率超阈值时返回 (None, 覆盖率)

    覆盖率 = Σπr² / 作业面积（全分辨率口径估计，并集略小于该值）。上限的意义：
               掩膜一旦近乎铺满，填充就等于把整幅换成局部均值，延展结构随之消失
               （753 实测 80px 档 99.2%、113px 档 99.7%），此时必须换路线。
    半径在降采样网格上加 FILL_SMEAR_PX：bin_down 会把星通量摊到邻近降采样
               像素上，掩膜必须把这些像素一起罩住，否则填充后仍留星点残翼。
    """
    d = band['decim']
    r = derived_radii(peaks, band['sigma_a'], band['sigma_b'], d, sigma)
    cov = float(np.sum(np.pi * r ** 2) / max(1, int(shape[0]) * int(shape[1])))
    if cov > FILL_COV_MAX or not np.any(r > 0.5):
        return None, cov
    win = int(round(FILL_WIN_MULT * FILL_RMAX_MULT * band['sigma_b'] / d)) | 1
    geo = {
        'xs': (np.asarray(xs, dtype=np.float64) / d).astype(np.float32),
        'ys': (np.asarray(ys, dtype=np.float64) / d).astype(np.float32),
        'radii': (r / d + FILL_SMEAR_PX).astype(np.float32),
        'win': max(win, 3),
    }
    return geo, cov


def disc_mask(shape, xs: np.ndarray, ys: np.ndarray, radii: np.ndarray) -> np.ndarray:
    """圆形掩膜并集（图章法：同一半径只算一次模板，避免逐星分配网格）"""
    h, w = int(shape[0]), int(shape[1])
    m = np.zeros((h, w), dtype=bool)
    stamps: Dict[int, np.ndarray] = {}
    for x, y, r in zip(xs, ys, radii):
        if r <= 0.5:
            continue
        ri = int(round(float(r)))
        st = stamps.get(ri)
        if st is None:
            yy, xx = np.mgrid[-ri:ri + 1, -ri:ri + 1]
            st = ((yy * yy + xx * xx) <= ri * ri)
            stamps[ri] = st
        y0, x0 = int(round(float(y))) - ri, int(round(float(x))) - ri
        dy0, dx0 = max(0, y0), max(0, x0)
        dy1, dx1 = min(h, y0 + 2 * ri + 1), min(w, x0 + 2 * ri + 1)
        if dy1 <= dy0 or dx1 <= dx0:
            continue
        sy, sx = dy0 - y0, dx0 - x0
        m[dy0:dy1, dx0:dx1] |= st[sy:sy + (dy1 - dy0), sx:sx + (dx1 - dx0)]
    return m


def norm_fill(img: np.ndarray, bad: np.ndarray, win: int) -> np.ndarray:
    """归一化均值填充（被掩像元不进均值），uniform_filter 可分离、O(N)

    与"用邻近中值填"不同：这里填的是"窗内未掩像素的均值"，星点邻域已被
               掩膜清空，窗又取 2.5×半径上限，所以填进去的就是该处的背景/延展流量，
               带通域里不再有星点响应，也没有掩膜边缘的假高频。
    """
    k = (~bad).astype(np.float32)
    num = ndimage.uniform_filter(img * k, size=int(win), mode='nearest')
    den = ndimage.uniform_filter(k, size=int(win), mode='nearest')
    return np.where(bad, num / np.maximum(den, 1e-6), img)


def derived_med(bands: List[float]) -> int:
    """去星中值窗：所判最细尺度的 1/5（下限 2×PSF 核心），取奇数

    窗必须远小于所判尺度（否则把要判的结构自己抹掉），又必须盖住点源
               （≈2×PSF 核心）。753（80/113px）→ 15px，7331（10/20px）→ 7px，
               与两个目标已验收的配置一致。
    """
    w = max(int(min(bands) / 5), EXT_MED_PX)
    return w if w % 2 == 1 else w - 1


def band_allowed(block: int = 256) -> np.ndarray:
    """哪些档参与"延展结构"判据

    上界由块大小决定：块边长必须至少是被判尺度的 BLOCK_SCALE_RATIO 倍，
               否则块内可容纳的独立结构样本太少，测出的功率只是随机起伏，
               判据会退化成噪声图（实测 256px 块判 40px 以上尺度时就出现此问题）。
    """
    hi = block / BLOCK_SCALE_RATIO
    return np.array([STRUCTURE_MIN_PX <= b['center_px'] <= hi for b in BANDS])


def block_for_scale(target_px: float) -> int:
    """推荐的分块大小：让目标尺度正好落在可判范围内"""
    need = target_px * BLOCK_SCALE_RATIO
    return int(2 ** np.ceil(np.log2(max(need, 64))))


# ---------------------------------------------------------------------------
# 帧读取
# ---------------------------------------------------------------------------

def _noop_print(*_a, **_k):
    """静音用的空 print：注入到 xisf_io 模块命名空间，覆盖其内建 print"""
    return None


def read_frame(path: Path) -> Tuple[np.ndarray, Dict]:
    """读取一帧，返回 (float32 二维数组, 元数据字典)

    **线程安全**：xisf_io 的 [DEBUG] 打印改成"给它注入同名 print"来静默，
               不再动全局 sys.stdout（详见下方 .xisf 分支的注释）。
    """
    suffix = path.suffix.lower()
    if suffix in FITS_SUFFIXES:
        with fits.open(path, memmap=False) as hdul:
            data = np.asarray(hdul[0].data)
            hdr = hdul[0].header
        meta = {
            'exptime': float(hdr.get('EXPTIME', hdr.get('EXP_TIME', EXPTIME_REF)) or EXPTIME_REF),
            'date_obs': str(hdr.get('DATE-OBS', '')),
            'object': str(hdr.get('OBJECT', '')).strip(),
            'filter': str(hdr.get('FILTER', '')).strip(),
        }
        # 元数据透传：FITS 卡片转成与 XISF 相同的 {name,value,comment} 列表；FITS 无 Property
        meta['fits_all'] = [
            {'name': str(k), 'value': hdr[k], 'comment': hdr.comments[k]}
            for k in hdr
            if k not in ('COMMENT', 'HISTORY', '')
            and isinstance(hdr[k], (str, int, float, bool))
        ]
        meta['props_raw'] = []
    elif suffix == '.xisf':
        import xisf_io
        # xisf_io 内部有大量调试输出，此处静默，避免污染分析日志
        # **改法**：给 xisf_io 注入同名 print（覆盖它模块内的内建 print）来静默，
        #   不再用 `contextlib.redirect_stdout`——那是**进程级全局状态**，多线程并发读帧时会
        #   互相抢：后进入者把前者的 StringIO 当作"原 stdout"存下，退出时恢复错位 → 主进程
        #   stdout 永久指向废弃缓冲、之后所有日志丢失。注入 print 只改模块字典，线程安全。
        if not getattr(xisf_io, '_DWT_QUIET', False):
            xisf_io.print = _noop_print
            xisf_io._DWT_QUIET = True
        images = xisf_io.read_xisf(path)
        img = images[0]
        data = np.asarray(img.data)
        kws = {k['name'].upper(): k['value'] for k in img.fits_keywords}
        meta = {
            'exptime': float(kws.get('EXPTIME', img.instrument.get('ExposureTime', EXPTIME_REF)) or EXPTIME_REF),
            'date_obs': str(kws.get('DATE-OBS', '')),
            'object': str(kws.get('OBJECT', '')).strip(),
            'filter': str(kws.get('FILTER', '')).strip(),
        }
        # 元数据透传：FITS 关键字全量 + Property 原始元素（base64 向量原样带回，
        #   写出端按 original_property_info 路径逐字节回写，PI 的属性不变形）
        meta['fits_all'] = [dict(k) for k in img.fits_keywords]
        meta['props_raw'] = [
            {'id': p['id'], 'type': p['type'],
             'attributes': dict(p['attributes']), 'text': p['text']}
            for p in img.original_property_info
        ]
    else:
        raise ValueError(f'不支持的格式：{path.name}')

    if data.ndim == 3:
        data = data[..., :3].mean(axis=-1)
    data = np.ascontiguousarray(data, dtype=np.float32)
    meta.update({'path': str(path), 'name': path.name, 'shape': data.shape})
    return data, meta


def read_filter_hint(path: Path) -> str:
    """20261006 轻量读取单帧的滤镜名（分组叠加的依据）：只读文件头，不解码像素。

    FITS：astropy 只读 header，取 FILTER 卡；
    XISF：只读固定头（魔数 8B + header_length 4B 小端）声明的 XML 元数据段
          （几十 KB，不碰 attachment 像素区），FITSKeyword FILTER 优先，
          Property Instrument:Filter:Name 后备。
    读不了（格式不对/头损坏）或没有该信息都返回 ''，调用方把这类帧
    归入"未知"组。不能整帧解码来分组：一帧几百 MB，几十帧就是几十 GB 的白读。
    """
    suffix = path.suffix.lower()
    try:
        if suffix in FITS_SUFFIXES:
            hdr = fits.getheader(path)
            return str(hdr.get('FILTER', '') or '').strip().strip("'").strip()
        if suffix == '.xisf':
            import xml.etree.ElementTree as ET
            with open(path, 'rb') as f:
                head = f.read(16)
                if len(head) < 16 or not head.startswith(b'XISF'):
                    return ''
                (hlen,) = struct.unpack_from('<I', head, 8)
                xml_text = f.read(hlen).decode('utf-8', 'ignore')
            try:
                root = ET.fromstring(xml_text)
            except ET.ParseError:
                return ''
            # 标签名可能带命名空间前缀（PI 写出的文件两种都有），按尾部比对
            val = ''
            for el in root.iter():
                if el.tag.rsplit('}', 1)[-1] == 'FITSKeyword' and \
                        el.get('name', '').upper() == 'FILTER':
                    val = el.get('value', '') or ''
                    break
            if not val:
                for el in root.iter():
                    if el.tag.rsplit('}', 1)[-1] == 'Property' and \
                            el.get('id', '') == 'Instrument:Filter:Name':
                        val = el.get('value', '') or ''
                        break
            return val.strip().strip("'").strip()
    except Exception:
        return ''
    return ''


# ---------------------------------------------------------------------------
# 星点检测与"星点层"选帧（固定星表，避免逐帧检测偏差）
# ---------------------------------------------------------------------------

def find_stars(data: np.ndarray, bg: float, noise: float,
               fwhm_guess: float = PSF_CORE_PX, threshold_sigma: float = 5.0):
    """星点检测（photutils DAOStarFinder，BSD-3 许可）"""
    import photutils
    if hasattr(photutils, 'future_column_names'):
        photutils.future_column_names = True
    from photutils.detection import DAOStarFinder
    finder = DAOStarFinder(fwhm=fwhm_guess, threshold=threshold_sigma * noise,
                           exclude_border=True)
    tbl = finder(data - bg)
    if tbl is None or len(tbl) == 0:
        return np.zeros(0), np.zeros(0), np.zeros(0)
    names = tbl.colnames
    xkey = 'x_centroid' if 'x_centroid' in names else 'xcentroid'
    ykey = 'y_centroid' if 'y_centroid' in names else 'ycentroid'
    return (np.asarray(tbl[xkey], dtype=np.float64),
            np.asarray(tbl[ykey], dtype=np.float64),
            np.asarray(tbl['peak'], dtype=np.float64))


def build_star_list(data: np.ndarray, bg: float, noise: float, box: int = 21,
                    n_max: Optional[int] = 400, peak_hi_frac: float = 0.40):
    """建立固定星表：只取"未饱和、信噪比足够、且孤立"的星

    关键：绝不能取饱和星。饱和星核心是平顶，二阶矩会被严重放大，
               实测能把核心 3px 的星测成 8px。用固定星表后，
               所有帧都在同一批星位上测量，彻底消除逐帧检测带来的排序偏差。
    n_max=None 表示不设数量上限（结构层的星点强度图要全量星表）：
               上限会把星表按峰值等间隔抽样，暗星被抽掉后 Σ峰值² 由少数亮星决定，
               星点强度对"该块星场有多脏"的分辨力下降。默认仍为 400（集中度口径只需
               一批代表性亮星，全量会让逐帧循环变慢）。
    返回值增加 peaks：填充路线的派生半径需要参考帧的星点峰值。
    """
    xs, ys, peaks = find_stars(data, bg, noise)
    if len(xs) < 5:
        z = np.zeros(0)
        return z, z, z

    h, w = data.shape
    half = box // 2
    m = half + 3
    keep = (xs > m) & (xs < w - m) & (ys > m) & (ys < h - m)
    keep &= (peaks >= 20.0 * noise) & (peaks <= peak_hi_frac * float(np.max(data)))
    xs, ys, peaks = xs[keep], ys[keep], peaks[keep]
    if len(xs) < 5:
        z = np.zeros(0)
        return z, z, z

    order = np.argsort(peaks)[::-1]
    if n_max and len(order) > n_max:
        order = order[np.linspace(0, len(order) - 1, n_max).astype(int)]
    xs, ys, peaks = xs[order], ys[order], peaks[order]

    tree = cKDTree(np.column_stack([xs, ys]))
    iso = np.array([len(p) == 1 for p in tree.query_ball_tree(tree, r=1.2 * box)])
    return xs[iso], ys[iso], peaks[iso]


def star_sharpness(data: np.ndarray, bg: float, xs: np.ndarray, ys: np.ndarray,
                   box: int = 21, r_inner: float = 1.6, r_outer: float = 6.0):
    """在固定星位上测"能量集中度"与二阶矩 FWHM

    集中度 = 内圈通量 / 外圈通量。星点越细，能量越集中，该值越高。
               这是行星摄影里选帧最常用的判据，对晕的亮度不敏感，
               比单纯的 FWHM 更能反映"星点是否细腻"。
    """
    if len(xs) < 5:
        return np.nan, np.nan, 0
    half = box // 2
    yy, xx = np.mgrid[-half:half + 1, -half:half + 1]
    rr = np.sqrt(xx ** 2 + yy ** 2)
    inner = rr <= r_inner
    outer = rr <= r_outer
    idx = np.arange(box, dtype=np.float64)

    concs, fwhms = [], []
    for x, y in zip(xs, ys):
        xi, yi = int(round(x)), int(round(y))
        # **边缘星必须跳过**：星表里含贴边星（裁剪域尤其多），切片越界会得到空数组
        #   或短数组，而 inner/outer 掩膜恒为 box×box → 布尔索引直接 IndexError（实测裁剪
        #   模式 t_min 阶梯四轮全在验收读数 3 崩掉，正是最后几行的一颗星）。判据恒等式
        #   patch.shape == (box, box) 同时把"负索引绕回"的短 patch 一起挡住。
        patch = np.maximum(data[yi - half:yi + half + 1,
                                xi - half:xi + half + 1].astype(np.float64) - bg, 0.0)
        if patch.shape != (box, box):
            continue
        f_out = patch[outer].sum()
        if f_out <= 0:
            continue
        # 集中度只在未饱和时可比，用内圈是否触顶做保护
        concs.append(patch[inner].sum() / f_out)

        wx, wy = patch.sum(axis=0), patch.sum(axis=1)
        if wx.sum() <= 0 or wy.sum() <= 0:
            continue
        cx = (wx * idx).sum() / wx.sum()
        cy = (wy * idx).sum() / wy.sum()
        sx = np.sqrt((wx * (idx - cx) ** 2).sum() / wx.sum())
        sy = np.sqrt((wy * (idx - cy) ** 2).sum() / wy.sum())
        if 0.4 < sx < 12.0 and 0.4 < sy < 12.0:
            fwhms.append(2.3548 * np.sqrt(sx * sy))

    if len(concs) < 5:
        return np.nan, np.nan, len(concs)
    return (float(np.median(concs)),
            float(np.median(fwhms)) if fwhms else np.nan,
            len(concs))


def fixed_star_peaks(data: np.ndarray, hp_sigma: float, xs: np.ndarray, ys: np.ndarray,
                     box: int = 1) -> np.ndarray:
    """固定星表口径的逐帧星点峰值：每帧在同一批星位上量本帧高通图的 (2box+1)² 最大值

    为什么必须固定星表（753 实测的根因）：逐帧检测的阈值 = 5×该帧噪声，
               模糊/高噪声帧检出得少（同一晚同曝光同天区的 12 帧：检出 1.7 万~7.4 万颗，
               与 Σ峰值² 相关 +0.971，帧间 Σ峰值² 差 7.16 倍）。于是"星点强度图"里混进了
               帧自身的质量差异：糊帧被系统性低估 → 星点解释量不足 → 残差偏大 → 糊帧被抬高。
               固定星表后每帧量的是同一批星位，帧间差异只剩真实的星点亮度（透明度/视宁度）。
    先高通（σ=HP_SIGMA=8px）再量：不滤掉延展光，云气/星系盘的平滑亮度会被
               算进峰值，在亮区造出与该块星场无关的"假星点强度"（v3 实测口径同此）。
    """
    if len(xs) == 0:
        return np.zeros(0)
    hp = data - ndimage.gaussian_filter(data, hp_sigma, mode='nearest')
    h, w = hp.shape
    xi = np.clip(np.round(np.asarray(xs, dtype=np.float64)).astype(int), 0, w - 1)
    yi = np.clip(np.round(np.asarray(ys, dtype=np.float64)).astype(int), 0, h - 1)
    pk = np.empty(xi.size, dtype=np.float64)
    for i in range(xi.size):
        y0, y1 = max(0, yi[i] - box), min(h, yi[i] + box + 1)
        x0, x1 = max(0, xi[i] - box), min(w, xi[i] + box + 1)
        pk[i] = float(hp[y0:y1, x0:x1].max())
    del hp
    return pk


def fixed_star_shape(data: np.ndarray, xs: np.ndarray, ys: np.ndarray,
                     half: int = 16, mom_r: float = 5.0,
                     sky_in: float = 14.0, sky_out: float = 16.0,
                     thr_frac: float = 0.15, chunk: int = 4096):
    """固定星表口径的逐帧星点形状：返回 (b/a, q)，长度 = 星数

    为什么需要一个"形状"量（本期实测结论）：判断"这帧这颗星锐不锐"不能只看一个数。
               裸峰值只对**模糊**敏感（拖线把核压扁，峰值不一定低）；轴比 b/a 只对**拖线**敏感
               （散焦的星依然是圆的，b/a≈1）。实测只按 b/a 挑帧会挑出"糊但圆"的帧——核峰更低、
               翼部更高，方向完全反了。
    故取**几何平均二阶矩半径** q = (λ1·λ2)^0.25（px）：拖线把 λ1 拉大；模糊把
               λ1、λ2 一起拉大；帧内噪声也把两者都拉大 —— 只有"又尖、又圆、又干净"的帧 q 最小。
               一个数同时管锐度、圆度与信噪比偏差。
    二阶矩只用 thr = 15% 峰值以上的像素并减掉 thr：把背景肩与噪声底一起赶出矩计算
               （不做这一步时，低信噪帧的 q 会被噪声抬大，判据方向会反）。
    局部天空取 r∈[14,16] 环带中位数：星点局部背景逐处不同（星系盘/云气），
               不减掉会把延展结构的斜率算进二阶矩。
    实测（7331select 27 帧 × 400 颗最亮星）：逐帧 q 中位分成清晰两群（13 帧≈0.82、
               14 帧≈1.42，差 1.7 倍）；按 q 选出的 14 帧，核内帧间 PSF 不一致度 0.040，
               而随机 14 帧 0.117、全 27 帧 0.123 —— 即"挑规则帧"把星核花斑的根压下 3 倍。
    """
    n = len(xs)
    if n == 0:
        return np.zeros(0), np.zeros(0)
    h, w = data.shape
    m = int(max(2, round(half)))
    off = np.arange(-m, m + 1, dtype=np.int64)
    gy, gx = np.meshgrid(off, off, indexing='ij')
    rr = np.hypot(gx, gy)
    mom = rr <= float(mom_r)
    ann = (rr >= float(sky_in)) & (rr <= float(sky_out))
    ba = np.full(n, np.nan, dtype=np.float64)
    qq = np.full(n, np.nan, dtype=np.float64)
    xi = np.round(np.asarray(xs, dtype=np.float64)).astype(np.int64)
    yi = np.round(np.asarray(ys, dtype=np.float64)).astype(np.int64)
    for s in range(0, n, int(chunk)):
        e = min(s + int(chunk), n)
        yy = np.clip(yi[s:e, None] + off[None, :], 0, h - 1)
        xx = np.clip(xi[s:e, None] + off[None, :], 0, w - 1)
        win = np.asarray(data[yy[:, :, None], xx[:, None, :]], dtype=np.float64)
        win -= np.median(win[:, ann], axis=1)[:, None, None]
        pk = np.where(mom[None], win, 0.0).reshape(len(win), -1).max(axis=1)
        thr = float(thr_frac) * pk
        wt = np.where(mom[None] & (win >= thr[:, None, None]),
                      win - thr[:, None, None], 0.0)
        s0 = wt.sum(axis=(1, 2))
        good = s0 > 1e-30
        del win
        if not np.any(good):
            continue
        wg, sg = wt[good], s0[good]
        cx = (wg * gx[None]).sum(axis=(1, 2)) / sg
        cy = (wg * gy[None]).sum(axis=(1, 2)) / sg
        dx = gx[None] - cx[:, None, None]
        dy = gy[None] - cy[:, None, None]
        sxx = (wg * dx * dx).sum(axis=(1, 2)) / sg
        syy = (wg * dy * dy).sum(axis=(1, 2)) / sg
        sxy = (wg * dx * dy).sum(axis=(1, 2)) / sg
        tr = sxx + syy
        det = np.maximum(sxx * syy - sxy * sxy, 1e-30)
        disc = np.sqrt(np.maximum(tr * tr / 4.0 - det, 0.0))
        l1 = tr / 2.0 + disc
        l2 = np.maximum(tr / 2.0 - disc, 1e-12)
        idx = np.nonzero(good)[0] + s
        ba[idx] = np.sqrt(l2 / np.maximum(l1, 1e-30))
        qq[idx] = det ** 0.25
    return ba, qq


def bright_peaks(data: np.ndarray, noise: float, hp_sigma: float = HP_SIGMA,
                 threshold_sigma: float = 5.0, box_size: int = 5):
    """掩膜专用星表：高通图上的全部局部极大（不做锐度/孤立/饱和筛选）

    为什么掩膜不能沿用固定星表（build_star_list）：那张表是为"能量集中度"
               口径建的，刻意排除亮星（peak ≤ 0.40×最亮像元）并要求孤立——因为饱和星的
               平顶会把二阶矩 FWHM 测大。但掩膜要的恰恰相反：最亮的星 halo 最大、最需要
               被罩住。7331 实测 (11,7)(9,4) 两块假热斑，把检测到的星圆盘填掉后只剩
               0.8%/2.5% 的功率，说明残差就是亮星光晕，而它们没进掩膜。
    不用 DAOStarFinder：它的 sharpness 判据会整颗丢掉平顶饱和星（v3 实测
               两颗最亮星完全没被遮住）。纯局部极大（find_peaks，BSD-3）不挑形状。
    在 HP 图上取峰，与 fixed_star_peaks 同口径（滤掉延展光，峰值为点源响应），
               阈值压到 5σ 以尽量不漏；掩膜宁多勿少（多余部分由覆盖率闸门兜底）。
    """
    if float(noise) <= 0:
        return np.zeros(0), np.zeros(0), np.zeros(0)
    from photutils.detection import find_peaks as _find_peaks
    hp = data - ndimage.gaussian_filter(data, hp_sigma, mode='nearest')
    tbl = _find_peaks(hp, threshold=threshold_sigma * float(noise), box_size=box_size)
    del hp
    if tbl is None or len(tbl) == 0:
        return np.zeros(0), np.zeros(0), np.zeros(0)
    names = tbl.colnames
    xk = 'x_peak' if 'x_peak' in names else 'xpeak'
    yk = 'y_peak' if 'y_peak' in names else 'ypeak'
    vk = 'peak_value' if 'peak_value' in names else 'peak'
    return (np.asarray(tbl[xk], dtype=np.float64),
            np.asarray(tbl[yk], dtype=np.float64),
            np.asarray(tbl[vk], dtype=np.float64))


# ---------------------------------------------------------------------------
# 倍频程档的稳健幅度（本模块的核心度量）
# ---------------------------------------------------------------------------

def _bin_down(img: np.ndarray, d: int) -> np.ndarray:
    """按 d×d 做均值抽样（同时把噪声方差降到 1/d²）"""
    if d == 1:
        return img
    h, w = img.shape
    h2, w2 = (h // d) * d, (w // d) * d
    out = img[:h2, :w2].reshape(h2 // d, d, w2 // d, d).mean(axis=(1, 3))
    return out.astype(np.float32)


def band_images(data: np.ndarray, bg: float,
                fill_geo: Optional[List[Optional[Dict]]] = None,
                band_idx: Optional[List[int]] = None) -> List[np.ndarray]:
    """构造各倍频程档的图像（高斯差分），按档的抽样倍率降采样

    每档只保留"该尺度上的起伏"，因此模糊帧在细档上必然能量更少；
               低频（大尺度亮度与色彩）不在这些档里，天然不参与判据。
    fill_geo：逐档的填充几何（降采样坐标，见 _fill_geometry_for）。给的档
               先"掩膜 + 归一化均值填充"再带通——星点在带通域被真正移除，而不是靠
               统计扣减（后者实测对最亮星的大光晕系统性欠扣）。
    band_idx：只算这些档（判据带），返回列表也按该顺序；None = 全档。
               细档在全分辨率上做高斯滤波，是逐帧耗时的主要来源，只算判据带可省一大截。
    """
    work = data.astype(np.float32) - np.float32(bg)
    out = []
    for k in (range(len(BANDS)) if band_idx is None else band_idx):
        b = BANDS[k]
        d = b['decim']
        lo = _bin_down(work, d)
        geo = None if fill_geo is None else fill_geo[k]
        if geo is not None:
            bad = disc_mask(lo.shape, geo['xs'], geo['ys'], geo['radii'])
            lo = norm_fill(lo, bad, geo['win'])
            del bad
        ga = ndimage.gaussian_filter(lo, b['sigma_a'] / d, mode='nearest')
        gb = ndimage.gaussian_filter(lo, b['sigma_b'] / d, mode='nearest')
        out.append((ga - gb).astype(np.float32))
        del lo, ga, gb
    return out


def robust_band_power(band: np.ndarray, block: int, decim: int,
                      ny: int, nx: int, clip_mad: float = CLIP_MAD) -> np.ndarray:
    """逐块的"截尾方差"：星点等稀疏极值被剔除，延展结构完整保留

    实现要点：块的 MAD 用抽样估计（大幅提速），
               然后用该 MAD 对整块做截尾，最后算保留像素的均方值。
    """
    bs = block // decim
    h, w = band.shape
    h2, w2 = ny * bs, nx * bs
    if h2 > h or w2 > w:
        raise ValueError('分块网格超出图像范围')
    tiles = band[:h2, :w2].reshape(ny, bs, nx, bs).transpose(0, 2, 1, 3).reshape(ny * nx, -1)

    # 抽样估计每块的稳健尺度（避免对整块排序，提速数十倍）
    sub = tiles[:, ::SUBSAMPLE]
    med = np.median(sub, axis=1)
    spread = np.median(np.abs(sub - med[:, None]), axis=1) * 1.4826
    thresh = np.maximum(spread * clip_mad, 1e-30)[:, None]

    dev = np.abs(tiles - med[:, None])
    kept = dev <= thresh
    sq = np.where(kept, (tiles - med[:, None]) ** 2, 0.0)
    power = sq.sum(axis=1) / np.maximum(kept.sum(axis=1), 1)
    return power.reshape(ny, nx)


def structure_band_maps(data: np.ndarray, bg: float, noise: float, block: int,
                        ny: int, nx: int,
                        noise_vector: Optional[np.ndarray] = None,
                        fill_geo: Optional[List[Optional[Dict]]] = None,
                        band_idx: Optional[List[int]] = None
                        ) -> Tuple[np.ndarray, np.ndarray]:
    """一次性得到所有档的"真实结构功率"（已扣除噪声）

    返回 (band_power, band_noise)，形状均为 (ny, nx, 档数)
    band_power = 截尾方差 − 噪声方差；后者由高斯核解析增益严格给出：
               带通后的噪声方差 = 抽样后噪声方差 × 核增益
    noise_vector：直接给定逐档噪声底（形状 (档数,)，按 BANDS 全表）。用于中值
               去星之后——中值滤波后的噪声不是白噪声，解析增益不再成立（见其函数）。
    fill_geo：逐档的填充几何（按 BANDS 全表索引），走"掩膜 + 填充"路线的档传它
               （此时噪声仍近似白噪声，噪声底继续用解析增益）。
    band_idx：只算这些档（判据带），返回的最后一维按该顺序；None = 全档。
    """
    idxs = list(range(len(BANDS))) if band_idx is None else list(band_idx)
    bands = band_images(data, bg, fill_geo, idxs)
    power = np.zeros((ny, nx, len(idxs)), dtype=np.float64)
    bnoise = np.zeros(len(idxs), dtype=np.float64)
    for j, (k, img) in enumerate(zip(idxs, bands)):
        b = BANDS[k]
        d = b['decim']
        raw = robust_band_power(img, block, d, ny, nx)
        if noise_vector is None:
            nv = (noise ** 2) / (d ** 2) * b['noise_gain']  # 抽样后噪声方差 × 核增益
        else:
            nv = float(np.asarray(noise_vector)[k])
        bnoise[j] = nv
        power[..., j] = np.maximum(raw - nv, 0.0)
        del img
    del bands
    return power, bnoise


# ---------------------------------------------------------------------------
# 连续场版：像素级带通能量场（无块边界）
# ---------------------------------------------------------------------------

MEANAD_TO_SIGMA = 1.2533141373155003   # 均值绝对偏差 → σ（用 1.4826 是"中位绝对偏差"的系数，不可混用）


def band_field_win(center_px: float, decim: int = 1,
                   ratio: float = BLOCK_SCALE_RATIO) -> int:
    """连续场的稳健聚合窗（降采样网格上的像素数，奇数）

    硬约束来自 BLOCK_SCALE_RATIO：窗内必须容纳足够多的独立结构样本，
               否则判据退化成噪声图。这条约束同时决定了"分区能有多细"——
               有效分辨率由窗口决定，而不是由像素决定，所以永远不会落到逐像素噪声
               （这正是"分区越小越精确但不能小到一个像素"的落点）。
    753 的 80/113px 判据带 → 窗 320~452px；7331 的 10/20px → 窗 40~80px。
               粒度是**目标自适应**的，不是全局常数。
    `ratio` 参数化（默认仍是全局常数，分析层不动）：**权重场**单独可调。
               实测（temp 的 _tmp_star_footprint.py，空白天区孤立亮星）：窗宽 4× 尺度时
               （5px 带 21px、10px 带 41px），星的光把能量抬到闸门之上的范围达 r≈22/41px，
               与成品星周暗环宽度（最深 −6σ 于 r=5~6px、到 r≈15~20px 才归零）吻合——
               即"星周影响宽度"由**窗宽**决定，与 FWHM 之差（3.5px）无关。
               缩到 2× 尺度即把该足迹减半（5px 带 11px、10px 带 21px）。
               代价：窗内独立样本少 4 倍，能量估计更抖（闸门用同窗宽标定的底，口径自洽）。
    """
    w = int(round(max(float(ratio) * float(center_px), 7.0) / float(decim)))
    return max(w + (1 - w % 2), 3)


def _band_kept(data: np.ndarray, bg: float, band_idx: int, sigma: float,
               fill_geo: Optional[List[Optional[Dict]]] = None,
               clip_mad: float = CLIP_MAD) -> Tuple[np.ndarray, np.ndarray, float]:
    """带通图 + 解析阈值保留掩膜 + 该档噪声底（`band_field` 专用）

    截尾阈值用**解析噪声底**（`3σ_band`），不做局部尺度估计；这是
               `band_field`（旧口径）的算法。新口径 `trimmed_field` 改用窗内局部 MAD
               与同管线经验底，因为解析阈值在密集星场（753）上会把场硬顶在 9×底以下。
    `clip_mad ≤ 0` = 不截尾（保留掩膜全 True）。
    """
    b = BANDS[band_idx]
    d = int(b['decim'])
    x = band_images(data, bg, fill_geo, [band_idx])[0]
    noise_power = float(sigma ** 2 / (d * d) * b['noise_gain'])
    if clip_mad > 0:
        thr = np.float32(clip_mad * np.sqrt(max(noise_power, 1e-300)))
        keep = np.abs(x) <= thr
    else:
        keep = np.ones(x.shape, dtype=bool)
    return x, keep, noise_power


def band_field(data: np.ndarray, bg: float, band_idx: int, sigma: float,
               win_px: Optional[float] = None,
               fill_geo: Optional[List[Optional[Dict]]] = None,
               clip_mad: float = CLIP_MAD) -> Tuple[np.ndarray, float]:
    """像素级连续带通能量场：连续版的 robust_band_power

    与块级的唯一区别是"没有块"：聚合窗以每个像素为中心滑动，输出连续场。
               同一位置的能量读数不再有块边界台阶，也不再有"块心 vs 块角"的口径分歧。
    截尾阈值用**解析噪声底**（clip_mad × σ_band），不做局部尺度估计。
               教训（实测）：最初用"窗内均值绝对偏差"当尺度，星晕会把该尺度抬高一至两个
               量级，阈值随之放宽、星晕整片漏进场里——空白天区的场值被抬高到噪声底的
               **14.6 倍**，而块级 MAD 口径只有 2 倍。改回解析阈值后两者同口径。
    返回 (field, noise_power)：
               field 在**降采样网格**上（形状 = 数据/d，d 取该档的 decim）；
               noise_power = (σ²/d²)×核增益×截尾因子，即"保留像素上残留的噪声期望"，
               调用方直接 field − noise_power 即得结构功率（与 band_floor_sigma 同源）。
    **本函数为旧口径，新口径见 `trimmed_field`**（用户裁决）。保留只为
               阶段 1 探针对照与回退；主流程不再使用。
    **截尾上限**教训（阶段 2 实测）：截尾时 keep 只在 |x| ≤ 3σ_band 上为真，
               于是场值被硬顶在 (3σ)² = 9×noise_power 以下——全图 97.5% 像素的场值都贴着
               9×底，而星点基场跨好几个数量级，任何依赖该场动态范围的拟合都会塌到角落解
               （实测 level 中位冲到 62.5）。这同时说明：**用解析噪声底当截尾阈值，密集星场
               一定失效**（753 实测），故新口径改用窗内局部 MAD。
    """
    b = BANDS[band_idx]
    d = int(b['decim'])
    w = band_field_win(b['center_px'] if win_px is None else win_px, d)
    x, keep, noise_power = _band_kept(data, bg, band_idx, sigma, fill_geo, clip_mad)
    kf = keep.astype(np.float32)
    den = ndimage.uniform_filter(kf, size=w, mode='nearest')
    num = ndimage.uniform_filter(np.where(keep, x * x, np.float32(0.0)), size=w, mode='nearest')
    field = num / np.maximum(den, 1e-6)
    return field.astype(np.float32), noise_power * _trim_noise_factor(clip_mad)


def _trim_noise_factor(k: float) -> float:
    """高斯在 ±kσ 截尾后，保留像素的均方与全样本方差之比

    E[X² | |X| < kσ] = σ²·(1 − 2k·φ(k)/(2Φ(k)−1))；k=3 时 ≈ 0.9733。
               不补这一项，空白天区会被系统性抬到噪声底的 1.027 倍（小数，但方向明确）。
    """
    phi = np.exp(-0.5 * k * k) / np.sqrt(2.0 * np.pi)
    cdf = 0.5 * (1.0 + math.erf(k / np.sqrt(2.0)))
    return float(max(1.0 - 2.0 * k * phi / max(2.0 * cdf - 1.0, 1e-12), 1e-6))


def field_upsample(field: np.ndarray, shape: Tuple[int, int], decim: int) -> np.ndarray:
    """降采样网格上的连续场 → 全分辨率（双线性）

    场本身是"窗口平滑过"的低频量，双线性升采样不引入信息损失；
               各档因此可以放到同一张全分辨率网格上相除（level = E / S）。
    """
    d = int(decim)
    gy = (np.arange(int(shape[0]), dtype=np.float32) + 0.5) / d - 0.5
    gx = (np.arange(int(shape[1]), dtype=np.float32) + 0.5) / d - 0.5
    gy = np.clip(gy, 0.0, field.shape[0] - 1.0)
    gx = np.clip(gx, 0.0, field.shape[1] - 1.0)
    gy, gx = np.meshgrid(gy, gx, indexing='ij')
    out = ndimage.map_coordinates(np.asarray(field, dtype=np.float32),
                                  (gy, gx), order=1, mode='nearest')
    return np.ascontiguousarray(out, dtype=np.float32)


# ---------------------------------------------------------------------------
# 连续场版：去星 + 窗内局部稳健尺度截尾能量场（E 的新口径）
# 用户裁决：改回局部 MAD 截尾、**去掉解析星点模型 S(x)**。
#            实测依据：753（80/113px 档）r(亮度, level) 由 +0.159（不截尾 + S 模型）
#            升到 +0.696（本口径；v6 原始工程自证 +0.78）。
# ---------------------------------------------------------------------------

def coarse_robust_scale(x: np.ndarray, w: int,
                        coarse: int = 4) -> Tuple[np.ndarray, np.ndarray]:
    """粗网格上的稳健尺度场（窗内中值 + 1.4826×MAD），升采样回原网格

    为什么不在原网格直接做：窗 w 在细档可达 41，147k 像素 × 41² 的
               median_filter 要几十秒到几分钟。稳健尺度本身是空间缓变量，粗网格足够
               （粗网格窗内样本更多、估计更稳），升采样不引入结构偏差。
    1.4826 是"中位绝对偏差 → σ"的系数，与 MEANAD_TO_SIGMA（均值绝对偏差的
               系数）不是一回事，不可混用。
    """
    c = max(1, int(coarse))
    if c > 1:
        h, ww = x.shape
        h2, w2 = (h // c) * c, (ww // c) * c
        xc = x[:h2, :w2].reshape(h2 // c, c, w2 // c, c).mean(axis=(1, 3)).astype(np.float32)
    else:
        xc = np.asarray(x, dtype=np.float32)
    wc = max(3, (int(w) // c) | 1)
    med = ndimage.median_filter(xc, size=wc, mode='nearest')
    spread = ndimage.median_filter(np.abs(xc - med), size=wc, mode='nearest') * np.float32(1.4826)
    if c > 1:
        # **必须补齐回原尺寸**：原写法把下采样网格按 c 放大后直接切片回 x.shape，
        #   一旦某一维不是 c 的整数倍（实测：全幅 6388×9576 的 decim=2 带 → 3194×4788，
        #   3194 % 4 = 2），放大后的数组比输入小 → `np.abs(x - med)` 广播失败（ValueError）。
        #   补边用 edge 模式（稳健尺度是空间缓变量，边缘补值不影响主体读数）；
        #   尺寸本来就是整数倍时，pad 宽度为 0 → 与旧口径逐位一致。
        def _grow(a):
            a = np.repeat(np.repeat(a, c, axis=0), c, axis=1)
            ph = max(0, x.shape[0] - a.shape[0])
            pw = max(0, x.shape[1] - a.shape[1])
            if ph or pw:
                a = np.pad(a, ((0, ph), (0, pw)), mode='edge')
            return a[:x.shape[0], :x.shape[1]]
        med = _grow(med)
        spread = _grow(spread)
    return med, spread


def trimmed_field(data: np.ndarray, bg: float, band_idx: int,
                  coarse: int = 4, clip_mad: float = CLIP_MAD,
                  fill_geo: Optional[List[Optional[Dict]]] = None,
                  win_ratio: float = BLOCK_SCALE_RATIO
                  ) -> Tuple[np.ndarray, np.ndarray]:
    """连续版"窗内局部稳健尺度截尾"能量场（E 的新口径，v6 已验证）→ (E, x_trim)

    与 `band_field` 的差别只有截尾阈值一处：不用解析噪声底，而用**窗内局部
               MAD**。这一处决定了密集星场能不能看见延展结构：
               - 解析阈值 3σ_band 对 753 太小 → keep 只在 |x| ≤ 3σ_band 上为真，场被硬顶在
                 9×noise_power 以下、判据塌成噪声；
               - 局部 MAD 跟着窗内内容走 → 稀疏的星点极值被截掉，延展结构（落在分布中段）
                 完整保留。
    为什么不需要星点模型：局部 MAD 截尾天然只剔稀疏极值，星点正是稀疏极值；
               延展结构遍布整窗、完整保留。于是"星点被拉平成 1、与疏密无关"自动成立，
               无需星点掩膜、无需填充、无边缘伪影。
    **数据必须已去星**（`star_removed`）：去星把窄于窗的星核整体换成周围水平，
               带通再在去星后的图上做，星点在该档的响应随之大幅削弱；局部 MAD 截尾负责
               收掉残余极值。两步缺一，密集星场（753）都会失效。
    第二个返回值 `x_trim = (x − med)·keep`（被截掉的像素置 0）：凡需要"星点
               幅度"的下游口径都必须在这张图上量，否则与 E 的截尾口径不一致。
    """
    b = BANDS[band_idx]
    d = int(b['decim'])
    w = band_field_win(b['center_px'], d, ratio=win_ratio)
    x = band_images(data, bg, fill_geo, [band_idx])[0]
    med, spread = coarse_robust_scale(x, w, coarse)
    thr = np.maximum(spread * np.float32(clip_mad), np.float32(1e-30))
    keep = np.abs(x - med) <= thr
    dev2 = np.where(keep, (x - med) ** 2, np.float32(0.0))
    den = ndimage.uniform_filter(keep.astype(np.float32), size=w, mode='nearest')
    num = ndimage.uniform_filter(dev2, size=w, mode='nearest')
    field = (num / np.maximum(den, 1e-6)).astype(np.float32)
    return field, ((x - med) * keep).astype(np.float32)


def trimmed_floor_unit(band_idx: int, med_px: int = 0, coarse: int = 4,
                       clip_mad: float = CLIP_MAD, n: int = 2048,
                       seed: int = 20260927,
                       win_ratio: float = BLOCK_SCALE_RATIO) -> float:
    """本口径的噪声底（单位 σ²，**均值**口径）：让白噪声走一遍同一条管线

    为什么不能用解析增益：去星后的噪声既非白也非高斯（是窗尺度的斑块纹理），
               解析式不再成立（v3 实测与解析预测差 20~350 倍）。各步对幅度齐次，故让单位 σ
               的白噪声走一遍完整管线（去星 → 带通 → 局部 MAD 截尾），量到的读数乘 σ²
               即该帧该档的底。
    取**均值**而非中位：场在纯噪声下右偏（χ² 型），物理上要扣掉的是"噪声对能量
               的平均贡献"，用均值相减残差才零均值、level 才以 1 为中心。
    med_px 必须与逐帧去星用的窗完全一致（窗不同，底差一个量级）。
    win_ratio 必须与逐帧/参考像用的窗完全一致（同上：窗不同，底就不是同一口径）。
    """
    rng = np.random.default_rng(seed)
    img = rng.normal(0.0, 1.0, size=(int(n), int(n))).astype(np.float32)
    img = star_removed(img, med_px)
    f, _ = trimmed_field(img, 0.0, band_idx, coarse=coarse, clip_mad=clip_mad,
                         win_ratio=win_ratio)
    return float(np.mean(f))



def _fit_star_model(power: np.ndarray, star_reg: np.ndarray,
                    max_pts: int = 400) -> float:
    """稳健直线拟合 power ≈ a·u（过原点；u 为分块星点强度，见 star_power_map）

    自变量必须用"星点强度"（Σ 峰值²）而不是"星点数"。
               实测 7331：用星点数拟合时，一颗亮星（峰值是普通星的十几倍）
               会让该块功率超出线性预测上百倍，残差里就留下一个孤立的亮点，
               温度图上表现为散落在星场里的孤立亮块（星点密度 vs need r=+0.44）。
               方差对点源是二次量：power ∝ Σ A_i²，所以 Σ 峰值² 才是正确的自变量。

    必须用稳健斜率（Theil–Sen，取所有点对斜率的中位数），不能用最小二乘。
               7331 实测教训：一个亮星块（功率是天空中位数的 600 倍）就能把最小二乘
               斜率抬到 2.2e-9/颗，于是普通天空块也被预测成 2.9e-9 —— 扣除后过半块
               为负、被截断成 0，比例信息被抹平，八帧的均温全部恒等于 1.000、尺度恒等于 10px。
               点对斜率取中位数时，这种个别极值只影响极少数点对，不会带偏整体。

    必须过原点（不保留截距）。此前取 b = median(raw − a·u)，等于把
               "全图典型结构功率"从每一块里都扣掉，中位块必然残差为 0，于是闸门
               pred < 0.5·raw 在数学上几乎不可能通过：753 实测 b 本身就超过中位 raw
               的一半（10px 档 b=7.3e-11，而 0.5×中位 raw=3.9e-11），216 块里 0 块通过，
               整幅温度图全灭；7331 也同理只过 27/144。星点解释量必须随星点强度趋于 0。
    """
    p = np.asarray(power, dtype=np.float64).ravel()
    n = np.asarray(star_reg, dtype=np.float64).ravel()
    ok = np.isfinite(p) & np.isfinite(n)
    if ok.sum() <= 20 or np.std(n[ok]) <= 0:
        return 0.0
    ps, ns = p[ok], n[ok]
    if ps.size > max_pts:  # 大面积数据集的点数上限，避免 O(n²) 过大
        sel = np.linspace(0, ps.size - 1, max_pts).astype(int)
        ps, ns = ps[sel], ns[sel]
    dn = ns[None, :] - ns[:, None]
    dp = ps[None, :] - ps[:, None]
    pair = np.abs(dn) > 0
    if np.count_nonzero(pair) < 20:
        return 0.0
    return max(float(np.median(dp[pair] / dn[pair])), 0.0)  # 斜率截断在 ≥0


def star_prediction(band_power: np.ndarray, star_reg: np.ndarray) -> np.ndarray:
    """逐档给出"该块功率中能由星点解释的部分"

    形状：(y,x,档) 或 (帧,y,x,档)，星点强度广播到前几维。
               多帧必须逐帧各自拟合：各帧透明度不同（753 实测帧间功率差可达数倍），
               共用一条基线会把较暗的帧整幅减成负数、截断为 0，判出"全图无细节"（实测踩过）。
    """
    arr = np.asarray(band_power, dtype=np.float64)
    sc = np.broadcast_to(np.asarray(star_reg, dtype=np.float64), arr.shape[:-1])
    if arr.ndim > 3:
        return np.stack([star_prediction(arr[i], sc[i]) for i in range(arr.shape[0])])
    out = np.empty_like(arr)
    for k in range(arr.shape[-1]):
        out[..., k] = np.maximum(_fit_star_model(arr[..., k], sc) * sc, 0.0)
    return out


def star_excess(band_power: np.ndarray, star_reg: np.ndarray) -> np.ndarray:
    """逐档扣掉"能由星点强度解释的功率"，返回残差（已截断为 ≥0）

    753 这类密集星场：星晕环在一些尺度上填满整块，使该档功率
               随星点同步起伏（实测 10px r=+0.35）。残差才是可能来自延展结构的部分。
    """
    arr = np.asarray(band_power, dtype=np.float64)
    return np.maximum(arr - star_prediction(arr, star_reg), 0.0)


def star_power_map(xs: np.ndarray, ys: np.ndarray, peaks: np.ndarray,
                   block: int, ny: int, nx: int) -> np.ndarray:
    """分块星点强度图：Σ 峰值²（点源对"方差"的贡献是二次量）

    用 Σ 峰值² 而不是星点数，才能正确表达"一颗亮星顶几十颗暗星"。
    """
    m = np.zeros((ny, nx), dtype=np.float64)
    if len(xs) == 0:
        return m
    ix = np.clip((np.asarray(xs) / block).astype(int), 0, nx - 1)
    iy = np.clip((np.asarray(ys) / block).astype(int), 0, ny - 1)
    np.add.at(m, (iy, ix), np.square(np.maximum(np.asarray(peaks, dtype=np.float64), 0.0)))
    return m


# ---------------------------------------------------------------------------
# 细节需求合成
# ---------------------------------------------------------------------------

def relative_spectrum(band_power: np.ndarray, band_noise: np.ndarray,
                      allowed: np.ndarray, snr_min: float = 3.0,
                      star_pred: Optional[np.ndarray] = None,
                      star_frac_max: float = STAR_FRAC_MAX, log=None,
                      med_fixed: Optional[np.ndarray] = None):
    """把各档功率换算成无量纲的相对谱型（判据的唯一入口，单帧/分组共用）

    关键：各档绝对功率天然相差极大（低频远高于高频），
               必须先把每档除以"该档在本图中的典型水平"，谱型才可比。
               这个归一化对单帧和帧组都一样适用，因此逐帧温度图与总体需求图口径一致。

    两道闸门（都作用在原始功率上，残差只用于归一化与比例）：
               1) 噪声闸门 raw > snr_min × 噪声方差：排除纯读出/天光噪声。
                  必须用原始功率：残差数值上必然更小，拿残差比噪声底会把目标误杀
                  （7331 实测 10px 档整档消失，每帧 need 恒为 0、尺度恒为 20px）。
               2) 星点闸门 pred < star_frac_max × raw：这块的功率必须有
                  (1 − star_frac_max) 以上无法用星点数解释，读数才算数。
                  这是"星点不参与细节判定"的落实方式。

    归一化基准 med 只用"通过双闸门"的块，因此 q 的量纲是"相对本图典型结构"。
               扣掉星点解释量后 q 才不含星晕环，比例 need 才有意义。

    med_fixed：给定"目标基准"（该目标最锐子集的残差中位）时不再自算 med。
               这是"相同目标共用一套等级体系"的落实方式：若每个分组/每帧各自归一化，
               两组相减求损失率时口径不同（损失率被系统性低估），逐帧温度也被抹平。
    """
    nb = band_power.shape[-1]
    raw = np.asarray(band_power, dtype=np.float64)
    valid = np.isfinite(raw).all(axis=2)
    if star_pred is None:
        pred = np.zeros_like(raw)
    else:
        pred = np.broadcast_to(np.asarray(star_pred, dtype=np.float64), raw.shape)
        valid = valid & np.isfinite(pred).all(axis=2)
    excess = np.maximum(raw - pred, 0.0)

    with np.errstate(invalid='ignore'):
        noise_ok = (raw > snr_min * band_noise) & valid[..., None] & allowed
        star_ok = (pred < star_frac_max * raw) & valid[..., None] & allowed
    band_ok = noise_ok & star_ok

    if med_fixed is None:
        med = np.full(nb, np.nan, dtype=np.float64)   # 归一化基准：只用通过双闸门的块
        for k in range(nb):
            if not allowed[k]:
                continue
            sel = band_ok[..., k] & np.isfinite(excess[..., k])
            if np.count_nonzero(sel) >= 10:
                med[k] = float(np.median(excess[..., k][sel]))
    else:
        # 用目标基准：形状对齐到 (档,)，缺失档仍为 NaN（判为不可用）
        med = np.broadcast_to(np.asarray(med_fixed, dtype=np.float64),
                              (nb,)).astype(np.float64)

    usable = np.isfinite(med) & (med > 0)
    trust = band_ok & usable[None, None, :]
    med_use = np.where(usable, med, 1.0)
    q = np.where(trust, excess / med_use[None, None, :], 0.0)

    if log is not None:
        log('  [闸门] ' + ' | '.join(
            f'{BANDS[k]["center_px"]:.0f}px 噪声{int(np.count_nonzero(noise_ok[..., k]))}'
            f'/星点{int(np.count_nonzero(star_ok[..., k]))}'
            f'/双{int(np.count_nonzero(band_ok[..., k]))}块 基准{med[k]:.2e}'
            for k in np.where(allowed)[0]))
    return q, band_ok, med_use, trust


def _need_over_trusted(q: np.ndarray, trust: np.ndarray, center_px: np.ndarray,
                       allowed: Optional[np.ndarray] = None, min_bands: int = 2):
    """只用"该块自己可信的档"算精细成分占比与细节承载尺度

    精细/粗放的分界逐块取"可信档里偏细的那一半"（不用固定像素阈值：
               可信档范围随块大小与闸门结果变化，固定阈值在档数少时会退化成恒等于 1）。

    不能让被闸门剔除的档以 0 参与比例，否则两端都会出现假的极端值：
               7331 亮星块粗档被剔 → need = q细/(q细+0) 饱和为 1（温度图上落在亮星上的红块）；
               753 细档被剔 → need = 0/(0+q粗) 恒为 0（整幅温度图全灭）。
               因此比例只在可信档上算。

    测不到不再留空（NaN），改为连续值记录：need 记 0，
               细节承载尺度记"最粗的允许档中心"——含义是"此处若还有结构，也只有大尺度的"。
               留空会让最糊的帧整幅消失（7331 实测 4 帧全空），同目标下所有帧就失去了
               一套可比的刻度；而且"测不到"与"只有粗结构"在数值上也确实应当同向。
    """
    t = np.asarray(trust, dtype=bool) & np.isfinite(q)
    n = t.sum(axis=2)
    tot = np.where(t, q, 0.0).sum(axis=2)
    pick = np.maximum((n - 1) // 2, 0)                      # 偏细一半的分界序号
    rank = np.cumsum(t, axis=2) - 1                         # 该档在"可信档"中的序号
    cut = np.where(t & (rank == pick[..., None]),
                   center_px[None, None, :], -np.inf).max(axis=2)
    fine = t & (center_px[None, None, :] <= cut[..., None])
    ok = (n >= min_bands) & (tot > 0)
    safe = np.maximum(tot, 1e-30)
    need = np.clip(np.where(fine, q, 0.0).sum(axis=2) / safe, 0.0, 1.0)
    scale = (np.where(t, q, 0.0) * center_px[None, None, :]).sum(axis=2) / safe
    a = (np.ones(center_px.shape, dtype=bool) if allowed is None
         else np.asarray(allowed, dtype=bool))          # 允许档（定"最粗"用）
    coarse_px = float(np.max(center_px[a])) if a.any() else float(np.max(center_px))
    return np.where(ok, need, 0.0), np.where(ok, scale, coarse_px), ok


def frame_detail(band_power: np.ndarray, band_noise: np.ndarray, allowed: np.ndarray,
                 center_px: np.ndarray, snr_min: float = 3.0,
                 star_pred: Optional[np.ndarray] = None,
                 star_frac_max: float = STAR_FRAC_MAX,
                 med_fixed: Optional[np.ndarray] = None) -> Dict[str, np.ndarray]:
    """单帧的"细节温度图"：哪里细节高、哪里细节低

    need         : 0~1 细节温度。精细成分占比，越高说明该区越精细
    detail_scale_px : 细节承载尺度（px），越小越精细
    has_detail   : 该区块是否存在可靠细节

    band_power 传该帧的原始功率，star_pred 传该帧自己的星点解释量
               （逐帧拟合，见 star_prediction）。两道闸门与残差都在 relative_spectrum 里完成；
               比例在"该块可信的档"上算，见 _need_over_trusted。
    med_fixed 传该目标的目标基准（最锐子集的残差中位）。默认 None 时自算，
               但主流程必须传：逐帧自归一化会把帧间等级差抹平（同目标必须共用一套刻度）。
    """
    q, band_ok, _, trust = relative_spectrum(band_power, band_noise, allowed, snr_min,
                                             star_pred, star_frac_max,
                                             med_fixed=med_fixed)
    need, scale, has = _need_over_trusted(q, trust, center_px, allowed=allowed)
    return {
        'need': need,
        'detail_scale_px': scale,
        'has_detail': has,
        'band_ok': band_ok,
        'band_trust': trust,
    }


def detail_demand(band_sharp: np.ndarray, band_all: np.ndarray,
                  band_noise_sharp: np.ndarray, band_noise_all: np.ndarray,
                  allowed: np.ndarray, center_px: np.ndarray,
                  snr_min: float = 3.0,
                  pred_sharp: Optional[np.ndarray] = None,
                  pred_all: Optional[np.ndarray] = None,
                  star_frac_max: float = STAR_FRAC_MAX, log=None) -> Dict[str, np.ndarray]:
    """合成"延展结构细节需求图"（锐利子集 vs 全量帧）

    band_noise 必须按组分别传入：曝光归一化后各帧噪声并不相同
               （长曝光在等效尺度上噪声更低），用同一个噪声基准会让高噪声帧
               伪通过"信号>3×噪声"的闸门，实测导致最糊的帧温度反而最高。

    demand_raw      : 模糊帧造成的结构损失率
    need            : 精细成分占比（"细节高低"，取锐利子集）
    demand          : need × demand_raw，即"必须挑帧的程度"
    detail_scale_px : 细节承载尺度
    传原始功率与各自的星点解释量（pred_*），残差与闸门在 relative_spectrum 内完成。
    """
    q_sharp, ok_sharp, med, trust_sharp = relative_spectrum(
        band_sharp, band_noise_sharp, allowed, snr_min, pred_sharp, star_frac_max, log=log)
    # 全量帧组必须复用锐利子集算出的基准：两组各自归一化再相减，
    # 等于拿两把不同的尺子量长度，损失率会被系统性低估。
    q_all, _, _, trust_all = relative_spectrum(
        band_all, band_noise_all, allowed, snr_min, pred_all, star_frac_max,
        med_fixed=med)

    # 比例只在"该块自己可信的档"上算（见 _need_over_trusted）
    need, detail_scale, has = _need_over_trusted(q_sharp, trust_sharp, center_px,
                                                 allowed=allowed)
    qs_tot = np.where(trust_sharp, q_sharp, 0.0).sum(axis=2)
    qa_tot = np.where(trust_all, q_all, 0.0).sum(axis=2)
    demand_raw = np.clip((qs_tot - qa_tot) / np.maximum(qs_tot, 1e-30), 0.0, 1.0)

    keep = lambda a: np.where(has, a, np.nan)  # noqa: E731
    return {
        'demand': keep(demand_raw * need),
        'demand_raw': keep(demand_raw),
        'need': need,
        'fine_ratio': need,
        'detail_scale_px': detail_scale,
        'lost': keep(np.maximum(qs_tot - qa_tot, 0.0)),
        'has_detail': has,
        'band_ok': ok_sharp,
        'band_trust': trust_sharp,
        'band_median': med,
    }


# ---------------------------------------------------------------------------
# 尺度自适应：剔除被星场主导的档
# ---------------------------------------------------------------------------

def band_star_correlation(band_frame: np.ndarray, star_reg: np.ndarray,
                          allowed: np.ndarray) -> np.ndarray:
    """逐档计算"分块功率 vs 分块星点强度"的相关系数

    密集星场里星晕环会在某些尺度上填满整块，该档功率就会随星点强度
               同步起伏；真实云气结构与星点强度无关，相关系数应接近 0。
    """
    nb = band_frame.shape[-1]
    r = np.full(nb, np.nan, dtype=np.float64)
    sc = np.asarray(star_reg, dtype=np.float64)
    for k in range(nb):
        if not allowed[k]:
            continue
        a = np.asarray(band_frame[..., k], dtype=np.float64)
        ok = np.isfinite(a) & np.isfinite(sc)
        if ok.sum() <= 10 or np.std(sc[ok]) <= 0 or np.std(a[ok]) <= 0:
            continue
        r[k] = float(np.corrcoef(sc[ok], a[ok])[0, 1])
    return r


def band_axis_correlation(band_frame: np.ndarray, allowed: np.ndarray) -> List[List[float]]:
    """逐档算"与该块所在行列位置"的相关系数，用来识别残差里的大尺度梯度

    暗角、透明度、天光噪声都会在功率图上留下平滑梯度，它们不是细节。
               扣掉星点解释量之后若 |r| 仍大（经验上 >0.5），说明梯度还在，
               需要进一步压制（例如按局部天光重算噪声底）。
    """
    ny, nx = band_frame.shape[:2]
    iy, ix = np.mgrid[0:ny, 0:nx]
    out = []
    for k in np.where(allowed)[0]:
        a = np.asarray(band_frame[..., k], dtype=np.float64).ravel()
        ok = np.isfinite(a)
        row = []
        for axis in (iy.ravel().astype(np.float64), ix.ravel().astype(np.float64)):
            if ok.sum() > 10 and np.std(a[ok]) > 0 and np.std(axis[ok]) > 0:
                row.append(float(np.corrcoef(axis[ok], a[ok])[0, 1]))
            else:
                row.append(float('nan'))
        out.append(row)
    return out


def adapt_allowed_bands(band_frame: np.ndarray, star_reg: np.ndarray,
                        allowed: np.ndarray, r_max: float = STAR_R_MAX,
                        log=print) -> Tuple[np.ndarray, np.ndarray]:
    """尺度自适应：从参与判据的档里剔除"量到的是星场"的那些档

    判据只应建立在与星点强度无关的档上。753 实测：block=128 时
               全部档都被星晕环填满（功率比 1024 块读数高 350 倍），温度图退化成斑点。
               若所有档都被剔除，说明当前块边长下判据不可用，必须明确报错，
               而不是给出一个看起来很合理、实则无意义的温度图。
    """
    r = band_star_correlation(band_frame, star_reg, allowed)
    drop = allowed & np.isfinite(r) & (np.abs(r) > r_max)
    keep = allowed & ~drop
    log('[自适应] 逐档与分块星点强度的相关性：' + ' | '.join(
        f'{BANDS[k]["center_px"]:.0f}px r={r[k]:+.2f}{"→剔除" if drop[k] else ""}'
        for k in np.where(allowed)[0]))
    if not keep.any():
        raise RuntimeError(
            '所有档都与星点强度强相关（|r| > %.2f），当前块边长下判据被星场主导，'
            '无法给出可靠的细节温度图。请增大块边长后重试' % r_max)
    return keep, r


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def star_removed(data: np.ndarray, med_px: int = EXT_MED_PX) -> np.ndarray:
    """抹掉点源、只留延展结构（中值窗 ≈ 2×PSF 核心）

    星点核心只有 3.5px，在 7×7 窗里只占少数像素，中值天然把它丢掉；
               云气结构比窗宽，中值几乎不动它。这与"星点掩膜 + 填充"不同：
               没有掩膜、没有填充、不产生边缘假高频，也不像 StarNet2 那样伪造内容。
    med_px 随所判尺度自适应（见 derived_med）：753（80/113px 档）用 15px，
               7331（10/20px 档）用 7px。窗若不适配，要么盖不住点源（细档被星点污染），
               要么把要判的结构自己抹掉（粗档）。
    """
    med_px = int(med_px)
    if med_px < 3:
        return data
    if med_px % 2 == 0:
        med_px += 1
    return ndimage.median_filter(data, size=med_px, mode='nearest')


def median_noise_floor_unit(block: int, med_px: int = EXT_MED_PX,
                            seed: int = 20260926) -> np.ndarray:
    """星点移除口径下各档的噪声底（单位 σ²，形状 (档数,)，按 BANDS 全表）

    为什么不沿用解析增益：_bin_down + 高斯带通对白噪声可解析计算，
               但中值滤波后的噪声既非白、也非高斯（是 ~窗宽尺度的斑块纹理），
               解析式不再成立（实测同 σ 下与解析预测差 20~350 倍）。
               最可靠的做法是让真实管线去量一遍合成白噪声：同一块几何、同一估计器、
               同一套档，量出来的就是"纯噪声在这个口径下的读数"。
    med_px 必须与逐帧去星用的窗完全一致（窗不同，噪声底差一个量级）。
    返回单位 σ² 的底；逐帧按 (σ_帧 × 曝光归一)² 缩放即可（各步都是齐次的）。
    """
    n = max(4, FLOOR_PATCH // block) * block
    rng = np.random.default_rng(seed)
    patch = rng.normal(0.0, 1.0, size=(n, n)).astype(np.float32)
    patch = star_removed(patch, med_px)
    p, _ = structure_band_maps(patch, 0.0, 1.0, block, n // block, n // block,
                               noise_vector=np.zeros(len(BANDS)))
    return p.reshape(-1, len(BANDS)).mean(axis=0)


def locate_extended_source(data: np.ndarray, bin_px: int = 32) -> Tuple[int, int]:
    """定位画面中最亮的延展天体（星系/星云），返回像素坐标 (y, x)

    方法：先按 bin_px 分块取中值。中值天然滤掉星点（星点只占块内极少数像素），
               只留下占据整块的延展流量，因此峰值必然落在星系或星云上。
               实测用"最粗频档峰值"定位会被亮星晕带偏，故不用频域法。
    """
    h, w = data.shape
    ny, nx = h // bin_px, w // bin_px
    if ny < 3 or nx < 3:
        return h // 2, w // 2
    t = (data[:ny * bin_px, :nx * bin_px]
         .reshape(ny, bin_px, nx, bin_px)
         .transpose(0, 2, 1, 3)
         .reshape(ny, nx, -1))
    med = np.median(t, axis=2)
    del t
    smooth = ndimage.gaussian_filter(med, 3.0, mode='nearest')
    smooth -= np.median(smooth)
    j, i = np.unravel_index(int(np.argmax(smooth)), smooth.shape)
    return int((j + 0.5) * bin_px), int((i + 0.5) * bin_px)


# ---------------------------------------------------------------------------
# v4 口径：块边长 / 判据带 / 去星窗全部由目标自身数据派生
# ---------------------------------------------------------------------------
# 为什么必须派生而不是写死：753（暗弱云气）的结构在 80/113px 档上显著、
#            7331（星系）在 10/20px 档上显著；同一目标的不同裁剪下，块边长也必须随
#            目标尺寸走，否则"有结构/没结构"的对比被稀释。以下四条规则在
#            5 组验收扫描（753@3072/4096、7331@1536/2048/4096）上全部复现真值配置。

def scan_block(crop: np.ndarray, log=print) -> int:
    """扫描块边长：由目标"最亮延展团"的尺寸派生（不写死单一数值）

    块必须装得下最亮延展团：团比块大时整块被团填满、块间失去"有结构/没结构"
               的对比（实测 7331 在 512px 块下 10px 档 ρ 只剩 +0.05）；块也不能远大于团，
               否则天空块占绝大多数、ρ 被稀释（实测 7331@4096 全块 ρ 只剩 +0.19）。故取
               "装得下团"的最小 2 的幂：半高区最大连通域的包围盒长边 → 向上取 2 的幂。
               实测：7331 团 80×64px → 128px 块（1536/2048/4096 三裁剪一致）；
               753 团 320×240px → 512px 块（3072/4096 一致，此块下 80px 档 ρ=+0.80/+0.81）。
    团在工作域中央的一半内量：全裁剪会把远处亮源当成目标——753 的全局最亮源
               离目标 (+1464,+960)px，按它定尺寸必错（旧口径实测失败）。
    中值必须在全裁剪上做、再切出中央一半（探针同口径）：补丁若自己算中值，
               亮源落在补丁角上时 'nearest' 填充会把角格复制 25 次（5×5），9×9 中值压不住
               它、假"峰"把半高阈顶高，真团被压到阈下只剩尖峰 1 格（实测 753@3072：
               补丁角上正好有亮源，得 0 格；全裁剪口径同一补丁得 527 格）。
    上下限：≥128px（档表最细档对 10/20px 需 4×20=80）；≤min(512px, 工作域/4)
               （512=4×最粗档 113；工作域/4 保证 ≥4×4 个块，秩相关可用）。
    """
    neb = ndimage.median_filter(_bin_down(crop, 8), size=9, mode='nearest')
    hc = min(neb.shape) // 2                # 中央一半（格），= 工作域/2
    py0 = (neb.shape[0] - hc) // 2
    px0 = (neb.shape[1] - hc) // 2
    p = neb[py0:py0 + hc, px0:px0 + hc]
    peak, bg = float(p.max()), float(np.median(p))
    m = p > bg + 0.5 * (peak - bg)
    lab, nlab = ndimage.label(m)
    cells, edge = 0, 0
    if nlab:
        counts = np.bincount(lab.ravel())
        counts[0] = 0
        k = int(np.argmax(counts))
        if counts[k] >= 3:                  # 零散小团（亮星晕/噪声）不定尺寸
            ys, xs = np.where(lab == k)
            cells = int(counts[k])
            edge = (max(int(ys.max() - ys.min()), int(xs.max() - xs.min())) + 1) * 8
    blk = 1
    while blk < edge:
        blk *= 2
    blk = max(blk, 128)                     # ≥4×20px（档表最细档对）
    cap = 1
    while cap * 2 <= min(crop.shape) // 4:
        cap *= 2
    blk = min(blk, 512, cap)
    log(f'最亮延展团：半高区 {cells} 格（8px），包围盒长边 {edge}px → '
        f'扫描块 {blk}px（工作域 {min(crop.shape)}px）')
    return blk


def band_envelope(crop: np.ndarray, window_px: float, f: int = 8) -> np.ndarray:
    """目标包络：去星图上再做"远大于所判尺度"的重窗中值，只留目标的延展流量

    中值对稀疏点源（含亮星光晕）稳健：星点被窗吃掉，云气/星系盘留下。
               窗取最粗扫描档——比任何参与比较的档都粗，包络才不含"被比较档"自己的内容。
               先降采样再中值（全分辨率 113px 窗太慢）。
    """
    wh = max(3, int(round(window_px / f))) | 1
    return ndimage.median_filter(_bin_down(crop, f), size=wh, mode='nearest')


def envelope_tiles(env_s: np.ndarray, f: int, block: int, ny: int, nx: int) -> np.ndarray:
    """把包络折成该档的格网格（格均值）"""
    fb = max(1, block // f)
    return env_s[:ny * fb, :nx * fb].reshape(ny, fb, nx, fb).mean(axis=(1, 3)).astype(np.float64)


def spearman(a: np.ndarray, b: np.ndarray) -> float:
    """秩相关：只看"谁排前谁排后"，对极少数极值不敏感（块级判据的稳健口径）"""
    from scipy.stats import rankdata
    x = np.asarray(a, dtype=np.float64).ravel()
    y = np.asarray(b, dtype=np.float64).ravel()
    ok = np.isfinite(x) & np.isfinite(y)
    if ok.sum() <= 10 or np.std(x[ok]) <= 0 or np.std(y[ok]) <= 0:
        return float('nan')
    return float(np.corrcoef(rankdata(x[ok]), rankdata(y[ok]))[0, 1])


def band_allowed_centers(block: int) -> np.ndarray:
    """该块边长下允许参与"延展结构"判据的档（4×档 ≤ 块，且 ≥STRUCTURE_MIN_PX）"""
    return np.array([STRUCTURE_MIN_PX <= b['center_px'] <= block / BLOCK_SCALE_RATIO
                     for b in BANDS])


def scan_table(crop: np.ndarray, block: int, log=print):
    """逐档度量并打印选带表（--auto 选带的依据）

    全部档共用同一个块（block，由 scan_block 按目标紧凑度派生）；去星窗仍
               逐档自适应（derived_med([该档])）——窗必须远小于所判尺度，否则会把要判的
               结构自己抹掉。
    为什么块必须共用：ρ 在不同块边长下不可比。块大 → 空间平均多、亮星残差
               被摊薄，ρ 机械性偏高（实测 7331@2048：512px 块的 113px 档 +0.43 压过
               64px 块的 10px 档 +0.24）；块小 → 单个亮星残差块就能挤进目标块的秩位
               （实测亮星块 SG/N 达 10^3.2），ρ 被稀释。共用块后各档抽样口径一致。
    判据列：vs包络ρ = 该档"星点无法解释的结构能量"与目标包络（重窗中值）
               的秩相关；(原始) = 同相关但用未扣星点的功率；vs星点r = 与格内星点强度的
               相关；典型lift = 格中位超噪功率 / 噪声底。
    """
    allowed = band_allowed_centers(block)
    idxs = [k for k in np.where(allowed)[0]]
    _, bg, sigma = sigma_clipped_stats(crop, sigma=3.0, maxiters=5)
    xs, ys, pk = find_stars(crop, bg, sigma)
    windows = {}
    for k in idxs:
        windows.setdefault(derived_med([BANDS[k]['center_px']]), []).append(k)
    env_s = band_envelope(star_removed(crop, EXT_MED_PX), max(BANDS[k]['center_px'] for k in idxs))
    m = {}
    for med, ks in sorted(windows.items()):
        ext = star_removed(crop, med)
        unit = median_noise_floor_unit(block, med)
        for k in ks:
            b = BANDS[k]
            d = b['decim']
            ny, nx = crop.shape[0] // block, crop.shape[1] // block
            img = _single_band_image(ext, bg, b)
            raw = robust_band_power(img, block, d, ny, nx)
            del img
            nv = float(unit[k]) * sigma ** 2
            star_map = star_power_map(xs, ys, pk, block, ny, nx)
            env = envelope_tiles(env_s, 8, block, ny, nx)
            e = np.maximum(raw - nv, 0.0)
            slope = _fit_star_model(raw, star_map)
            sg = np.maximum(raw - nv - slope * star_map, 0.0)
            m[b['center_px']] = {
                'band': k, 'med': med, 'ny': ny, 'nx': nx, 'n': ny * nx,
                'lift': float(np.median(e)) / max(nv, 1e-30),
                'rho_s': spearman(env, sg), 'rho_p': spearman(env, e),
                'r': _star_r(raw, star_map), 'slope': slope,
                'grid': np.log10(np.maximum(sg / max(nv, 1e-30), 1e-2)),
            }
        del ext
    log('选带表（共用块 %dpx；档 / 去星窗 / 典型lift / vs包络ρ / (原始) / vs星点r）：'
        % block)
    for ctr in sorted(m):
        v = m[ctr]
        log(f'{ctr:>5.0f}px  窗{v["med"]:>3}px  lift {v["lift"]:>7.1f}  '
            f'ρ {v["rho_s"]:>+5.2f}  (原始) {v["rho_p"]:>+5.2f}  星点r {v["r"]:>+5.2f}')
    return m


def _single_band_image(data: np.ndarray, bg: float, band: Dict) -> np.ndarray:
    """单个档的高斯差分图（扫描阶段用，避免一次构造全部档）"""
    d = band['decim']
    lo = _bin_down(data.astype(np.float32) - np.float32(bg), d)
    ga = ndimage.gaussian_filter(lo, band['sigma_a'] / d, mode='nearest')
    gb = ndimage.gaussian_filter(lo, band['sigma_b'] / d, mode='nearest')
    out = (ga - gb).astype(np.float32)
    del lo, ga, gb
    return out


def _star_r(power: np.ndarray, star_reg: np.ndarray) -> float:
    """该档块功率与分块星点强度的相关系数（判该档是否被星场主导）"""
    a = np.asarray(power, dtype=np.float64).ravel()
    u = np.asarray(star_reg, dtype=np.float64).ravel()
    ok = np.isfinite(a) & np.isfinite(u)
    if ok.sum() <= 10 or np.std(a[ok]) <= 0 or np.std(u[ok]) <= 0:
        return float('nan')
    return float(np.corrcoef(a[ok], u[ok])[0, 1])


def pick_bands(m: Dict, log=print) -> List[float]:
    """自适应选带：取"结构最跟着目标走"的那一档 + 其粗邻一档

    判据是本图自己量出来的 vs包络ρ（该档星点无法解释的结构能量 与 目标包络
               的秩相关）——它回答"这一档的结构图长得像不像目标"。753（暗弱云气）的细档
               主要是星点纹理（ρ 弱），80px 才与云气同步（ρ 最高）；7331（星系核/旋臂）
               的 10px 就跟着星系盘走（ρ 最高）。取 ρ 最高档作细节承载档，加其粗邻一档
               成对（模糊帧最先丢这一段）。不需要"星系细、云气粗"之类的预设。
    各档共用同一个块、格数相同，故并列阈值取该格数下秩相关的 2σ 抽样噪声
               （τ=2/√(n−1)），而不是写死 0.02：格数多 → 阈值紧（分辨率高）；格数少时
               0.02 会把"统计上分不出高低"的两档硬判成粗档赢（实测 7331@4096 用 128px
               块时 10px(+0.19) 与 20px(+0.23) 差 0.04，小于 2σ=0.063，按并列取更细的
               10px 才复现真值带 [10,20]）。
    """
    bs = sorted(m)

    def rho(b):
        v = m[b]['rho_s']
        return v if np.isfinite(v) else -2.0
    order = sorted(bs, key=lambda b: -rho(b))
    b0 = order[0]
    n = max(int(m[bs[0]]['n']), 2)
    tau = 2.0 / np.sqrt(n - 1)          # 2σ 秩相关抽样噪声：格数越多越紧
    for b in order[1:]:
        if b < b0 and rho(b) >= rho(b0) - tau:
            b0 = b
        else:
            break
    i = bs.index(b0)
    sel = [b0] if i == len(bs) - 1 else [b0, bs[i + 1]]
    log(f'选带：ρ 最高档 {b0:.0f}px（vs包络ρ={rho(b0):+.2f}，'
        f'vs星点r={m[b0]["r"]:+.2f}，典型lift={m[b0]["lift"]:.1f}；'
        f'并列阈值 2σ={tau:.3f}@{n}格）→ 判据带 '
        + '/'.join(f'{b:.0f}px' for b in sel))
    log('ρ 排序：' + ' > '.join(f'{b:.0f}px {rho(b):+.2f}' for b in order))
    return sel


def structure_prom(sg: np.ndarray, noise: np.ndarray, band_idx: List[int],
                   gate: float = 3.0, med_fixed: Optional[float] = None):
    """结构显著度（v4 温度定义）：该块在判据带上的结构能量 ÷ 全图中位

    sg 形状 (..., ny, nx, 判据带)；band_idx 是判据带在 sg 最后一维上的序号。
               返回 (prom, tot, valid, med)：
                 tot   各判据带的"星点无法解释的结构能量"之和（已扣噪声）
                 valid 至少一个判据带的能量超过 gate×噪声（否则该块"测不到"）
                 prom  = log10(tot / median(tot[valid]))，测不到的块为 NaN（图上留白）
    med_fixed：跨帧共用的归一化基准（锐利子集合成的全图中位）。逐帧比较必须
               传它——各帧自算中位等于每帧被自己图内的中位块除一遍，帧与帧的等级差会被
               抹平（753 实测整夜 0.515~0.572）。不传则用本图自己的中位（单图口径）。
    与"细半带占比"的区别：相邻两档占比在实测数据上退化成散斑（753 全图 36 块
               挤在 0.33~0.64、7331 的 10/20 占比图是纯散斑）——分子分母都是同一处的邻档
               读数，比值不带目标信息。显著度问的是"这块在该目标尺度上有多突出"。
    """
    tot = np.zeros(sg.shape[:-1], dtype=np.float64)
    ok = np.zeros_like(tot, dtype=bool)
    for k in band_idx:
        tot += np.where(np.isfinite(sg[..., k]), sg[..., k], 0.0)
        ok |= sg[..., k] > gate * noise[..., k]
    valid = ok & (tot > 0)
    if med_fixed is not None:
        med = float(med_fixed)
    else:
        med = float(np.median(tot[valid])) if np.any(valid) else 1.0
    prom = np.full(tot.shape, np.nan, dtype=np.float64)
    prom[valid] = np.log10(np.maximum(tot[valid], 1e-30) / max(med, 1e-30))
    return prom, tot, valid, med


# ---------------------------------------------------------------------------
# 提速：逐帧多进程 + block 级中间量缓存
# ---------------------------------------------------------------------------
#
# 为什么要缓存：逐帧重活的产物（各档功率、噪声基准、星点强度图、能量集中度）
#            只依赖数据与块参数，不依赖任何判据参数（闸门、归一化基准、锐利子集比例）。
#            把这一层存下来之后，改判据就是秒级重算，不必再等整轮分析。
# 为什么要并行：帧与帧完全独立——判据的跨帧合成只发生在 block 级，
#            数据量是像素级的百万分之一；因此逐帧用多进程，墙钟时间直接除以进程数。

CACHE_VERSION = 4   # 缓存口径版本：逐帧度量一变就加一，旧缓存自动作废
                    # v2：结构层改为在"星点已抹掉"的图上量（中值移除 + 合成噪声底）
                    # v3：v4 口径——逐帧只算判据带、结构层两条去星路线
                    #            （填充 / 中值）、星点强度改固定星表口径
                    # v4：掩膜改用"含亮/饱和星"的全量局部极大星表
                    #            （原来误用集中度口径的星表，亮星光晕漏掩）


def _frame_job(job: Dict) -> Dict:
    """单帧的全部重活（多进程 worker；必须在模块顶层才能被序列化）

    返回值全是 block 级小数组（几十 KB），进程间回传无代价。
    结构层的两条路线（由 analyze_folder 按掩膜覆盖率裁决后写进 job）：
               fill = 固定星表 → 逐档派生半径掩膜 → 归一化均值填充：星点在带通域被真正
                      移除（不靠统计扣减，后者对最亮星的大光晕系统性欠扣，7331 实测残留
                      两块假热斑），噪声仍是白噪声，噪声底继续用解析增益；
               med  = 中值滤波去星 + 星点斜率闸门：用于密集星场（753 掩膜覆盖率 99%，
                      填充等于把延展结构一起抹掉）。
    """
    data, meta = read_frame(Path(job['path']))
    crop = job['crop']
    if crop is not None:
        data = np.ascontiguousarray(data[crop[0]:crop[1], crop[2]:crop[3]])
    _, bg, noise = sigma_clipped_stats(data, sigma=3.0, maxiters=5)
    norm = np.float32(EXPTIME_REF / meta['exptime'])
    idxs = job['band_idx']

    if job['route'] == 'fill':
        work = (data * norm).astype(np.float32, copy=False)
        power, bnoise = structure_band_maps(
            work, float(bg) * float(norm), float(noise) * float(norm),
            job['block'], job['ny'], job['nx'],
            fill_geo=job['fill_geo'], band_idx=idxs)
        del work
    else:
        ext = star_removed(data, job['med_px'])
        ext *= norm
        # floor_unit 按 BANDS 全表索引（structure_band_maps 内部用 k 取），故此处
        # 只按本帧噪声缩放，不能先按 idxs 取子集
        nv = np.asarray(job['floor_unit'], dtype=np.float64) \
            * (float(noise) * float(norm)) ** 2
        power, bnoise = structure_band_maps(
            ext, float(bg) * float(norm), 0.0, job['block'], job['ny'], job['nx'],
            noise_vector=nv, band_idx=idxs)
        del ext

    # 星点层仍在原图上做：集中度与星点强度都必须看真实星点
    conc, fwhm, n_used = star_sharpness(data, bg, job['star_x'], job['star_y'])
    # 星点强度改固定星表口径：每帧在同一批星位上量本帧高通图的 3×3 峰值
    pk = fixed_star_peaks(data, HP_SIGMA, job['star_x'], job['star_y'])
    del data
    sflux = star_power_map(job['star_x'], job['star_y'], pk, job['block'],
                           job['ny'], job['nx'])
    return {
        'name': meta['name'], 'path': job['path'], 'exptime': meta['exptime'],
        'date_obs': meta['date_obs'], 'object': meta['object'], 'filter': meta['filter'],
        'bg': float(bg), 'noise': float(noise),
        'sharpness': conc, 'fwhm': fwhm, 'n_used': int(n_used),
        # n_stars = 固定星表里本帧真正量到（峰值 > 5σ）的星数：与帧质量同向、
        #            可跨帧比较（旧的逐帧检出口径下它混着检测阈值差异，753 实测 4.3 倍差）
        'n_stars': int(np.count_nonzero(pk > 5.0 * noise)),
        'power': power.astype(np.float32),
        'bnoise': np.asarray(bnoise, dtype=np.float64),
        'star_power': sflux.astype(np.float32),
    }


def _default_workers(n_frames: int) -> int:
    """默认并行度：物理核数的一半，且不超过帧数

    os.cpu_count() 给的是逻辑核（9950X 报 32），先折半得物理核（16），
               再折半作为并行度（8）：单帧峰值 RSS 实测 1.26GB（全幅、DAOStarFinder 段），
               8 进程约 10GB，留足余量；再往上加只会在内存与带宽上互相拖。
    """
    logical = max(1, os.cpu_count() or 4)
    phys = max(1, logical // 2)
    return max(1, min(n_frames, max(1, phys // 2)))


def _log_frame(log, i: int, n: int, fr: Dict):
    log(f'  [{i + 1}/{n}] {fr["name"][:50]}  {fr["exptime"]:.0f}s  '
        f'集中度={fr["sharpness"]:.4f}  FWHM={fr["fwhm"]:.2f}  星点={fr["n_stars"]}')


def _run_frame_jobs(jobs: List[Dict], workers: Optional[int], log) -> List[Dict]:
    """跑逐帧任务；workers=1 退化为串行（便于调试），默认自动定并行度"""
    n = len(jobs)
    w = _default_workers(n) if workers is None else max(1, min(int(workers), n))
    out: List[Dict] = [{}] * n
    t0 = time.perf_counter()
    if w == 1:
        for i, job in enumerate(jobs):
            out[i] = _frame_job(job)
            _log_frame(log, i, n, out[i])
    else:
        with ProcessPoolExecutor(max_workers=w) as ex:
            futs = {ex.submit(_frame_job, job): i for i, job in enumerate(jobs)}
            for fut in as_completed(futs):
                i = futs[fut]
                out[i] = fut.result()
                _log_frame(log, i, n, out[i])
    log(f'[逐帧] {n} 帧 × {w} 进程，用时 {time.perf_counter() - t0:.1f}s')
    return out


_META_KEYS = ('name', 'path', 'exptime', 'date_obs', 'object', 'filter',
              'bg', 'noise', 'sharpness', 'fwhm', 'n_used', 'n_stars')


def _cache_file(photo_dir: Path, paths: List[Path], block: int, crop_px,
                key_extra: str, cache_dir: Optional[Path]) -> Path:
    """缓存文件名 = 目录名 + 帧清单指纹（名字/大小/修改时间）+ 参数指纹

    key_extra：判据带 / 去星路线与窗 / 填充几何常量。逐帧产物依赖它们，
               任一项变化都必须重算；裁剪与块也已进指纹。
    """
    h = hashlib.sha1()
    h.update(f'v{CACHE_VERSION}|{block}|{crop_px}|{key_extra}|{HP_SIGMA}'
             f'|{EXPTIME_REF}|{photo_dir}'.encode())
    for p in paths:
        st = p.stat()
        h.update(f'{p.name}|{st.st_size}|{int(st.st_mtime)}'.encode())
    base = (Path(cache_dir) if cache_dir is not None
            else Path(tempfile.gettempdir()) / 'dwt_detail_cache')
    base.mkdir(parents=True, exist_ok=True)
    return base / f'{photo_dir.name}_{block}_{h.hexdigest()[:12]}.npz'


def _save_cache(fp: Path, frames: List[Dict], star_x, star_y, shape, crop) -> None:
    """写缓存（很小：753 全幅 12 帧合计约 60KB）；失败不影响主流程"""
    try:
        np.savez_compressed(
            fp,
            meta=json.dumps([{k: f[k] for k in _META_KEYS} for f in frames]),
            power=np.stack([f['power'] for f in frames]),
            bnoise=np.stack([f['bnoise'] for f in frames]),
            star=np.stack([f['star_power'] for f in frames]),
            cat_x=np.asarray(star_x, dtype=np.float32),
            cat_y=np.asarray(star_y, dtype=np.float32),
            shape=np.asarray(shape, dtype=np.int64),
            crop=np.asarray(crop if crop is not None else [], dtype=np.int64),
        )
    except Exception as e:
        print(f'缓存写入失败（{e}），忽略')


def _load_cache(fp: Path):
    """读缓存；任何异常都当未命中，回退到重算"""
    with np.load(fp, allow_pickle=False) as z:
        meta = json.loads(str(z['meta']))
        power, bnoise, star = z['power'], z['bnoise'], z['star']
        cat_x, cat_y = z['cat_x'], z['cat_y']
        shape = tuple(int(v) for v in z['shape'])
        crop = tuple(int(v) for v in z['crop'])
    frames = []
    for i, m in enumerate(meta):
        fr = dict(m)
        fr['power'] = power[i]
        fr['bnoise'] = bnoise[i]
        fr['star_power'] = star[i]
        frames.append(fr)
    return (frames, cat_x.astype(np.float64), cat_y.astype(np.float64), shape,
            crop if len(crop) == 4 else None)


def analyze_folder(photo_dir: Path, block: Optional[int] = None,
                   sharp_frac: float = 0.30,
                   limit: Optional[int] = None, log=print,
                   crop_px: Optional[int] = None,
                   crop_center: Optional[Tuple[int, int]] = None,
                   bands: Optional[List[float]] = None,
                   workers: Optional[int] = None,
                   use_cache: bool = True,
                   cache_dir: Optional[Path] = None) -> Dict:
    """对目录内所有亮场做一次完整的延展结构分析（v4 口径）

    v4 与 v3 的差别（用户确认的方向）
               1) 块边长、判据带、去星窗全部由目标自身数据派生，不写死数值：块按"最亮
                  延展团"尺寸（scan_block），判据带按"该档结构图长得像不像目标"
                  （scan_table + pick_bands），去星窗随所判尺度（derived_med）。实测
                  7331（星系）落在 10/20px、753（暗弱云气）落在 80/113px，都是本图
                  自己量出来的，不需要"星系细、云气粗"之类的预设。
               2) 温度 = 结构显著度 prom = log10(该块判据带结构能量 ÷ 全图中位)。
               3) 去星路线按掩膜覆盖率裁决：覆盖率低（7331 的 10/20px 档 8%/23%）走
                  "掩膜 + 归一化填充"——星点在带通域被真正移除；覆盖率高（753 的
                  80/113px 档 99%）走"中值去星 + 星点斜率闸门"——填充会铺满整幅。
               4) 星点强度改固定星表口径（逐帧检测阈值差异会污染它），选帧仍只用锐利子集。

    两个层次严格分离：星点层只管 PSF 级精细（固定星表上的能量集中度排序），
               结构层管延展结构（本模块的判据对象）。
    crop_px：只分析目标附近的方形区域。深空目标常常只占画面的很小一部分
               （实测 NGC 7331 仅 930×330 像素，却落在 9568×6388 的画面里），
               不裁剪就只能用很少的区块覆盖目标，判据会退化成噪声图。
    crop_center=(y, x)：裁剪中心（原图像素）；不给则按分块中值自动定位目标。
    bands：判据带（档中心尺度 px 列表，如 [10, 20]）；不给则自适应选带。
    block：块边长（px）；不给则按目标尺寸派生（scan_block，上下限 128/512）。
    workers：逐帧并行进程数（None = 自动取物理核的一半，且不超过帧数）；
               use_cache / cache_dir：逐帧中间量的缓存开关与目录（默认系统临时目录）。
               缓存只存与判据无关的量，因此改闸门、改锐利子集比例都不必重算。
    """
    paths = sorted(p for p in photo_dir.iterdir()
                   if p.suffix.lower() in FITS_SUFFIXES | {'.xisf'})
    if limit:
        paths = paths[:limit]
    if not paths:
        raise FileNotFoundError(f'{photo_dir} 下没有找到可读帧')

    t_all = time.perf_counter()
    center_px = np.array([b['center_px'] for b in BANDS])
    log(f'[分析] 共 {len(paths)} 帧，锐利子集 {sharp_frac:.0%}')

    # --- 参考帧（首帧）：定裁剪、定块、选带、裁决去星路线、建固定星表
    data0, _ = read_frame(paths[0])
    crop = None
    if crop_px:
        if crop_center is not None:
            cy, cx = int(crop_center[0]), int(crop_center[1])
        else:
            cy, cx = locate_extended_source(data0)
        half = crop_px // 2
        h, w = data0.shape
        y0 = int(np.clip(cy - half, 0, max(h - crop_px, 0)))
        x0 = int(np.clip(cx - half, 0, max(w - crop_px, 0)))
        half = min(half, h - y0, w - x0)
        crop = (y0, y0 + 2 * half, x0, x0 + 2 * half)
        data0 = np.ascontiguousarray(data0[crop[0]:crop[1], crop[2]:crop[3]])
        log(f'[裁剪] 目标中心 (y={cy}, x={cx})，分析区 '
            f'{crop[1] - crop[0]}×{crop[3] - crop[2]} px = '
            f'y[{crop[0]}:{crop[1]}] x[{crop[2]}:{crop[3]}]')

    _, bg0, noise0 = sigma_clipped_stats(data0, sigma=3.0, maxiters=5)
    shape = data0.shape
    if block is None:
        block = scan_block(data0, log)
    log(f'[块] 块边长 {block}px（该块下尺度上限 {block / BLOCK_SCALE_RATIO:.0f}px）')

    # 判据带：显式给（按最近档匹配，档中心尺度带小数）或本图自适应选带
    if bands:
        req = sorted({float(b) for b in bands})
    else:
        req = pick_bands(scan_table(data0, block, log), log)
    band_idx: List[int] = []
    for r in req:
        k = int(np.argmin([abs(b['center_px'] - r) for b in BANDS]))
        if abs(BANDS[k]['center_px'] - r) > 0.1 * r:
            raise ValueError('未知判据带 %.0fpx，可选：' % r
                             + '/'.join(f'{b["center_px"]:.0f}' for b in BANDS))
        band_idx.append(k)
    band_idx = sorted(set(band_idx))
    band_sel = [float(BANDS[k]['center_px']) for k in band_idx]

    # 固定星表（参考帧建、全量不设上限）：逐帧都在同一批星位上量
    star_x, star_y, star_pk = build_star_list(data0, bg0, noise0, n_max=None)
    if len(star_x) < 20:
        raise RuntimeError('可用星点太少，无法可靠排序帧质量')
    log(f'[星表] 固定星表 {len(star_x)} 颗未饱和孤立星，全程复用')

    # 去星路线裁决：逐档算"掩膜 + 填充"的半径覆盖率，任一判据带盖不住就改中值
    # 掩膜星表另建一张（含饱和/非孤立亮星）：与上面为集中度口径排除亮星的那张
    # 是两回事——掩膜要罩住的正是最亮的那几颗（它们的 halo 最大）
    mx, my, mp = bright_peaks(data0, noise0)
    if len(mx) < 20:
        mx, my, mp = star_x, star_y, star_pk
    fill_geo: List[Optional[Dict]] = [None] * len(BANDS)
    covs: Dict[float, float] = {}
    for k in band_idx:
        geo, cov = _fill_geometry_for(BANDS[k], mp, mx, my, shape, noise0)
        covs[float(BANDS[k]['center_px'])] = cov
        fill_geo[k] = geo
    route = 'fill' if all(fill_geo[k] is not None for k in band_idx) else 'med'
    med_px = derived_med(band_sel) if route == 'med' else 0
    log(f'[去星] 掩膜星表 {len(mx)} 颗（含亮/饱和星）；判据带掩膜覆盖率 ' + ' | '.join(
        f'{c:.0f}px {v:.1%}' for c, v in sorted(covs.items())) +
        f' → 路线 {route}' +
        (f'（中值窗 {med_px}px）' if route == 'med' else '（掩膜 + 归一化填充）'))

    ny, nx = shape[0] // block, shape[1] // block
    if ny < 2 or nx < 2:
        raise RuntimeError(f'分析区 {shape} 小于块 {block}px，请缩小块或扩大裁剪')
    del data0

    # 先看缓存：命中就完全跳过"读帧 + 滤波 + 星点检测"这一段
    key_extra = (f'{route}|med{med_px}|bands{band_sel}|'
                 f'fill{FILL_K}/{FILL_RMAX_MULT}/{FILL_SMEAR_PX}/{FILL_COV_MAX}')
    cache_fp = (_cache_file(photo_dir, paths, block, crop, key_extra, cache_dir)
                if use_cache else None)
    frames = None
    if cache_fp is not None and cache_fp.exists():
        try:
            frames, _, _, _, _ = _load_cache(cache_fp)
            log(f'[缓存] 命中 {cache_fp.name}（{len(frames)} 帧），跳过逐帧重算')
        except Exception as e:
            log(f'[缓存] 不可用（{e}），改为重算')
            frames = None

    if frames is None:
        # 噪声底只算一次：让真实管线量一遍合成白噪声（中值去星口径专用）
        floor_unit = (median_noise_floor_unit(block, med_px) if route == 'med'
                      else np.zeros(len(BANDS)))
        if route == 'med':
            log(f'[去星] 中值窗 {med_px}px，判据带噪声底（单位 σ²）：' + ' | '.join(
                f'{BANDS[k]["center_px"]:.0f}px {floor_unit[k]:.3e}' for k in band_idx))
        # 逐帧任务全部并行（帧间独立、回传只有 block 级小数组）
        jobs = [{'path': str(p), 'crop': crop, 'block': block, 'ny': ny, 'nx': nx,
                 'band_idx': band_idx, 'route': route, 'med_px': med_px,
                 'fill_geo': fill_geo, 'floor_unit': floor_unit,
                 'star_x': star_x, 'star_y': star_y}
                for p in paths]
        frames = _run_frame_jobs(jobs, workers, log)
        if cache_fp is not None:
            _save_cache(cache_fp, frames, star_x, star_y, shape, crop)
            log(f'[缓存] 已写入 {cache_fp}')

    if len(frames) < 4:
        raise RuntimeError('有效帧太少，无法排序，请检查数据')

    # 星点层选帧：能量集中度越高，星点越细腻
    # 子集边界改用"集中度自然断档"（见 SHARP_GAP_MIN）：固定比例会把同一清晰
    # 等级的帧切一半，丢掉同等锐利的帧后星核方向分散变差、星点反而更椭圆。
    sh = np.array([f['sharpness'] for f in frames], dtype=np.float64)
    order = np.argsort(-np.where(np.isfinite(sh), sh, -np.inf))
    n_frac = min(max(2, int(round(len(frames) * sharp_frac))), len(frames) - 2)
    n_sharp = n_frac
    shs = sh[order]
    shs = shs[np.isfinite(shs)]
    if shs.size >= 4:
        ratio = shs[:-1] / np.maximum(shs[1:], 1e-30)
        j = int(np.argmax(ratio))
        if ratio[j] >= SHARP_GAP_MIN and 2 <= j + 1 <= len(frames) - 2:
            n_sharp = j + 1
            log(f'[分析] 集中度自然断档：排名 {j + 1}/{j + 2} 之间 {ratio[j]:.2f} 倍'
                f'（{shs[j]:.4f} → {shs[j + 1]:.4f}），锐利子集取 {n_sharp} 帧'
                f'（固定 {sharp_frac:.0%} 会取 {n_frac} 帧）')
    sharp_idx = [int(i) for i in order[:n_sharp]]
    blur_idx = [int(i) for i in order[n_sharp:]]

    cube = np.stack([f['power'] for f in frames]).astype(np.float64)      # (帧, y, x, 判据带)
    bnoise_cube = np.stack([f['bnoise'] for f in frames])                 # (帧, 判据带)
    sp_cube = np.stack([f['star_power'] for f in frames]).astype(np.float64)  # (帧, y, x) 分块星点强度

    # 结构层：扣掉"星点强度可解释的功率"。只在中值路线做——填充路线星点已在数据里
    # 被真正移除，再扣会把星密区的真结构一起减掉。逐帧各自拟合：各帧透明度不同，
    # 共用一条基线会把较暗的帧整幅减成负数、截断成 0（v3 实测踩过）。
    sg_cube = star_excess(cube, sp_cube) if route == 'med' else cube
    sg_sharp = _nanmedian(sg_cube[sharp_idx], axis=0)   # 参考像（锐利子集聚合）
    nsh = np.median(bnoise_cube[sharp_idx], axis=0)
    local_idx = list(range(len(band_idx)))

    # 逐帧 prom 的基准必须跨帧共用（参考像的全图中位）：各帧自算中位等于每帧被
    # 自己图内的中位块除一遍，帧与帧的等级差会被抹平（753 实测整夜 0.515~0.572）
    prom_ref, _, valid_ref, med_ref = structure_prom(sg_sharp, nsh, local_idx)
    frame_prom = [structure_prom(sg_cube[i], bnoise_cube[i], local_idx,
                                med_fixed=med_ref)[0] for i in range(len(frames))]

    # 目标温度图 = 全量帧累积（753 中心云气靠全部帧累积才出结构）
    sg_all = _nanmedian(sg_cube, axis=0)
    nall = np.median(bnoise_cube, axis=0)
    prom, tot, valid, prom_med = structure_prom(sg_all, nall, local_idx)
    star_power = _nanmedian(sp_cube, axis=0)

    log(f'[显著度] 判据带 ' + '/'.join(f'{b:.0f}px' for b in band_sel))
    log(f'[显著度] 全量帧累积：有效块 {int(np.count_nonzero(valid))}/{valid.size}，'
        f'全图中位结构能量 {prom_med:.4g}' +
        (f'，prom P50={np.nanpercentile(prom, 50):.3f} '
         f'P90={np.nanpercentile(prom, 90):.3f} '
         f'max={np.nanmax(prom):.3f}' if np.any(valid) else '（无有效块）'))
    if np.any(valid):
        it = np.unravel_index(int(np.nanargmax(np.where(valid, prom, -np.inf))), prom.shape)
        # 全幅（crop=None）时工作域左上角就是原图原点
        oy = 0 if crop is None else crop[0]
        ox = 0 if crop is None else crop[2]
        log(f'[显著度] 最热块 ({it[0]},{it[1]}) → 原图 '
            f'(y={oy + it[0] * block}, x={ox + it[1] * block})，'
            f'prom={prom[it]:.2f}')

    for i, f in enumerate(frames):
        f['is_sharp'] = i in sharp_idx
        f['rank'] = int(np.where(order == i)[0][0])
        p = frame_prom[i][np.isfinite(frame_prom[i])]
        f['mean_prom'] = float(np.mean(p)) if p.size else 0.0
        f['p95_prom'] = float(np.percentile(p, 95)) if p.size else 0.0
        f['max_prom'] = float(np.max(p)) if p.size else 0.0
        f['n_prom_blocks'] = int(np.count_nonzero(p > 0.1))

    log(f'[分析] 星点层锐利子集 {len(sharp_idx)} 帧，其余 {len(frames) - len(sharp_idx)} 帧'
        f'，总用时 {time.perf_counter() - t_all:.1f}s')

    allowed_band = np.zeros(len(BANDS), dtype=bool)
    allowed_band[band_idx] = True
    return {
        'frames': frames,
        'block': block,
        'grid': (ny, nx),
        'bands': [BANDS[k] for k in band_idx],        # 判据带（该目标自己的尺度带）
        'band_idx': band_idx,
        'band_scale': band_sel,
        'allowed_band': allowed_band,
        'allowed_block': band_allowed(block),
        'band_scale_px': center_px,
        'band_noise': bnoise_cube,
        'sharp_idx': sharp_idx,
        'blur_idx': blur_idx,
        'band_cube': cube,
        'band_sharp': sg_sharp,                       # 参考像（锐利子集）结构能量
        'band_all': sg_all,                           # 全量帧累积结构能量
        'band_res_sharp': sg_sharp,
        'band_res_cube': sg_cube,                     # (帧, y, x, 判据带) 星点无法解释的能量
        'band_star_r': [_star_r(sg_all[..., j], star_power) for j in local_idx],
        'star_power': star_power,
        'star_power_cube': sp_cube,
        'prom': prom,                                 # 全量帧累积的结构显著度（温度图）
        'prom_sharp': prom_ref,                       # 参考像的结构显著度
        'prom_tot': tot,
        'prom_valid': valid,
        'prom_med': prom_med,
        'frame_prom': np.stack(frame_prom),           # 逐帧（基准 = 参考像全图中位）
        'route': route,
        'med_px': med_px,
        'fill_cov': covs,
        'n_stars_catalog': int(len(star_x)),
        'crop': crop,                                 # None 表示未裁剪（全幅工作域）
        'shape': shape,                               # 工作域尺寸，全幅叠加时要用
        'ref_frame': frames[sharp_idx[0]]['path'],
    }