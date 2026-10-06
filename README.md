# DWT：Partition-Weighted Stacking（PWS 分区加权叠加）

DWT 是一款深空摄影叠加软件，把一组**已校准、已对齐**的单通道帧合成为一幅线性
Float32 灰度成品。它使用的叠加算法名为 **Partition-Weighted Stacking（PWS，分区加权叠加）**，
由 **D.Cikey** 提出。

- 图形界面 + 命令行两个入口，共用同一份参数定义。
- 全程只有线性运算，成品不做拉伸、归一或截断。
- **读帧、逐帧测星点与叠加都自动分给多个进程**，进程数按可用内存与核数自动定；
  同一份数据下结果与串行逐位相同。
- 依赖均为可商用的开源许可（BSD-3-Clause / PSF-2.0 / MIT）。
- 软件 MIT、方法论文 CC BY 4.0。欢迎二次开发，只请保留署名——见文末「许可与署名」。

---测试软件下载：https://pan.baidu.com/s/1b_jnoBnct1Y5kaiAv0Px5g?pwd=rnst

## 一、设计前提：只做叠加

以下工作本软件**不做**，请在别处完成：

- **校准**（暗场 / 平场 / 偏置）——输入必须是已校准帧。
- **对齐**（旋转 + 平移）——输入必须是已对齐帧；软件不做星点配准，也不做
  "未对齐检测"。
- **拉伸 / 去噪 / 合成彩色 / 预览**——成品是线性数据，拉伸请交给后续工具。

**输入**：单通道灰度 XISF 或 FITS（先找 `.xisf`，找不到再找 `.fit*`），按文件名
排序读取。同一目录内不要混入不同目标的帧。

**输出**：线性 Float32 灰度，全程只有线性运算（曝光归一的乘性常数、量纲回乘的
乘性常数、天空的加性平移、加权平均），不做任何拉伸、归一或截断。成品量纲与素材
一致（全部同曝光时即输入帧量纲；混合曝光时为中位曝光量纲），可直接与其他软件
在同一数据上的成品对比。

## 二、算法一句话

三类权重、一个融合式、唯一一次加权平均：

```
W[i,r] = C_i^(A·R_r) · S_i^(B·(1−R_r))        再逐像素归一 mean_i(W) = 1      （论文式 6）
```

- `C_i` —— 单帧清晰度（帧级标量）= FWHM 中位 / 该帧 FWHM，越锐越大。
- `S_i` —— 单帧信噪比（帧级标量）= σ 中位 / 该帧 σ，越干净越大。
- `R_r` —— 分区权重（逐像素连续场，取值 0～1）：1 = 细节区（星点、星核、尘埃带），
  0 = 朦胧区（空白天区、平滑云气）。它由"亮度归一包络"映射得到（论文式 8），
  径向轮廓只含 `r/σ_psf`，与目标亮度无关 —— 换目标不必改参数。

效果：**细节区偏向锐帧**（保住解析力），**朦胧区自动趋近等权**（吃满 √N 的
信噪比收益）。`A` 是唯一的风格旋钮，也是唯一需要标定的参数；`B` 由逆方差最优直接取 2，
不需要标定。

## 三、安装

需要 CPython。本项目在 CPython 3.12 上开发与测试。

```
python -m pip install -r requirements.txt
python -m pip install psutil        # 可选：用于探测可用内存，决定临时帧是否落盘
```

图形界面使用仓库自带的 `WevvMoldGUI_for_python`（自研框架，MIT，无需 pip 安装），
文件对话框与剪贴板走 Python 标准库 tkinter。

XISF / FITS 读写由本仓库自研的 `xisf_io.py` 完成，**不需要**安装 PyPI 上的
`xisf` 包（该包为 GPL-3）。

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

界面为深色顶栏 + 四页视图（顶栏分段导航切换）：**素材**页 = 取景框拖放 +
来源行卡清单（目录与单张可混用）；**参数 / 高级**页 = 双列参数卡（悬停出说明）；
**监控**页 = 左侧圆环大百分比 + 阶段说明、右侧终端式日志（按阶段着色）。
进度按阶段分权重单调推进（读帧 → 星表 → 测星点 → R 场 → 叠加条带 → 验收）。
参数自动记住（`%APPDATA%\DWT\settings.json`），默认工作点版本号（`PARAMS_VER`）
变化时一次性刷新为新的定稿值；素材 / 输出 / 帧落盘目录**不记忆**，每次从空白
开始，避免换新数据后沿用上一批数据集的位置（这三项与"这一次要叠哪批数据"
绑定，跨次继承只会出错）。

## 五、参数（共 17 项，三档）

### 基础档（3）

| 界面标签 | 命令行 | 默认 | 说明 |
| --- | --- | --- | --- |
| 素材 | `--photos` | （空） | 待叠加的帧来源，目录与单张可混用（多行）。已校准、已对齐的单通道 XISF / FITS；目录内按文件名排序读取。同一目标不同夜晚的帧可混叠，重复声明的文件只算一帧。 |
| 输出目录 | `--out` | （空） | 成品与附带文件的落盘位置，不存在会自动创建。 |
| 成品名 | `--tag` | `stack` | 成品文件名后缀，输出为 `stack_<成品名>.xisf`；留空则随素材目录名自动生成。 |

### 标准档（5）

| 界面标签 | 命令行 | 默认 | 说明 |
| --- | --- | --- | --- |
| 深化强度 A | `--A` | `20` | 细节区愿意用多少信噪比换解析力，唯一的风格旋钮（论文式 6）。`R=1` 处权重为 `C^A`。定稿值 20 在 NGC7331 与 NGC6888 上共用一套参数通过验收；12 更保守（天空噪声只涨 2～4%、星点略软）；30 更锐（星点再细 0.1～0.3px，但天空噪声涨 6～7%、细节区等效帧数掉到 4 以下）。 |
| 排异强度 k | `--rej-k` | `3.5` | 剔除单帧异常像元（卫星、宇宙线、热噪）：逐像素跨帧偏差超过 `k × 1.4826 × MAD + m·R·(信号−天空)` 的帧被判离群并剔除（论文式 11）——`m` 从数据实测（真实视宁度分歧上界），故排异只打伪迹、不打信号。`3.5 ≈ 3.5σ` 为定稿值；0 = 关闭排异（仅用于对照）。 |
| 帧数上限 | `--limit` | `0` | 只取排序后的前 N 帧。0 = 全部（正式出图）。>0 用于先在小样本上试参数，**不是"挑好帧"**：帧的取舍由权重在叠加内部完成。 |
| 帧落盘目录 | `--frames-dir` | （空） | 帧多到装不进内存时的临时帧文件位置。留空 = 自动挑盘（项目盘够用就用项目盘，否则挑余量最大的盘，需 1.25× 余量）。166 帧全幅约需 40 GiB。 |
| 保留帧文件 | `--keep-frames` | 关 | 跑完不删除临时帧文件。默认关闭；反复调参时勾上，可省掉重新落盘几十 GiB 的时间。 |

### 高级档（9）

| 界面标签 | 命令行 | 默认 | 说明 |
| --- | --- | --- | --- |
| 裁剪边长 | `--crop` | `0` | 只叠画面中间 N×N 像素。0 = 全幅（正式出图）；>0 只用于快速试参数，成品不是完整画面。 |
| 裁剪中心 | `--center` | （空） | 裁剪窗中心坐标 `(y,x)`。留空 = 自动定位最亮的延展源（32px 分块中值法）。注意顺序是先行后列。 |
| 信噪比幂 B | `--B` | `2` | 朦胧区用多少帧数换噪声（论文式 6）。`B=2` 不是调出来的：加权平均方差最小 ⇔ 权重 ∝ 1/σ² ⇔ S²。除非做实验，否则不要动。 |
| 包络窗 | `--env-win-mult` | `2` | 局部极大窗宽度 = 该值 × 2σ_psf，决定"和多大范围内的最亮处比"。2.0 对应约 4σ_psf 见方。调大更宽容（R 覆盖更广），调小更贴合星点。 |
| 包络噪声项 | `--env-eps` | `8` | 分母加上 该值 × σ（论文式 8 的 εσ 项），使空白天区的 R 趋近 0（严格吃满信噪比）。定稿 8：R>0.5 只占 1.2%～1.7% 像素、天空 σ 只涨 2%～3%；调到 3 会让天空 R 抬到 3% 像素、噪声涨 4%～6%。 |
| 包络形状幂 | `--env-p` | `1` | >1 收窄支撑（R 更贴核心），<1 放宽。1.0 即定稿。 |
| R 下限 | `--r-floor` | `0` | 全图保留的最小清晰度偏好。抬高它 → 空白天区也偏向锐帧 → 星周"外圈亮环"变浅；代价是天空等效帧数下降。0 = 关闭。 |
| 星表星数 | `--n-star` | `120` | 测帧间清晰度用的固定星表规模。在信噪比最好的一帧上一次性选星（未饱和、孤立、最亮），之后所有帧量同一批位置。中位数在 100 颗以上已稳定。 |
| 少数派闸门 | `--rej-gate` | `0.33` | 同一像素被排帧数超出"噪声期望误排数"的部分超过该比例，即判"不是孤立离群"，该像素不排异——防止把多数帧共有的真实结构（星核锐 / 糊双峰、配准黑边）当伪迹排掉。默认 1/3 = 只排少数派；调大排得更狠，调小更保守。闸门已扣除噪声期望误排数，与 k 解耦。 |

## 六、输出

落盘四件套（`<tag>` 即"成品名"）：

| 文件 | 内容 |
| --- | --- |
| `stack_<tag>.xisf` | 成品：线性 Float32 灰度。 |
| `weights_<tag>.txt` | 逐帧权重表，列依次为 `idx fwhm_px C_sharp sigma S_snr`。 |
| `R_<tag>.npy` | 分区权重场（Float32，与成品同尺寸）。 |
| `neff_<tag>.npy` | 等效帧数场（每个像素实际用上多少帧的信噪比）。 |

## 七、自检

```
python tests/smoke_ui.py         # 界面冒烟：控件与参数一一对应、读写闭环、日志着色
```

仓库内另有两项引擎回归——`tests/check_equiv.py`（与定稿基准逐位比对）与
`tests/check_parallel.py`（并行与串行逐位比对）——它们依赖开发环境里的定稿
测试台 `test_tools/DWT_stack_v2.py` 与观测数据，这些**不随本仓库分发**，
在公开仓库中无法直接运行。

## 八、目录结构

```
main.py                入口：python main.py（--cli 走命令行）
core/
  pws_params.py        参数唯一真源：PwsParams 数据类 + PARAMS 说明表
  pws.py               PWS 引擎（含日志 / 进度 / 取消三个钩子）
ui/
  theme.py             明暗令牌与动效常量（页签 / 悬停 / 滑块时长、定时器节拍）
  style.py             浅色扁平主题：调色板与绘制助手（圆角 / 阴影 / 文字测量）
  widgets.py           自绘控件库（分段导航 / 圆钮 / 环形进度 / 输入框 / 微调框 /
                       下拉 / 源列表 …）
  logview.py           日志视图（按阶段着色）
  panel.py             四页视图（素材 / 参数 / 高级 / 监控），控件由 PARAMS 生成
  worker.py            threading 跑引擎 + 进度 / 取消
  app.py               主窗口（布局 / 事件路由 / 渲染 / 设置持久化）
tests/
  smoke_ui.py          界面冒烟自检
  check_equiv.py       引擎等价性回归（需内部测试台与观测数据）
  check_parallel.py    并行一致性回归（需内部测试台与观测数据）
xisf_io.py             XISF / FITS 读写（自研实现）
DWT_DetailCore.py      读帧、曝光归一、星点检测与 PSF 拟合
WevvMoldGUI_for_python/  图形界面框架（自研，MIT；Python 绑定 + 核心 DLL）
docs/
  分区权重叠加法(PWS)的原理与实现.md   方法论文：PWS 的原理与推导
```

参数只有一份定义（`core/pws_params.py`），界面控件、引擎入参、命令行 argparse
三处都由它生成，不存在"同一口径两处实现"的漂移。

## 九、打包成可直接运行的软件（Windows）

用 PyInstaller 生成**文件夹形式**的产物（`--onedir`）。在仓库根执行；
`--paths` 必须给：`core/` 与 `ui/` 不在默认模块搜索路径上，漏掉会让 PyInstaller
解析不到本软件自己的模块。

```
python -m pip install pyinstaller
python -m PyInstaller --noconfirm --clean --windowed --name DWT ^
    --paths . --paths core --paths ui ^
    --paths WevvMoldGUI_for_python/python ^
    --add-binary "WevvMoldGUI_for_python/python/WevvMoldCore.dll;." ^
    --add-binary "WevvMoldGUI_for_python/python/_wevvmold.pyd;." ^
    --collect-all photutils ^
    --exclude-module matplotlib ^
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

注意两点：

- `WevvMoldCore.dll` 与 `_wevvmold.pyd` 必须用 `--add-binary` 放进产物根目录
  （`_internal/`），两者**必须在同一目录**——扩展模块加载时按同目录定位核心 DLL。
- **不能排除 tkinter**：界面用它做文件对话框与剪贴板，排除会直接弄坏这两处。

`--exclude-module` 那一串是给"环境里还装着别的科学计算包"的机器用的：本软件不依赖
它们，不排除就会被拖进包里，产物体积成倍上涨。

入口 `main.py` 里那句 `multiprocessing.freeze_support()` **不要删**：引擎用多进程并行，
打包后子进程要靠它引导回多进程引导程序，删掉会让每次并行都重新打开一遍界面。

产物在 `dist/DWT/`（要放到别处就加 `--distpath`）。双击 `DWT.exe` 启动。
打包出的是窗口模式，命令行入口请用源码跑：`python main.py --cli …`。

再分发时请一并带上 `LICENSE`（本软件 MIT）、`WevvMoldGUI_for_python/LICENSE`
（自研框架的 MIT 全文）与 `THIRD_PARTY_LICENSES.md`。

## 十、许可与署名

作者：**D.Cikey**

### 本软件

以 **MIT 许可证**发布，全文见 [LICENSE](LICENSE)。

### 《分区权重叠加法》论文

方法论文以 **CC BY 4.0** 发布——署名即可自由转载、改编、商用，无需另行申请。
全文见 [docs/分区权重叠加法(PWS)的原理与实现.md](<docs/分区权重叠加法(PWS)的原理与实现.md>)；
原文与 PDF 下载渠道：<https://github.com/DDCikey/Partition-Weighted-Stacking/tree/main/docs>；
PDF 版可在微信公众号「小丁的星空」下载。

### 如果你用到了它

欢迎 fork、二次开发、改编、翻译乃至商用，**无需申请、无需付费**。只请在你的项目
README、文章或视频简介里留一句署名，下面这句可以直接复制：

> 本项目的方法基于 **D.Cikey** 提出的**分区权重叠加法（Partition-Weighted
> Stacking，PWS）**，原文见 <https://github.com/DDCikey/Partition-Weighted-Stacking/tree/main/docs>；PDF 版可在微信公众号「小丁的星空」下载。

### 第三方开源组件

本软件使用了若干第三方开源组件，均为可商用许可，各自遵循原许可；自研图形界面
框架 WevvMoldGUI（MIT）随本仓库整体分发。各组件的版权声明、许可证与用途，见
[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)。

XISF 1.0 规范要求所有副本与衍生作品附带其版权声明、Copyright Information 与
Disclaimers 三节；该三节原文同样收录在 [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)
并随 `xisf_io.py` 文件头一并分发。
