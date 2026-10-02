# -*- coding: utf-8 -*-
# DontWasteTime 核心层：帧读取
#
# 目的
# 把 FITS / XISF 帧文件读成 float32 二维数组与元数据字典，
# 供叠加管线（core/pws.py）加载素材使用。
#
# 本文件以 MIT 许可证发布，全文见 LICENSE，授权范围见 README.md。
# XISF 读取由同目录的 xisf_io.py 提供。

import numpy as np
from pathlib import Path
from typing import Dict, Tuple

from astropy.io import fits
from scipy import ndimage

FITS_SUFFIXES = {'.fits', '.fit', '.fts'}
EXPTIME_REF = 300.0  # 曝光归一化基准（秒）


# ---------------------------------------------------------------------------
# 帧读取
# ---------------------------------------------------------------------------

def read_frame(path: Path) -> Tuple[np.ndarray, Dict]:
    """读取一帧，返回 (float32 二维数组, 元数据字典)"""
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
    elif suffix == '.xisf':
        import xisf_io
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
    else:
        raise ValueError(f'不支持的格式：{path.name}')

    if data.ndim == 3:
        data = data[..., :3].mean(axis=-1)
    data = np.ascontiguousarray(data, dtype=np.float32)
    meta.update({'path': str(path), 'name': path.name, 'shape': data.shape})
    return data, meta


# ---------------------------------------------------------------------------
# 延展源定位（裁剪中心留空时用）
# ---------------------------------------------------------------------------

def locate_extended_source(data: np.ndarray, bin_px: int = 32) -> Tuple[int, int]:
    """定位画面中最亮的延展天体（星系/星云），返回像素坐标 (y, x)。

    方法：按 bin_px 分块取中值。中值可滤除星点（星点仅占块内极少数像素），
    仅保留占据整块的延展流量，故峰值位于星系或星云所在位置。
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
