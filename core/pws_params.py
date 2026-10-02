# DWT 叠加软件：参数定义
# 本文件以 MIT 许可证发布，全文见 LICENSE，授权范围见 README.md。
#
# 单一来源，三处消费：
#   · 界面控件由 PARAMS 生成（标签、范围、默认值、悬停说明均取自此处）
#   · 引擎由 PwsParams 数据类读取
#   · 命令行入口由 PARAMS 生成 argparse

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Tuple

# 工作点版本号：默认值变更时须更新此号；界面下次启动即一次性
#   将持久化参数刷新为新的默认值（否则本机将保留旧默认值）。
PARAMS_VER = '20261001c'


@dataclass(frozen=True)
class Param:
    """一个参数的完整描述（界面与命令行都由它生成）"""

    key: str                      # 同时是 PwsParams 的字段名与命令行 --key
    label: str                    # 界面标签
    kind: str                     # dir / text / float / int / choice / check
    group: str                    # 基础 / 标准 / 高级
    desc: str                     # 用途
    usage: str                    # 取值与影响
    lo: float = 0.0
    hi: float = 0.0
    step: float = 1.0
    dec: int = 2                  # 小数位
    unit: str = ''
    choices: Tuple[Tuple[str, object], ...] = ()   # ((界面文字, 值), …)


@dataclass
class PwsParams:
    """引擎入参（字段名 == Param.key，两边靠这一条对齐）"""

    # ---- 基础 ----
    photos: str = ''
    out: str = ''
    tag: str = 'stack'

    # ---- 标准 ----
    A: float = 20.0
    rej_k: float = 3.5
    limit: int = 0
    frames_dir: str = ''
    keep_frames: bool = False

    # ---- 高级 ----
    crop: int = 0
    center: str = ''
    B: float = 2.0
    env_win_mult: float = 2.0
    env_eps: float = 8.0
    env_p: float = 1.0
    r_floor: float = 0.0
    n_star: int = 120
    rej_gate: float = 1 / 3


# 界面显示顺序 == 本表顺序
PARAMS: Tuple[Param, ...] = (
    # ---------------- 基础 ----------------
    Param('photos', '素材目录', 'dir', '基础',
          '待叠加的帧目录',
          '已校准（暗/平/偏）、已对齐（旋转+平移）的单通道 XISF / FITS；'
          '按文件名排序读取。同一目录内不要混入不同目标的帧。'),
    Param('out', '输出目录', 'dir', '基础',
          '成品与附带文件的落盘位置',
          '不存在会自动创建。成品是线性 Float32 灰度 XISF，附带权重表 / R 场 / 等效帧数。'),
    Param('tag', '成品名', 'text', '基础',
          '成品文件名后缀',
          '输出为 stack_<成品名>.xisf。留空或随素材目录名自动跟随。'),

    # ---------------- 标准 ----------------
    Param('A', '深化强度 A', 'float', '标准',
          '细节区以信噪比换取解析力的强度',
          '方法参数中唯一需经验标定者。R=1 处的权重为 C^A，A 越大越偏向锐帧，默认 20。'
          '调小则趋于保守：天空噪声增长较少、星点略软；'
          '调大则解析力更强：星点更细，但天空噪声上升、细节区等效帧数下降。',
          lo=1.0, hi=60.0, step=0.5, dec=1),
    Param('rej_k', '排异强度 k', 'float', '标准',
          '剔除单帧异常像元（卫星、宇宙线、热噪）',
          '逐像素跨帧 |偏差| 超过 阈值 = k×1.4826×MAD + m·R·(信号−天空) 的帧判为离群并剔除；'
          'm 从数据实测（细节像素偏差/信号的 P99 跨帧中位数），即真实分歧上界，'
          '故排异只剔除伪迹、不剔除信号。k 以 σ 为单位，线性可感：越小越严、越大越宽；'
          '默认 3.5；取 2 可多剔除 2~3.5σ 的离群；低于 2 将成批剔除清洁帧的噪声尾'
          '（信噪比明显下降），不建议；0 = 关闭排异。',
          lo=0.0, hi=6.0, step=0.1, dec=1),
    Param('limit', '帧数上限', 'int', '标准',
          '只取排序后的前 N 帧',
          '0 = 全部（正式出图用此值）。>0 用于在小样本上先行试参；'
          '此处并非"挑好帧"，帧的取舍由权重在叠加内部完成。',
          lo=0, hi=100000, step=1),
    Param('frames_dir', '帧落盘目录', 'dir', '标准',
          '帧数据超出内存容量时的临时帧文件位置',
          '留空 = 自动选盘：项目盘空间足够时使用项目盘，否则选余量最大的盘（需 1.25× 余量）。'
          '166 帧全幅约需 40 GiB，超出内存容量时自动启用。'),
    Param('keep_frames', '保留帧文件', 'check', '标准',
          '运行结束后不删除临时帧文件',
          '默认关闭（运行结束后自动删除）。反复调参时启用，可省去重新落盘数十 GiB 的时间。'),

    # ---------------- 高级 ----------------
    Param('crop', '裁剪边长', 'int', '高级',
          '只叠画面中间 N×N 像素',
          '0 = 全幅（正式出图）。>0 仅用于快速试参：裁剪后帧更小、耗时更短，'
          '但成品非完整画面。',
          lo=0, hi=20000, step=64),
    Param('center', '裁剪中心', 'text', '高级',
          '裁剪窗中心坐标 (y,x)',
          '留空 = 自动定位最亮的延展源（32 px 分块中值法）。'
          '顺序为 y,x（先行后列），非 x,y。仅在裁剪边长 > 0 时有效。'),
    Param('B', '信噪比幂 B', 'float', '高级',
          '朦胧区以帧数换取噪声抑制的强度',
          'B=2 由逆方差最优性给出：加权平均方差最小 ⇔ 权重 ∝ 1/σ² ⇔ S²。'
          '除实验外不建议修改。',
          lo=0.0, hi=4.0, step=0.1, dec=1),
    Param('env_win_mult', '包络窗', 'float', '高级',
          '局部极大窗宽度 = 该值 × 2σ_psf',
          '决定亮度归一包络的局部极大比较范围，2.0 对应约 4σ_psf 见方。'
          '调大则更宽容（R 覆盖更广）；调小则支撑更贴近星点。',
          lo=0.5, hi=6.0, step=0.1, dec=1),
    Param('env_eps', '包络噪声项', 'float', '高级',
          '抑制空白天区噪声的相对强度',
          '分母加上 该值 × σ（论文式 8 的 εσ 项），使空白天区的 R 趋近 0（完全按信噪比加权）。'
          '默认 8；调大则空白天区的 R 更低、天空噪声增长更少。',
          lo=0.0, hi=20.0, step=0.5, dec=1),
    Param('env_p', '包络形状幂', 'float', '高级',
          '包络形状的收束程度',
          '默认 1.0。>1 收窄支撑（R 更贴近核心），<1 展宽。',
          lo=0.25, hi=4.0, step=0.05, dec=2),
    Param('r_floor', 'R 下限', 'float', '高级',
          '全图保留的最小清晰度偏好',
          '增大则该值使空白天区亦偏向锐帧，星周"外圈亮环"变浅；'
          '代价为天空的等效帧数下降（信噪比变差）。0 = 关闭。',
          lo=0.0, hi=0.95, step=0.01, dec=2),
    Param('n_star', '星表星数', 'int', '高级',
          '测帧间清晰度用的固定星表规模',
          '在信噪比最高的一帧上一次选星（未饱和、孤立、最亮），'
          '之后全部帧测量同一批位置。中位数在 100 颗以上已稳定；星点稀少的视场可调小。',
          lo=20, hi=600, step=10),
    Param('rej_gate', '少数派闸门', 'float', '高级',
          '同一像素被剔帧数超出噪声期望误排数后的余量超过该比例，即判为非孤立离群，该像素不排异',
          '避免将多数帧共有的真实结构（星核锐/模糊双峰、配准黑边）判为伪迹而剔除。'
          '默认 1/3 = 仅剔除少数派；调大则保护减弱（剔除更强），调小则更保守。'
          '闸门已扣除噪声期望误排数 n·erfc(k/√2)，与 k 解耦。',
          lo=0.10, hi=0.90, step=0.05, dec=2),
)

GROUP_ORDER: Tuple[str, ...] = ('基础', '标准', '高级')


def params_of(group: str):
    """取某一档的参数（界面按档建面板）"""
    return tuple(p for p in PARAMS if p.group == group)


def tip(p: Param) -> str:
    """悬停说明：用途 / 取值，两段"""
    return f'{p.desc}\n\n{p.usage}'


def defaults() -> PwsParams:
    """默认值校验（数据类的字段默认值就是工作点，此处只做校验）"""
    keys = {f.name for f in fields(PwsParams)}
    for p in PARAMS:
        if p.key not in keys:
            raise RuntimeError(f'参数 {p.key} 在 PwsParams 里没有对应字段')
    return PwsParams()


def cli_flag(key: str) -> str:
    """参数名 → 命令行选项（下划线转连字符；A/B 保持大写）"""
    return '--' + key.replace('_', '-')


def to_argv(p: PwsParams) -> list:
    """参数对象 → 命令行（供批处理使用）"""
    argv: list = []
    for prm in PARAMS:
        v = getattr(p, prm.key)
        if prm.kind == 'check':
            if v:
                argv.append(cli_flag(prm.key))
            continue
        if prm.kind == 'float':
            argv += [cli_flag(prm.key), f'{float(v):g}']
        elif prm.kind == 'int':
            argv += [cli_flag(prm.key), str(int(v))]
        else:
            txt = str(v)
            if txt:
                argv += [cli_flag(prm.key), txt]
    return argv
