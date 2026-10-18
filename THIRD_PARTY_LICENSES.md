# 第三方开源组件与版权声明

本软件使用了下列第三方开源组件。它们**不在**本软件的 MIT 授权
范围内，各自遵循原许可；再分发时请保留其版权声明与许可证（随各组件包一并分发）。

| 组件 | 本软件中的用途 | 版权声明 | 许可证 |
| --- | --- | --- | --- |
| NumPy | 数组与逐像素数值运算（全栈） | Copyright (c) 2005-2025, NumPy Developers. All rights reserved. | BSD-3-Clause（另含 0BSD / MIT / Zlib / CC0-1.0 的少量组件） |
| SciPy | 高斯 / 均值滤波、KD 树、秩统计（`scipy.ndimage` / `spatial.cKDTree` / `stats`） | Copyright (c) 2001-2002 Enthought, Inc. 2003, SciPy Developers. All rights reserved. | BSD-3-Clause（全文见 [LICENSES/scipy-LICENSE.txt](LICENSES/scipy-LICENSE.txt)，其中含随包 OpenBLAS 的版权声明） |
| Astropy | FITS 头读写、WCS 承载、σ 剪裁统计（`io.fits` / `wcs` / `stats`）；随包组件 erfa（pyerfa，坐标系底层）与 astropy_iers_data（地球自转参数表） | Copyright (c) 2011-2024, Astropy Developers. All rights reserved.（pyerfa：Copyright (c) 2011-2023, PyERFA Developers；astropy_iers_data：Copyright (c) 2023, Astropy Developers） | BSD-3-Clause（另含随包的 AURA / ERFA / EXPAT 等许可）；pyerfa 全文见 [LICENSES/pyerfa-LICENSE.rst](LICENSES/pyerfa-LICENSE.rst)，astropy_iers_data 全文见 [LICENSES/astropy-iers-data-LICENSE.rst](LICENSES/astropy-iers-data-LICENSE.rst) |
| Photutils | 星点检测与 PSF 拟合（`detection.DAOStarFinder` / `detection.find_peaks` / `psf.fit_fwhm`） | Copyright (c) 2011-2026, Photutils Developers. All rights reserved. | BSD-3-Clause |
| psutil（可选） | 探测可用内存，决定临时帧是否落盘 | Copyright (c) 2009, Jay Loden, Dave Daeschler, Giampaolo Rodola. All rights reserved. | BSD-3-Clause（全文见 [LICENSES/psutil-LICENSE.txt](LICENSES/psutil-LICENSE.txt)） |
| OpenBLAS | NumPy 与 SciPy 的线性代数后端（`numpy.libs/`、`scipy.libs/` 下的 `libscipy_openblas*.dll`） | Copyright (c) 2011-2014, The OpenBLAS Project. All rights reserved. | BSD-3-Clause（版权声明与全文见 [LICENSES/scipy-LICENSE.txt](LICENSES/scipy-LICENSE.txt) 的 OpenBLAS 节） |
| zstandard | astropy 的 ZSTD 压缩路径所需模块，随 astropy 导入链被 PyInstaller 收集（`_internal/zstandard/`） | Copyright (c) 2016, Gregory Szorc. All rights reserved. | BSD-3-Clause（全文见 [LICENSES/zstandard-LICENSE.txt](LICENSES/zstandard-LICENSE.txt)） |
| PyYAML | astropy 表格元数据（ECSV/YAML 头）读写的必需依赖，随 astropy 导入链加载 | Copyright (c) 2017-2021 Ingy döt Net / Copyright (c) 2006-2016 Kirill Simonov | MIT（全文见 [LICENSES/PyYAML-MIT.txt](LICENSES/PyYAML-MIT.txt)） |
| OpenSSL（libssl-3 / libcrypto-3） | Python 标准库 `ssl` / `hashlib` 扩展的底层加密实现（随引擎导入加载；本软件自身不发起任何联网） | Copyright 1998-2024 The OpenSSL Authors. All rights reserved. | Apache-2.0（全文见 [LICENSES/Apache-2.0.txt](LICENSES/Apache-2.0.txt)） |
| Python 标准库（含 `ctypes` 及其捆绑的 libffi） | zlib / xml.etree / math / argparse / concurrent.futures / threading / tkinter / io … | Copyright (c) Python Software Foundation.（捆绑组件的版权声明含于同一文件） | PSF-2.0（全文见 [LICENSES/Python-PSF.txt](LICENSES/Python-PSF.txt)；libffi 许可亦收录于该文件） |
| Tcl/Tk | tkinter 的底层 GUI 工具包（文件对话框、剪贴板；`tcl86t.dll` / `tk86t.dll` / `tcl8` / `_tcl_data` / `_tk_data`） | Copyright (c) The Regents of the University of California, Sun Microsystems, Inc., Scriptics Corporation, ActiveState Corporation, Apple Inc. and other parties. | Tcl/Tk License（BSD 式；全文见 [LICENSES/Tcl-Tk.txt](LICENSES/Tcl-Tk.txt)） |

## 图形界面框架 WevvMoldGUI

图形界面框架 **WevvMoldGUI**（C++ 核心 + Python 绑定）与本软件同作者，
以 **MIT 许可证**随本仓库分发，许可证全文见
[WevvMoldGUI_for_python/LICENSE](WevvMoldGUI_for_python/LICENSE)。

## Microsoft Visual C++ 运行时

打包产物 `_internal/` 内含 Microsoft Visual C++ 运行时 DLL（`VCRUNTIME140.dll`、
`VCRUNTIME140_1.dll`、`MSVCP140.dll`，以及 NumPy 自带的
`msvcp140-a4c2…dll`），用于支撑 CPython 与 NumPy / SciPy 的 C 扩展。这些文件按
Microsoft Visual Studio 可再分发组件条款随本软件分发，版权归 Microsoft
Corporation 所有，不改变本软件的 MIT 许可。

## BSD-3-Clause 许可证全文

下列组件在打包产物中没有随各自包附带许可证文件，其许可证全文随本分发包收录：

- **SciPy**（含 `scipy.libs/` 下的 OpenBLAS）：[LICENSES/scipy-LICENSE.txt](LICENSES/scipy-LICENSE.txt)
- **psutil**：[LICENSES/psutil-LICENSE.txt](LICENSES/psutil-LICENSE.txt)
- **erfa（pyerfa）**：[LICENSES/pyerfa-LICENSE.rst](LICENSES/pyerfa-LICENSE.rst)
- **astropy_iers_data**：[LICENSES/astropy-iers-data-LICENSE.rst](LICENSES/astropy-iers-data-LICENSE.rst)
- **zstandard**：[LICENSES/zstandard-LICENSE.txt](LICENSES/zstandard-LICENSE.txt)

NumPy、Astropy、Photutils 的许可证已随包分发在 `_internal/` 下各组件的
`*.dist-info/licenses/`（Astropy 的该目录同时收录其随包组件 AURA、ERFA、
EXPAT、WCSLIB 等的许可全文）。OpenBLAS 的版权声明（Copyright (c) 2011-2014,
The OpenBLAS Project）与全文见 `LICENSES/scipy-LICENSE.txt` 的 OpenBLAS 节。

各文件均为标准 BSD 3-Clause 条款，正文如下（版权人见上表与对应文件）：

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

## 二进制再分发时需要附带什么

Python 侧的许可证由打包过程自动放入 `_internal/` 下各组件的 `*.dist-info/licenses/`
（NumPy、Astropy、Photutils 等）。除此之外请随包保留：

- `LICENSE`：本软件的 MIT 许可证。
- `WevvMoldGUI_for_python/LICENSE`：图形界面框架的 MIT 许可证全文。
- `WevvMoldGUI_for_python/THIRD_PARTY_NOTICES.md`：图形界面框架所链接的 Windows
  系统组件与构建工具说明。
- `LICENSES/Apache-2.0.txt`：OpenSSL 的许可证全文。
- `LICENSES/PyYAML-MIT.txt`：PyYAML 的许可证全文。
- `LICENSES/Python-PSF.txt`：Python 运行时及其捆绑组件（含 libffi）的许可证。
- `LICENSES/Tcl-Tk.txt`：tkinter 底层 Tcl/Tk 运行时的许可证全文。
- `LICENSES/scipy-LICENSE.txt`：SciPy 及其捆绑 OpenBLAS 的许可证全文。
- `LICENSES/psutil-LICENSE.txt`：psutil 的许可证全文。
- `LICENSES/pyerfa-LICENSE.rst`：erfa（pyerfa）的许可证全文。
- `LICENSES/astropy-iers-data-LICENSE.rst`：astropy_iers_data 的许可证全文。
- `LICENSES/zstandard-LICENSE.txt`：zstandard 的许可证全文。
- `THIRD_PARTY_LICENSES.md`：本文件。

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
