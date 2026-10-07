# DWT 叠加软件：参数的**唯一真源**
#
# 一份定义，三处消费：
#   · 界面控件由 PARAMS 生成（标签、范围、默认值、悬停说明都取自这里）
#   · 引擎由 PwsParams 数据类读取
#   · 命令行入口由 PARAMS 生成 argparse
# 改一个默认值或一句说明只改这一处 —— 界面与引擎不可能漂移
# （v1 的 44 参数与 v2 的 10 参数并存时，正是"同一口径两处实现"才漂的）。

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Tuple

# 工作点版本号：改动默认值时把它改一下，界面下次启动会一次性
#   把持久化的参数刷成新定稿值（否则用户机器上会一直留着老默认值）。
PARAMS_VER = '20261007a'


@dataclass(frozen=True)
class Param:
    """一个参数的完整描述（界面与命令行都由它生成）"""

    key: str                      # 同时是 PwsParams 的字段名与命令行 --key
    label: str                    # 界面标签
    kind: str                     # dir / text / float / int / choice / check
    group: str                    # 基础 / 标准 / 高级
    desc: str                     # 用途（一句话）
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
    tag: str = ''

    # ---- 标准 ----
    A: float = 20.0
    rej_k: float = 3.5
    limit: int = 0
    frames_dir: str = ''
    keep_frames: bool = False

    # ---- 高级 ----
    # 已删除：分区映射 rmap（含只服务它的 t0 / m / taper_mult）、
    #   R 判据口径 r_kind、空间裙边 smooth —— 都实测未通过，只作过对照。
    crop: int = 0
    center: str = ''
    B: float = 2.0
    env_win_mult: float = 2.0
    env_eps: float = 16.0
    env_p: float = 1.0
    r_floor: float = 0.0
    n_star: int = 120
    rej_gate: float = 1 / 3


# 界面显示顺序 == 本表顺序
PARAMS: Tuple[Param, ...] = (
    # ---------------- 基础 ----------------
    Param('photos', '素材', 'sources', '基础',
          '待叠加的帧来源（目录与单张可混用）',
          '已校准（暗/平/偏）、已对齐（旋转+平移）的单通道 XISF / FITS。'
          '用「添加目录 / 添加单张」加入多个来源（一行一个，行尾「×」移除，「清空」一键移除）；'
          '重复声明的文件只算一帧。同一目标不同夜晚的帧可以混叠。'
          '混合滤镜的素材会按文件头（FILTER）自动分组，各滤镜独立叠加、各自出一张成品。'),
    Param('out', '输出目录', 'dir', '基础',
          '成品与附带文件的落盘位置',
          '不存在会自动创建。成品是线性 Float32 灰度 XISF，附带权重表 / R 场 / 等效帧数。'),
    Param('tag', '成品名', 'text', '基础',
          '成品文件名后缀',
          '输出为 PWS_<成品名>.xisf；按滤镜分组时每组再带滤镜名'
          '（PWS_<成品名>_<滤镜>.xisf）。留空则命名为 PWS_<滤镜>。'),

    # ---------------- 标准 ----------------
    Param('A', '深化强度 A', 'float', '标准',
          '细节区愿意用多少信噪比换解析力',
          '唯一需要标定的参数，也是本方法唯一"可调风格"的旋钮。'
          'R=1 处的权重为 C^A，A 越大越偏向锐帧。'
          '定稿值 20（7331 与 6888 共用一套参数通过验收）；'
          '12 更保守：天空噪声只涨 2~4%、星点略软；'
          '30 更锐：星点再细 0.1~0.3px，但天空噪声涨 6~7%、细节区等效帧数掉到 4 以下。',
          lo=1.0, hi=60.0, step=0.5, dec=1),
    Param('rej_k', '排异强度 k', 'float', '标准',
          '剔除单帧异常像元（卫星、宇宙线、热噪）',
          '逐像素跨帧|偏差|超过 阈值 = k×1.4826×MAD + m·R·(信号−天空) 的帧被判离群剔除；'
          'm 从数据实测（细节像素偏差/信号的 P99 跨帧中位数）= 真实视宁度分歧上界，'
          '故排异只打伪迹、不打信号。k 以 σ 为单位，是线性可感知的旋钮：越小越严、越大越宽；'
          '3.5 为定稿；2 可多排 2~3.5σ 带离群；低于 2 开始成批排清洁帧噪声尾'
          '（信噪比明显下降），不建议；0 = 关闭排异（仅用于对照）。',
          lo=0.0, hi=6.0, step=0.1, dec=1),
    Param('limit', '帧数上限', 'int', '标准',
          '只取排序后的前 N 帧',
          '0 = 全部（正式出图用这个）。>0 用于先在小样本上试参数，'
          '注意这不是"挑好帧"：帧的取舍由权重在叠加内部完成。',
          lo=0, hi=100000, step=1),
    Param('frames_dir', '帧落盘目录', 'dir', '标准',
          '帧多到装不进内存时的临时帧文件位置',
          '留空 = 自动挑盘：项目盘够用就用项目盘，否则挑余量最大的盘（需 1.25× 余量）。'
          '166 帧全幅约需 40 GiB，装不下内存时会自动启用。'),
    Param('keep_frames', '保留帧文件', 'check', '标准',
          '跑完不删除临时帧文件',
          '默认关闭（跑完自动删）。反复调参时勾上，可省掉重新落盘几十 GiB 的时间。'),

    # ---------------- 高级 ----------------
    Param('crop', '裁剪边长', 'int', '高级',
          '只叠画面中间 N×N 像素',
          '0 = 全幅（正式出图）。>0 只用于快速试参数：裁剪后帧小、跑得快，'
          '但成品不是完整画面。',
          lo=0, hi=20000, step=64),
    Param('center', '裁剪中心', 'text', '高级',
          '裁剪窗中心坐标 (y,x)',
          '留空 = 自动定位最亮的延展源（32px 分块中值法）。'
          '注意顺序是 y,x（先行后列），不是 x,y。仅在裁剪边长 > 0 时有效。'),
    Param('B', '信噪比幂 B', 'float', '高级',
          '朦胧区用多少帧数换噪声',
          'B=2 不是调出来的：加权平均方差最小 ⇔ 权重 ∝ 1/σ² ⇔ S²。'
          '除非做实验，否则不要动。',
          lo=0.0, hi=4.0, step=0.1, dec=1),
    Param('env_win_mult', '包络窗', 'float', '高级',
          '局部极大窗的宽度 = 该值 × 2σ_psf',
          '决定"和多大范围内的最亮处比"。2.0 对应 ≈4σ_psf 见方。'
          '调大 → 更宽容（R 覆盖更广）；调小 → 支撑更贴星点。',
          lo=0.5, hi=6.0, step=0.1, dec=1),
    Param('env_eps', '包络噪声项', 'float', '高级',
          '压住空白天区的噪声倍数',
          '分母加上 该值 × σ，使空白天区的 R 趋近 0（严格吃满信噪比）。'
          '定稿 16（12 组 568 帧全量定标）：星点余晖收紧 0.6%~36%'
          '（12 组无一实质恶化），'
          '星核与天空噪声在 8→16 全程不变（唯一例外：帧间清晰度分歧极大的数据'
          '星核增约 5%，目视整体更干净）。8 为旧工作点，星点余晖偏松；'
          '20 以上收益进入测量噪声带、24 为安全上限，不建议超过。',
          lo=0.0, hi=24.0, step=0.5, dec=1),
    Param('env_p', '包络形状幂', 'float', '高级',
          '把 R 收得更紧',
          '1.0 即定稿。>1 收窄支撑（R 更贴核心），<1 放宽。',
          lo=0.25, hi=4.0, step=0.05, dec=2),
    Param('r_floor', 'R 下限', 'float', '高级',
          '全图保留的最小清晰度偏好',
          '抬高它 → 空白天区也偏向锐帧 → 星周"外圈亮环"变浅；'
          '代价是天空的等效帧数下降（信噪比变差）。0 = 关闭。',
          lo=0.0, hi=0.95, step=0.01, dec=2),
    Param('n_star', '星表星数', 'int', '高级',
          '测帧间清晰度用的固定星表规模',
          '在信噪比最好的一帧上一次性选星（未饱和、孤立、最亮），'
          '之后所有帧量同一批位置。中位数在 100 颗以上已稳定；星太少的视场可调小。',
          lo=20, hi=600, step=10),
    Param('rej_gate', '少数派闸门', 'float', '高级',
          '同一像素被排帧数超出噪声期望误排数的部分超过该比例，即判"不是孤立离群"，该像素不排异',
          '防止把多数帧共有的真实结构（星核锐/糊双峰、配准黑边）当伪迹排掉。'
          '默认 1/3 = 只排少数派；调大保护变弱（排得更狠），调小更保守。'
          '闸门已扣除噪声期望误排数 n·erfc(1.15k/√2)，与 k 解耦。',
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
    """定稿默认值（数据类的字段默认值就是定稿工作点，此处只做校验）"""
    keys = {f.name for f in fields(PwsParams)}
    for p in PARAMS:
        if p.key not in keys:
            raise RuntimeError(f'参数 {p.key} 在 PwsParams 里没有对应字段')
    return PwsParams()


def cli_flag(key: str) -> str:
    """参数名 → 命令行选项（下划线转连字符；A/B 保持大写）"""
    return '--' + key.replace('_', '-')


def to_argv(p: PwsParams) -> list:
    """参数对象 → 命令行（供批处理与等价性回归使用）"""
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
