# DWT：Partition-Weighted Stacking（PWS 分区加权叠加）

DWT 是一款深空摄影叠加软件，把一组**已校准、已对齐**的单通道帧合成为一幅线性
Float32 灰度成品。它使用的叠加算法名为 **Partition-Weighted Stacking（PWS，分区加权叠加）**，
由 **D.Cikey** 提出。

- 图形界面 + 命令行两个入口，共用同一份参数定义。
- 全程只有线性运算，成品不做拉伸、归一或截断。
- **读帧、逐帧测星点与叠加都自动分给多个进程**，进程数按可用内存与核数自动定；
  同一份数据下结果与串行逐位相同。
- 依赖均为可商用的开源许可（BSD-3-Clause / PSF-2.0 / LGPL-3.0）。
- 软件 MIT、方法论文 CC BY 4.0。欢迎二次开发，只请保留署名——见文末「许可与署名」。

---

## 一、设计前提：只做叠加

以下工作本软件**不做**，请在别处完成：

- **校准**（暗场 / 平场 / 偏置）——输入必须是已校准帧。
- **对齐**（旋转 + 平移）——输入必须是已对齐帧；软件不做星点配准，也不做
  "未对齐检测"。
- **拉伸 / 去噪 / 合成彩色 / 预览**——成品是线性数据，拉伸请交给后续工具。

**输入**：单通道灰度 XISF 或 FITS（先找 `.xisf`，找不到再找 `.fit*`），按文件名
排序读取。同一目录内不要混入不同目标的帧。

**输出**：线性 Float32 灰度，全程只有线性运算（曝光归一的乘性常数、天空的加性
平移、加权平均），不做任何拉伸、归一或截断。

## 二、算法一句话

三类权重、一个融合式、唯一一次加权平均：

```
W[i,r] = C_i^(A·R_r) · S_i^(B·(1−R_r))        再逐像素归一 mean_i(W) = 1      （论文式 6）
```

- `C_i` —— 单帧清晰度（帧级标量）= FWHM 中位 / 该帧 FWHM，值越大表示该帧越锐利。
- `S_i` —— 单帧信噪比（帧级标量）= σ 中位 / 该帧 σ，值越大表示该帧信噪比越高。
- `R_r` —— 分区权重（逐像素连续场，取值 0～1）：1 = 细节区（星点、星核、尘埃带），
  0 = 朦胧区（空白天区、平滑云气）。它由亮度归一包络映射得到（论文式 8），
  其径向轮廓只含 `r/σ_psf`，与星点亮度无关，因而**换目标无需重新标定**。

效果：**细节区偏向锐帧**（保持解析力），**朦胧区自动趋近等权**（获得 √N 的
信噪比收益）。`A` 为方法参数中唯一需经验标定者；`B` 由逆方差最优性给出，取 2。

## 三、安装

需要 CPython（在 CPython 3.12 上验证）。

```
python -m pip install -r requirements.txt
python -m pip install psutil        # 可选：探测可用内存，用于判断临时帧是否落盘
```

XISF / FITS 读写由本仓库自带的 `xisf_io.py` 完成。

## 四、运行

图形界面：

```
python main.py
```

命令行（批处理）：

```
python main.py --cli --photos <帧目录> --out <输出目录> --tag stack
python main.py --cli --help          # 查看全部参数与默认值
```

界面为三档折叠的参数面板 + 进度卡 + 日志卡：进度按阶段分权重单调推进（读帧 →
星表 → 测星点 → R 场 → 叠加 → 验收），日志按阶段着色。参数会自动记住
（QSettings），版本号变化时一次性刷新为新的默认值；但**素材目录、输出目录、
帧落盘目录不记忆**，每次重开软件都从空白开始选，避免换新数据后仍沿用上一批
数据集的位置（这三项与"这一次要叠哪批数据"绑定，跨次继承只会出错）。

## 五、参数（共 16 项，三档）

### 基础档（3）

| 界面标签 | 命令行 | 默认 | 说明 |
| --- | --- | --- | --- |
| 素材目录 | `--photos` | （空） | 待叠加的帧目录。已校准、已对齐的单通道 XISF / FITS，按文件名排序读取。 |
| 输出目录 | `--out` | （空） | 成品与附带文件的落盘位置，不存在会自动创建。 |
| 成品名 | `--tag` | `stack` | 成品文件名后缀，输出为 `stack_<成品名>.xisf`。 |

### 标准档（5）

| 界面标签 | 命令行 | 默认 | 说明 |
| --- | --- | --- | --- |
| 深化强度 A | `--A` | `20` | 细节区以信噪比换取解析力的强度；方法参数中唯一需经验标定者（论文式 6）。`R=1` 处权重为 `C^A`。减小则天空噪声增长更少、星点略软；增大则星点更细，但天空噪声上升、细节区等效帧数下降。 |
| 排异强度 k | `--rej-k` | `3.5` | 剔除单帧异常像元（卫星、宇宙线、热噪）：逐像素跨帧偏差超过 `k × 1.4826 × MAD + m·R·(信号−天空)` 的帧判为离群并剔除（论文式 11）。`m` 为数据自测的真实分歧上界（细节像素偏差/信号的 P99 跨帧中位数），故排异仅剔除伪迹、不剔除信号。`k = 3.5 ≈ 3.5σ`；0 = 关闭排异。 |
| 帧数上限 | `--limit` | `0` | 只取排序后的前 N 帧。0 = 全部，正式出图用此值；>0 用于在小样本上试参，此处并非"挑好帧"：帧的取舍由权重在叠加内部完成。 |
| 帧落盘目录 | `--frames-dir` | （空） | 帧数据超出内存容量时，临时帧文件的落盘位置。留空 = 自动选盘（项目盘空间足够时使用项目盘，否则选余量最大的盘，需 1.25× 余量）。166 帧全幅约需 40 GiB。 |
| 保留帧文件 | `--keep-frames` | 关 | 运行结束后不删除临时帧文件。默认关闭；反复调参时启用，可省去重新落盘数十 GiB 的时间。 |

### 高级档（8）

| 界面标签 | 命令行 | 默认 | 说明 |
| --- | --- | --- | --- |
| 裁剪边长 | `--crop` | `0` | 只叠画面中间 N×N 像素。0 = 全幅，正式出图用此值；>0 仅用于快速试参，成品不是完整画面。 |
| 裁剪中心 | `--center` | （空） | 裁剪窗中心坐标 `(y,x)`。留空 = 自动定位最亮的延展源（32 px 分块中值法）。顺序为 y,x（先行后列）。 |
| 信噪比幂 B | `--B` | `2` | 朦胧区以帧数换取噪声抑制的强度（论文式 6）。加权平均方差最小 ⇔ 权重 ∝ 1/σ² ⇔ S²，故 B = 2；除实验外不建议修改。 |
| 包络窗 | `--env-win-mult` | `2` | 局部极大窗宽度 = 该值 × 2σ_psf，即"与多大范围内的最亮处比较"。2.0 对应约 4σ_psf 见方。增大则更宽容，减小则更贴合星点。 |
| 包络噪声项 | `--env-eps` | `8` | 分母加上 该值 × σ（论文式 8 的 εσ 项），使空白天区的 R 趋近 0（完全按信噪比加权）。增大则把空白天区的 R 压得更低、天空噪声增长更少。 |
| 包络形状幂 | `--env-p` | `1` | >1 收窄支撑（R 更贴近核心），<1 展宽。 |
| R 下限 | `--r-floor` | `0` | 全图保留的最小清晰度偏好。增大则空白天区也偏向锐帧，星周"外圈亮环"变浅；代价是天空等效帧数下降。0 = 关闭。 |
| 星表星数 | `--n-star` | `120` | 测帧间清晰度用的固定星表规模。在信噪比最高的一帧上一次选星（未饱和、孤立、最亮），之后全部帧测量同一批位置。 |

## 六、输出

落盘四件套（`<tag>` 即"成品名"）：

| 文件 | 内容 |
| --- | --- |
| `stack_<tag>.xisf` | 成品：线性 Float32 灰度。 |
| `weights_<tag>.txt` | 逐帧权重表，列依次为 `idx fwhm_px C_sharp sigma S_snr`。 |
| `R_<tag>.npy` | 分区权重场（Float32，与成品同尺寸）。 |
| `neff_<tag>.npy` | 等效帧数场（每个像素实际用上多少帧的信噪比）。 |

## 七、目录结构

```
main.py                入口：python main.py（--cli 走命令行）
core/
  pws_params.py        参数定义：PwsParams 数据类 + PARAMS 说明表
  pws.py               PWS 引擎（含日志 / 进度 / 取消三个钩子）
ui/
  theme.qss            浅色扁平主题
  logview.py           日志视图（按阶段着色）
  panel.py             参数面板（三档折叠，控件由 PARAMS 生成）
  worker.py            QThread 跑引擎 + 进度 / 取消
  app.py               主窗口
xisf_io.py             XISF / FITS 读写
DWT_DetailCore.py      帧读取（FITS / XISF）与延展源定位
分区权重叠加法(PWS)的原理与实现.md   方法论文：PWS 的原理与推导
```

界面控件、引擎入参、命令行 argparse 三处都由 `core/pws_params.py` 生成。

## 八、打包成可直接运行的软件（Windows）

用 PyInstaller 生成**文件夹形式**的产物（`--onedir`）。不要用 `--onefile`：Qt 采用
LGPL-3.0，接收者必须能够替换 Qt 库，而单文件包把 Qt 二进制都塞进一个自解压
可执行文件里，不满足这一条。

在仓库根执行。`--paths` 必须给：`core/` 与 `ui/` 不在默认模块搜索路径上，漏掉会
让 PyInstaller 解析不到本软件自己的模块，连带漏掉 PySide6。

```
python -m pip install pyinstaller
python -m PyInstaller --noconfirm --clean --windowed --name DWT ^
    --paths . --paths core --paths ui ^
    --add-data "ui/theme.qss;." ^
    --collect-all photutils ^
    --exclude-module tkinter --exclude-module matplotlib ^
    --exclude-module torch --exclude-module torchvision ^
    --exclude-module skimage --exclude-module sklearn ^
    --exclude-module pycocotools --exclude-module av ^
    --exclude-module imageio --exclude-module cupy --exclude-module cupyx ^
    --exclude-module numba --exclude-module pandas ^
    --exclude-module PIL --exclude-module lxml ^
    --exclude-module cryptography --exclude-module shapely ^
    --exclude-module tqdm --exclude-module certifi ^
    --exclude-module Cython --exclude-module charset_normalizer ^
    --exclude-module markupsafe --exclude-module lz4 ^
    main.py
```

`--exclude-module` 那一串是给"环境里还装着别的科学计算包"的机器用的：本软件不依赖
它们，不排除就会被拖进包里，产物体积成倍上涨。

入口 `main.py` 里那句 `multiprocessing.freeze_support()` **不要删**：引擎用多进程并行，
打包后子进程要靠它引导回多进程引导程序，删掉会让每次并行都重新打开一遍界面。

产物在 `dist/DWT/`（要放到别处就加 `--distpath`）。双击 `DWT.exe` 启动；Qt 的动态库
以独立文件形式放在 `_internal/PySide6/` 下（`Qt6Core.dll`、`Qt6Gui.dll`、
`Qt6Widgets.dll` 等），接收者可以直接替换，这正是 LGPL-3.0 要求的"合适的共享库机制"。
打包出的是窗口模式，命令行入口请用源码跑：`python main.py --cli …`。

再分发时请一并带上 `LICENSE`、`LICENSES/`（LGPL-3.0 与 GPL-3.0 全文、Qt 声明文件）
与 `THIRD_PARTY_LICENSES.md`（见下一节）。

## 九、许可与署名

作者：**D.Cikey**

### 本软件

以 **MIT 许可证**发布，全文见 [LICENSE](LICENSE)。

### 如果你用到了它

本软件实现的是作者提出的**分区权重叠加法（Partition-Weighted Stacking，PWS）**，
方法论文以 CC BY 4.0 单独发布，全文见 [分区权重叠加法(PWS)的原理与实现.md](<docs/分区权重叠加法(PWS)的原理与实现.md>)（<https://github.com/DDCikey/Partition-Weighted-Stacking/tree/main/docs>）；PDF 版可在微信公众号「小丁的星空」下载。
欢迎 fork、二次开发、改编、翻译乃至商用，**无需申请、
无需付费**。只请在你的项目 README、文章或视频简介里留一句署名，下面这句可以直接复制：

> 本项目的方法基于 **D.Cikey** 提出的**分区权重叠加法（Partition-Weighted
> Stacking，PWS）**，原文见 <https://github.com/DDCikey/Partition-Weighted-Stacking/tree/main/docs>；PDF 版可在微信公众号「小丁的星空」下载。

### 第三方开源组件

本软件使用了若干第三方开源组件，均为可商用许可，各自遵循原许可。它们的版权声明、
许可证与用途，以及 PySide6 / Qt 的 LGPL-3.0 使用方式，见
[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)。

XISF 1.0 规范要求所有副本与衍生作品附带其版权声明、Copyright Information 与
Disclaimers 三节；该三节原文同样收录在 [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)
并随 `xisf_io.py` 文件头一并分发。
