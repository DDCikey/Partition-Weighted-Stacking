# DWT 设计令牌：结构尺寸 / 动效 / 页签 / 颜色混合
#
# 色板唯一真源是 style.py；本文件放"布局与动效"令牌。
# 结构：深色顶栏（logo + 分段导航 + 圆形运行钮）+ 全宽页面 + 细底栏。
#   页面五张：素材（Drop 区+来源清单）/ 参数（双列卡片）/ 高级（双列卡片）/
#   监控（圆环+终端日志）/ 评价（R 场与 N_eff 场）。

# ---- 页面结构：页签名 → PARAMS 档；'监控' 无参数档（app 渲染圆环与日志）；
#      '评价' 为哨兵档（app 渲染 maps.MapsPage，panel 不生成任何参数卡） ----
MAPS_GROUP = '__maps__'
TABS = (('素材', '基础'), ('参数', '标准'), ('高级', '高级'),
        ('监控', None), ('评价', MAPS_GROUP))

# ---- 结构尺寸 ----
HEADER_H = 92         # 深色顶栏总高（含标题栏按钮区）
CAPTION_H = 44        # 顶栏中留给系统三控制按钮的上带
CONTENT_X = 40        # 内容区左右留白
FOOT_H = 20           # 底栏高
R_CARD = 16           # 大卡片圆角
R_CTRL = 9            # 控件圆角
SEG_H = 38            # 分段导航高
RUN_D = 48            # 圆形运行钮直径

# ---- 参数卡片网格 ----
CARD_H = 84           # 双列参数卡高度
CARD_GAP = 14

# ---- 圆环进度 ----
RING_OUT = 118
RING_IN = 92

# ---- 动效时长（ms） ----
HOVER_MS = 140
PRESS_MS = 90
FOCUS_MS = 120
SLIDE_MS = 110        # 分段导航滑块/下划线滑动时长
PROGRESS_MS = 500     # 进度值平滑
TICK_IDLE = 16        # 定时器周期（与 TICK_ANIM 同值，恒定 60fps）：每帧回调开销仅
                      #   1~3ms，空闲只跑回调不重绘、开销可忽略；不按有无动画切换
                      #   周期（反复 start_timer 无收益，且引入节拍落差隐患）
TICK_ANIM = 16        # 有动画进行时的定时器周期（与空闲同值=常转 60fps）


def mix(c1, c2, t):
    """颜色线性插值（t=0 取 c1，t=1 取 c2）；对带 alpha 的颜色同样有效"""
    if t <= 0.0:
        return c1
    if t >= 1.0:
        return c2
    return tuple(a + (b - a) * t for a, b in zip(c1, c2))
