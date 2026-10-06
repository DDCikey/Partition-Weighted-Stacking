# 第三方组件说明（THIRD PARTY NOTICES）

本项目源码**未内嵌任何第三方源码、数据或字体文件**。构建与运行依赖以下外部组件，
它们均随各自的官方载体分发，本项目不重复打包、不修改、不随附其二进制：

## 1. Windows SDK 系统组件

C++ 核心链接以下微软 Windows 操作系统组件（随 Windows 及 Windows SDK 分发，遵循微软系统组件许可）：

| 组件 | 头文件 | 用途 |
|---|---|---|
| Direct2D | `d2d1.h` / `d2d1_1.h` / `d2d1effects.h` | 2D 渲染与图像效果 |
| DirectWrite | `dwrite.h` | 文本排版与绘制 |
| DXGI / Direct3D 11 | `dxgi1_2.h` / `d3d11.h` | 交换链与设备互操作 |
| WIC | `wincodec.h` | 图像编解码（BMP/PNG/JPEG/TIFF/GIF） |
| DWM | `dwmapi.h` | 自定义窗口边框 |
| Win32 | `windows.h` / `windowsx.h` | 窗口、消息、输入 |

## 2. Python 运行时（CPython）

Python 绑定（`_wevvmold` 扩展模块）构建时链接 CPython 开发组件，运行时依赖 Python 解释器。
CPython 遵循 [PSF License Agreement](https://docs.python.org/3/license.html)（PSF-2.0），
由使用者自行安装，本项目不分发解释器或其任何部分。

## 3. 构建工具链

| 工具 | 许可 | 说明 |
|---|---|---|
| CMake | BSD-3-Clause | 构建系统，仅构建期使用 |
| MSVC | 微软专有许可 | C++ 编译工具链，仅构建期使用 |

## 4. 字体声明

本项目不指定、不分发任何字体文件；界面文本一律使用系统默认字体，
不产生字体版权风险。若使用者自行配置字体，须自证其使用权。

## 5. 登记约定

后续若引入任何第三方组件（源码级或随分发），必须在本文件登记其名称、版本、
来源与完整许可文本，并核实其与 MIT 许可的兼容性。
