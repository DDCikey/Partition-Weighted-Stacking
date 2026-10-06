# 织铸 WevvMold GUI（WevvMoldGUI）

Windows 原生轻量级 GUI 框架：C++ 核心 + Python 绑定。单窗口即时渲染模型，控件即对象，
事件归宿主，绘制归框架——不引入消息映射宏、不依赖重量级 UI 运行时，构建后仅一个 DLL 与一个扩展模块。

## 特性

- **窗口与事件**：DWM 自定义边框窗口（原生缩放/最大化/Aero Snap/系统菜单），17 类事件统一派发
- **渲染**：Direct2D / Direct3D 互操作呈现，即时模式绘制原语（矩形/椭圆/多边形/文本对齐九宫）
- **动画与缓动**：补间/关键帧/循环/往复，10 种缓动曲线 × 3 种形态，线程时钟节拍（不依赖 WM_TIMER）
- **基础控件**：15 种——标签/分隔线/图片/进度条/按钮/开关/复选/单选/文本框/文本域/微调/滑块/滚动条/列表/下拉
- **高级控件**：树/表格/颜色选择/日期选择/消息框/向导
- **容器与图层**：容器/页面栈/页签/分割条（可拖）/停靠区/浮动面板/弹出层/菜单/滚动容器/多选/拖放协议
- **图像**：BMP/PNG/JPEG/TIFF/GIF 解码，几何变换与像素滤镜链，图片控件四种拉伸模式
- **图标**：矢量形状原语（7 种）任意尺寸锐利绘制 + 多 DPI 位图档位
- **自由绘制画布**：五工具矢量化笔画、选择变换（翻转/缩放/层级/删除）、撤销重做、无限视口（缩放/平移）
- **图表**：11 种——折线/柱状/堆叠柱状/面积/饼/环/散点/雷达/仪表盘/热力图/K 线，30ms 批量快照动画
- **统计表**：虚拟化渲染（万行级滚动流畅）、列头排序、行内统计（SUM/AVG/MIN/MAX/COUNT）、色阶映射
- **特效引擎**：转场（淡入/滑轨/弹出/翻转）、控件级（涟漪/发光/呼吸/扫光/抖动）、合成级（扫描线/噪点）、粒子系统、帧预算自适应降级

## 环境要求

- Windows 10 及以上
- CMake 3.16+、MSVC（C++17，源码含 UTF-8 中文注释需 `/utf-8`）
- Python 3.8+（仅构建 Python 绑定时需要，含开发组件）

## 构建

```bat
cmake -S . -B build
cmake --build build --config Release
```

产物统一输出至 `build\dist\Release\`：

| 目录 | 内容 |
|---|---|
| `cpp\` | `WevvMoldCore.dll`（C++ 核心动态库）及导入库 |
| `python\` | `_wevvmold.pyd`（CPython 扩展模块） |

### Python 绑定部署

将 `build\dist\Release\python\_wevvmold.pyd` 与 `build\dist\Release\cpp\WevvMoldCore.dll`
复制到 `WevvMoldGUI_for_Python\wevvmold\` 包目录内（扩展模块加载时会自动定位同目录的核心 DLL），随后：

```python
import wevvmold
print(wevvmold.get_version())
```

## 最小示例

```python
from wevvmold import WevvMoldWindow, WevvMoldControl, color

win = WevvMoldWindow(title="Hello 织铸", width=480, height=320)
btn = WevvMoldControl("BUTTON", "ok")
btn.set_rect(180, 130, 300, 170)     # 控件缺省零矩形，呈现前必须设置
btn.set_text("点我")

def on_render(rc):
    rc.fill_rect(0, 0, 480, 320, color(24, 27, 37))
    rc.render_control(btn, 180.0, 130.0)   # 控件画在窗口坐标 (180, 130)

def on_event(ev):
    t = ev["type"]
    if t == WevvMoldWindow.EVENT["POINTER_BUTTON_DOWN"]:
        btn.dispatch("PRESS", x=ev["x"] - 180, y=ev["y"] - 130)
    elif t == WevvMoldWindow.EVENT["POINTER_BUTTON_UP"]:
        btn.dispatch("RELEASE", x=ev["x"] - 180, y=ev["y"] - 130)
        if btn.poll("CLICKED"):
            btn.set_text("已点击")
    elif t == WevvMoldWindow.EVENT["KEY_DOWN"] and ev.get("key_code") == 0x1B:
        win.request_close()

win.on_render = on_render
win.on_event = on_event
win.show()
win.run()
win.close()
```

事件只到达 `on_event`，命中转发与局部坐标换算是宿主的责任；一次性结果用 `poll_*` 轮询。
更多要点（非拥有 API 保活、容器初始化、特效钩子自绘等）见接口文档 §17。

## 目录结构

```
WevvMoldGUI/
├── CMakeLists.txt              # 顶层聚合工程
├── 接口文档.md                  # Python API 参考（含使用要点与常见陷阱）
├── WevvMoldGUI/                # C++ 核心
│   ├── include/WevvMold/       # 公共头（Abi 声明 + Core 类）
│   ├── src/                    # Abi / Core / Pal 三层实现
│   ├── tests/                  # C++ 自测与演示程序
│   └── CMakeLists.txt
└── WevvMoldGUI_for_Python/     # Python 绑定
    ├── src/                    # CPython 扩展模块源码
    ├── wevvmold/               # 纯 Python 封装包（12 个公开类）
    ├── tests/                  # Python 测试与演示（含 showcase.py 十页综合展示）
    └── CMakeLists.txt
```

## 测试与展示

- C++ 自测：28 个（构建后在 `build\dist\Release\cpp\` 运行）
- Python 测试：`WevvMoldGUI_for_Python\tests\test_*.py` 共 21 个文件，逐个直接运行即可
- 综合展示：`python tests\showcase.py`——单程序十页展示全部能力（基础图形/容器/控件/高级控件/
  滚动浮动/图像图标/画布/图表/统计表/特效），数字键 1-9/0 切页，ESC 退出

## 许可证

本项目以 [MIT License](LICENSE) 开源。界面文本使用系统默认字体，本项目不指定、不分发任何字体文件；
第三方组件说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
