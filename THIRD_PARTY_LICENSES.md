# 第三方开源组件与版权声明

本软件使用了下列第三方开源组件，均为可商用许可。它们**不在**本软件的 MIT 授权
范围内，各自遵循原许可；再分发时请保留其版权声明与许可证（随各组件包一并分发）。

| 组件 | 本软件中的用途 | 版权声明 | 许可证 |
| --- | --- | --- | --- |
| NumPy | 数组与逐像素数值运算（全栈） | Copyright (c) 2005-2025, NumPy Developers. All rights reserved. | BSD-3-Clause（另含 0BSD / MIT / Zlib / CC0-1.0 的少量组件） |
| SciPy | 高斯 / 均值滤波、KD 树、秩统计（`scipy.ndimage` / `spatial.cKDTree` / `stats`） | Copyright (c) 2001-2002 Enthought, Inc. 2003, SciPy Developers. All rights reserved. | BSD-3-Clause |
| Astropy | FITS 头读写、WCS 承载、σ 剪裁统计（`io.fits` / `wcs` / `stats`）；随包组件 erfa（pyerfa，坐标系底层）与 astropy_iers_data（地球自转参数表） | Copyright (c) 2011-2024, Astropy Developers. All rights reserved.（pyerfa：Copyright (c) 2019-2024, The pyerfa developers） | BSD-3-Clause（另含随包的 AURA / ERFA / EXPAT 等许可） |
| Photutils | 星点检测与 PSF 拟合（`detection.DAOStarFinder` / `detection.find_peaks` / `psf.fit_fwhm`） | Copyright (c) 2011-2026, Photutils Developers. All rights reserved. | BSD-3-Clause |
| psutil（可选） | 探测可用内存，决定临时帧是否落盘 | Copyright (c) 2009, Jay Loden, Dave Daeschler, Giampaolo Rodola. All rights reserved. | BSD-3-Clause |
| PyYAML | astropy 表格元数据（ECSV/YAML 头）读写的必需依赖，随 astropy 导入链加载 | Copyright (c) 2017-2021 Ingy döt Net / Copyright (c) 2006-2016 Kirill Simonov | MIT（全文见 [LICENSES/PyYAML-MIT.txt](LICENSES/PyYAML-MIT.txt)） |
| PySide6（Qt for Python） | 图形界面（`QtWidgets` / `QtCore` / `QtGui`） | The Qt Company Ltd.（随包的 `LicenseRef-Qt-Commercial.txt` 及各 Qt 库自带的声明文件） | LGPL-3.0（亦可按 GPL-2.0 / GPL-3.0 / 商业许可使用） |
| OpenSSL（libssl-3 / libcrypto-3） | Python 标准库 `ssl` / `hashlib` 扩展的底层加密实现（随引擎导入加载；本软件自身不发起任何联网） | Copyright 1998-2024 The OpenSSL Authors. All rights reserved. | Apache-2.0（全文见 [LICENSES/Apache-2.0.txt](LICENSES/Apache-2.0.txt)） |
| Python 标准库（含 `ctypes` 及其捆绑的 libffi） | zlib / xml.etree / math / argparse / concurrent.futures / html / threading / io … | Copyright (c) Python Software Foundation.（捆绑组件的版权声明含于同一文件） | PSF-2.0（全文见 [LICENSES/Python-PSF.txt](LICENSES/Python-PSF.txt)） |

## BSD-3-Clause 许可证全文

下列组件在打包产物中未随附各自的许可证文件，全文统一收录于此（版权人见上表）：
**SciPy、psutil、erfa（pyerfa）、astropy-iers-data**。
NumPy、Astropy、Photutils 的许可证文件已随包分发（`_internal/` 下各组件的
`*.dist-info/licenses/`），无需另行收录。

Copyright (c) 版权人见上表. All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice,
   this list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

3. Neither the name of the copyright holder nor the names of its contributors
   may be used to endorse or promote products derived from this software
   without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
POSSIBILITY OF SUCH DAMAGE.

## PySide6 / Qt 的 LGPL-3.0 使用方式

本软件仅以动态导入方式使用 pip 安装的二进制 wheel，**未修改 Qt 源码**。

LGPL-3.0 的完整文本见 [LICENSES/LGPL-3.0.txt](LICENSES/LGPL-3.0.txt)；LGPL-3.0 通过引用
并入 GPL-3.0 的条款，GPL-3.0 全文见 [LICENSES/GPL-3.0.txt](LICENSES/GPL-3.0.txt)。

再分发二进制时：

- 保留上述两份许可证全文与 Qt 版权声明（Qt 自带的声明文件随 PySide6 包一并分发）。
- 保证接收者能够替换 Qt 库。因此打包时必须使用 PyInstaller 的**文件夹模式**
  （`--onedir`）而不是单文件模式（`--onefile`）：前者把 Qt 的动态库以独立文件放在
  产物目录中，接收者可以自行替换；后者会把它们封进一个自解压可执行文件，无法替换。
- 本软件以源码形式发布，接收者也可以直接用源码配合自己安装的 Qt 运行。

## 二进制再分发时需要附带什么

Python 侧的许可证由打包过程自动放入 `_internal/` 下各组件的 `*.dist-info/licenses/`
（NumPy、Astropy、Photutils 等）。除此之外请随包保留：

- `LICENSE`：本软件的 MIT 许可证。
- `LICENSES/LGPL-3.0.txt`、`LICENSES/GPL-3.0.txt`：Qt / PySide6 的许可证全文。
- `LICENSES/LicenseRef-Qt-Commercial.txt`、`LICENSES/LicenseRef-Qt-Commercial-shiboken6.txt`：PySide6 与 shiboken6 自带的许可证声明文件。
- `LICENSES/Apache-2.0.txt`：OpenSSL 的许可证全文。
- `LICENSES/PyYAML-MIT.txt`：PyYAML 的许可证全文。
- `LICENSES/Python-PSF.txt`：Python 运行时及其捆绑组件（含 libffi）的许可证。
- `THIRD_PARTY_LICENSES.md`：本文件。

Qt 的动态库以独立文件形式位于 `_internal/PySide6/`（如 `Qt6Core.dll`、`Qt6Gui.dll`、
`Qt6Widgets.dll`），接收者可直接替换，满足 LGPL-3.0 对"合适的共享库机制"的要求。

## XISF 1.0 规范声明

本软件的 XISF 读写为本仓库自带实现（`xisf_io.py`）。XISF 1.0 规范要求所有副本与
衍生作品附带其版权声明、Copyright Information 与 Disclaimers 三节，以下为该三节
原文；同一份原文也随 `xisf_io.py` 文件头一并分发。

> Copyright © 2014-2026 Pleiades Astrophoto S.L. All rights reserved.
>
> This document may be copied and furnished to others, and derivative works that comment on or
> otherwise explain it or assist in its implementation may be prepared, copied, published, and
> distributed, in whole or in part, without restriction of any kind, provided that the above
> copyright notice, this Copyright Information section and the Disclaimers section below are
> included on all such copies and derivative works. However, this document itself may not be
> modified in any way, including by removing the copyright notice or references to Pleiades
> Astrophoto S.L., except as needed for the purpose of developing any document or deliverable
> produced by Pleiades Astrophoto S.L.
>
> The official version of this format specification document is the English language version on
> the PixInsight.com website. In the event of discrepancies between a translated version and the
> official version, the official version shall govern.
>
> The limited permissions granted above are perpetual and will not be revoked by Pleiades
> Astrophoto S.L. or its successors or assigns.
>
> This document and the information contained herein is provided on an "AS IS" basis and
> PLEIADES ASTROPHOTO S.L. DISCLAIMS ALL WARRANTIES OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING,
> BUT NOT LIMITED TO, ANY ACTUAL OR ASSERTED WARRANTY OF NON-INFRINGEMENT OF PROPRIETARY RIGHTS,
> MERCHANTABILITY, OR FITNESS FOR A PARTICULAR PURPOSE. NEITHER PLEIADES ASTROPHOTO S.L. NOR ITS
> CONTRIBUTORS SHALL BE HELD LIABLE FOR ANY IMPROPER OR INCORRECT USE OF INFORMATION. NEITHER
> PLEIADES ASTROPHOTO S.L. NOR ITS CONTRIBUTORS ASSUME ANY RESPONSIBILITY FOR ANYONE'S USE OF
> INFORMATION PROVIDED BY PLEIADES ASTROPHOTO S.L. IN NO EVENT SHALL PLEIADES ASTROPHOTO S.L. OR
> ITS CONTRIBUTORS BE LIABLE TO ANYONE FOR DAMAGES OF ANY KIND, INCLUDING BUT NOT LIMITED TO,
> COMPENSATORY DAMAGES, LOST PROFITS, LOST DATA OR ANY FORM OF SPECIAL, INCIDENTAL, INDIRECT,
> CONSEQUENTIAL OR PUNITIVE DAMAGES OF ANY KIND WHETHER BASED ON BREACH OF CONTRACT OR WARRANTY,
> TORT, PRODUCT LIABILITY OR OTHERWISE.
>
> TRADEMARKS: Pleiades Astrophoto and PixInsight are trademarks of Pleiades Astrophoto S.L. Other
> product names and trademarks mentioned in this document are the property of their respective
> owners, which are in no way associated or affiliated with Pleiades Astrophoto S.L. Use of these
> names does not imply any co-operation or endorsement.

规范原文（英文版为正式版本）：
<https://pixinsight.com/doc/docs/XISF-1.0-spec/XISF-1.0-spec.html>
