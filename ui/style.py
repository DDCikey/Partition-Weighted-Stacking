# 20261005 DWT 浅色扁平主题常量与绘制助手（WevvMold 版，替代原 theme.qss）
# 20261006 质感助手：实测渲染器支持 alpha 混合（a15 白底→240、可叠加累积），
#   新增 shadow / gloss_top / shade_bottom / inner_shadow 四层微立体原语，
#   alpha 一律压在 5~30/255，保持浅色扁平基调、只做层次不花哨。
# 20261006 截图目检修正：白卡上的黑投影（alpha≈12/255≈5%）肉眼不可见，
#   提到 26/32 档（≈10%）才有可感知的浮起感；控件层同步提一档。
#
# 色值与原 QSS 定稿一一对应；字号 pt→px 按 96dpi 换算（9pt=12px）。
# WevvMold 核心控件是固定深色调色板、无法定制，因此界面全部用
# 渲染原语自绘，保证配色与原 PySide6 版完全一致。

from __future__ import annotations

from wevvmold import color

# ---- 调色板（原 theme.qss）----
BG        = color(245, 247, 250)   # 窗口底 #f5f7fa
CARD      = color(255, 255, 255)   # 卡片 #ffffff
LOG_BG    = color(251, 252, 254)   # 日志 #fbfcfe
BORDER    = color(227, 232, 239)   # 边框 #e3e8ef
TEXT      = color(28, 36, 49)      # 主文字 #1c2431
TEXT2     = color(91, 103, 121)    # 次要 #5b6779
HINT      = color(147, 160, 176)   # 提示 #93a0b0
ACCENT    = color(15, 157, 144)    # 强调青绿 #0f9d90
ACCENT_D  = color(12, 138, 126)    # run hover #0c8a7e
ACCENT_BG = color(240, 251, 249)   # run pressed / chip run #f0fbf9
ACCENT_BD = color(185, 230, 223)   # chip run border / 进度填充 #b9e6df
ACCENT_DS = color(207, 230, 226)   # run disabled #cfe6e2
TRACK     = color(238, 242, 247)   # 进度条轨道 #eef2f7
RED       = color(192, 57, 43)     # 错误 #c0392b
RED_BD    = color(242, 196, 190)   # #f2c4be
RED_BG    = color(253, 245, 244)   # #fdf5f4
RED_BD2   = color(232, 180, 172)   # stop hover #e8b4ac
DISABLED  = color(182, 191, 202)   # #b6bfca
DISABLED_BD = color(238, 242, 247)
DISABLED_BG = color(250, 251, 253) # #fafbfd
SEL_BG    = color(185, 230, 223)   # 选中文本底色 #b9e6df
SB_HANDLE = color(221, 227, 236)   # 滚动条 #dde3ec
SB_HOVER  = color(198, 207, 219)   # #c6cfdb
BLUE      = color(43, 110, 168)    # 日志结果类 #2b6ea8
WHITE     = color(255, 255, 255)
TRANSPARENT = (0.0, 0.0, 0.0, 0.0)

# ---- 字号（px，对应原 8/9/10/14/22pt）----
FS_SMALL  = 11
FS_BODY   = 12
FS_TITLE2 = 13
FS_TITLE  = 19
FS_PCT    = 29
FS_HUGE   = 40              # 监控页圆环中央大百分比（20261006 v3）


def char_w(ch: str, size: float) -> float:
    """单字符宽度估算（系统默认字体；CJK≈1em，ASCII 按字形分档）"""
    o = ord(ch)
    if o >= 0x2E80 or ch in '…「」（）：；，。×—':
        return size
    if ch.isdigit():
        return size * 0.556
    if ch in 'WM':
        return size * 0.94
    if 'A' <= ch <= 'Z':
        return size * 0.68
    if ch in 'il.,:;|!\'()[] ':
        return size * 0.32
    return size * 0.54


def text_w(s: str, size: float) -> float:
    return sum(char_w(c, size) for c in s)


def elide_middle(s: str, size: float, maxw: float) -> str:
    """路径中段省略：保留头尾，中间 …"""
    if text_w(s, size) <= maxw:
        return s
    head = tail = ''
    hw = tw = 0.0
    dots = text_w('…', size)
    budget = maxw - dots
    i, j = 0, len(s) - 1
    while i <= j:
        cw = char_w(s[i], size)
        if hw + tw + cw <= budget:
            head += s[i]; hw += cw; i += 1
        else:
            break
    while j >= i:
        cw = char_w(s[j], size)
        if hw + tw + cw <= budget:
            tail = s[j] + tail; tw += cw; j -= 1
        else:
            break
    return head + '…' + tail


def wrap_text(s: str, size: float, maxw: float):
    """按宽度折行（悬停说明用），保留显式换行"""
    out = []
    for para in s.split('\n'):
        line = ''
        lw = 0.0
        for ch in para:
            cw = char_w(ch, size)
            if line and lw + cw > maxw:
                out.append(line)
                line, lw = ch, cw
            else:
                line += ch
                lw += cw
        out.append(line)
    return out


def round_rect(rc, l, t, r, b, rad, col):
    """圆角矩形（多边形逼近，每角 5 段）"""
    rad = min(rad, (r - l) / 2, (b - t) / 2)
    if rad <= 0.5:
        rc.fill_rect(l, t, r, b, col)
        return
    pts = []
    # 屏幕坐标（y 向下）顺时针：右上 −90°→0°、右下 0°→90°、左下 90°→180°、左上 180°→270°
    corners = ((r - rad, t + rad, -90), (r - rad, b - rad, 0),
               (l + rad, b - rad, 90), (l + rad, t + rad, 180))
    import math
    for cx, cy, a0 in corners:
        for k in range(6):
            a = math.radians(a0 + 90 * k / 5)
            pts.append((cx + rad * math.cos(a), cy + rad * math.sin(a)))
    rc.fill_polygon(pts, col)


def round_rect_border(rc, l, t, r, b, rad, border_col, fill_col, bw: float = 1.0):
    """带 1px 边框的圆角矩形（外框色 + 内芯填充色）"""
    round_rect(rc, l, t, r, b, rad, border_col)
    round_rect(rc, l + bw, t + bw, r - bw, b - bw, max(0.0, rad - bw), fill_col)


def pill(rc, l, t, r, b, col):
    """胶囊形（半径=高度一半）"""
    round_rect(rc, l, t, r, b, (b - t) / 2, col)


def aa_round_rect(rc, l, t, r, b, rad, col):
    """抗锯齿圆角矩形：两条正交矩形 + 四角原生 fill_ellipse 拼合。
    仅限不透明纯色（重叠区域不能带 alpha，否则接缝处会叠色）。"""
    rad = min(rad, (r - l) / 2, (b - t) / 2)
    if rad <= 0.5:
        rc.fill_rect(l, t, r, b, col)
        return
    rc.fill_rect(l + rad, t, r - rad, b, col)
    rc.fill_rect(l, t + rad, r, b - rad, col)
    rc.fill_ellipse(l + rad, t + rad, rad, rad, col)
    rc.fill_ellipse(r - rad, t + rad, rad, rad, col)
    rc.fill_ellipse(l + rad, b - rad, rad, rad, col)
    rc.fill_ellipse(r - rad, b - rad, rad, rad, col)


# ---------------------------------------------------------------- 微立体（质感）
def shadow(rc, l, t, r, b, rad, alpha=26):
    """柔投影：同心向外扩张的半透明黑圆角矩形（下缘多扩 2px 模拟光源在上），
    逐层 alpha 递减；扩张而非错位，避免三层错位叠加出硬边台阶"""
    for i, a in enumerate((alpha, alpha * 3 // 4, alpha // 2, alpha // 4)):
        round_rect(rc, l - 1 - i, t - 1 - i, r + 1 + i, b + 2 + i, rad + 1 + i,
                   color(0, 0, 0, a))


def gloss_top(rc, l, t, r, b, rad, alpha=20):
    """上亮高光：上半部叠一层半透明白（底边在填充内部，直角不外露）"""
    round_rect(rc, l + 1, t + 1, r - 1, (t + b) / 2, max(0.0, rad - 1),
               color(255, 255, 255, alpha))


def shade_bottom(rc, l, t, r, b, rad, alpha=12):
    """下暗：下半部叠一层半透明黑，与高光合成微弧光感"""
    round_rect(rc, l + 1, (t + b) / 2, r - 1, b - 1, max(0.0, rad - 1),
               color(0, 0, 0, alpha))


def inner_shadow_top(rc, l, t, r, b, rad, alpha=7):
    """内凹顶影：轨道/输入框上缘一条半透明黑，做出"嵌进去"的深度"""
    round_rect(rc, l + 1, t + 1, r - 1, t + (b - t) * 0.45, max(0.0, rad - 1),
               color(0, 0, 0, alpha))


# ---------------------------------------------------------------- 深色控制台语言（20261006 重设计）
# 顶栏与终端日志用深墨底；文字取其亮色变体。浅色内容区沿用上方原调色板。
DARK      = color(23, 30, 41)      # 顶栏 / 日志底 #171e29
DARK_2    = color(33, 43, 58)      # 深色面上的控件底 #212b3a
DARK_HOV  = color(43, 55, 72)      # 深色面上的悬停 #2b3748
D_TEXT    = color(226, 233, 242)   # 深色面上的主文字
D_TEXT2   = color(159, 176, 195)   # 深色面上的次文字
D_HINT    = color(116, 132, 152)   # 深色面上的提示
D_ACCENT  = color(53, 201, 182)    # 深底上的青绿（日志强调）
D_BLUE    = color(106, 183, 255)   # 深底上的蓝（日志结果）
D_RED     = color(255, 122, 107)   # 深底上的红（日志错误）
D_TRACK   = color(52, 64, 82)      # 深底上的轨道/分隔

# 内容区新令牌
SOFT      = color(242, 245, 249)   # 窗口底（比 BG 更冷一档）
TINT      = color(240, 251, 249)   # Drop 区浅青底
TINT_BD   = color(190, 232, 226)   # Drop 区边饰
