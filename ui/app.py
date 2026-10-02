# DWT 主窗口：单页两栏（左参数 / 右进度 + 日志），浅色扁平，无预览窗口。
# 本文件以 MIT 许可证发布，全文见 LICENSE，授权范围见 README.md。
#
# 界面布局：
#   顶栏 DWT + 副标题 + 状态 + 终止 + 开始叠加；
#   左栏 参数三档折叠（控件全部来自 pws_params.PARAMS）；
#   右栏 进度卡（大号百分比 + 阶段 + 已用/预计剩余）+ 日志（按阶段标签着色）；
#   底栏 输出路径与成品信息。
# 窗口几何与参数值持久化在 QSettings('DWT','DWT')；PARAMS_VER 变化时
#   一次性将参数刷新为新的默认值。
#   素材/输出/帧落盘目录（VOLATILE_KEYS）不持久化，每次启动从空白开始，
#   以免换用新数据集后仍沿用上一批数据集的目录。

from __future__ import annotations

import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent                 # ui/
_ROOT = _HERE.parent                                    # 仓库根（xisf_io / DWT_DetailCore 在这）
for _p in (str(_ROOT), str(_ROOT / 'core'), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from PySide6.QtCore import QSettings, Qt, QTimer                        # noqa: E402
from PySide6.QtGui import QCloseEvent                                    # noqa: E402
from PySide6.QtWidgets import (                                          # noqa: E402
    QApplication, QFrame, QHBoxLayout, QLabel, QMainWindow, QProgressBar,
    QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from pws_params import PARAMS, PARAMS_VER, PwsParams                     # noqa: E402
from logview import LogView                                              # noqa: E402
from panel import ParamPanel                                             # noqa: E402
from worker import StackWorker                                           # noqa: E402

# 与"这一次要叠哪批数据"绑定的路径字段：不写进 QSettings。
# 否则下次启动会原样恢复上一批数据的目录，换新数据后成品仍落进旧目录。
VOLATILE_KEYS = frozenset({'photos', 'out', 'frames_dir'})

# 引擎阶段名 → 界面显示文本
PHASE_TEXT = {
    '读帧': '读取帧 · 曝光归一 · 天空平移',
    '星表': '建立固定星表',
    '测星点': '测帧间星点 FWHM（清晰度权重）',
    'R 场': '建分区权重场 R',
    '叠加': '加权叠加 · 逐条带',
    '验收': '验收与导出',
}


def mmss(sec: float) -> str:
    """秒 → mm:ss（超过一小时输出 h:mm:ss）"""
    s = max(0, int(sec))
    if s >= 3600:
        return f'{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}'
    return f'{s // 60:02d}:{s % 60:02d}'


class MainWindow(QMainWindow):
    """DWT 主窗口"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle('DWT · Partition-Weighted Stacking')
        self.resize(1100, 740)
        self.worker: StackWorker | None = None
        self._prog = ('', 0.0, '')
        self._t0 = 0.0
        self._auto_out = ''      # 上次自动推导出的输出目录（换素材时据此跟随）
        self._build()
        self._load_settings()
        self._set_state('idle', '就绪')
        self._tick = QTimer(self)
        self._tick.setInterval(700)          # 进度约每 0.7 s 刷新一次
        self._tick.timeout.connect(self._flush_progress)
        self._refresh_foot()

    # ---------------------------------------------------------------- 界面
    def _build(self) -> None:
        root = QWidget()
        root.setObjectName('root')
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(14, 12, 14, 10)
        lay.setSpacing(10)

        # ---- 顶栏 ----
        title = QLabel('DWT')
        title.setObjectName('title')
        sub = QLabel('Partition-Weighted Stacking')
        sub.setObjectName('subtitle')
        self.chip = QLabel('就绪')
        self.chip.setObjectName('statusChip')
        self.chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.btn_run = QPushButton('开始叠加')
        self.btn_run.setObjectName('run')
        self.btn_stop = QPushButton('终止')
        self.btn_stop.setObjectName('stop')
        self.btn_stop.setEnabled(False)
        self.btn_run.clicked.connect(self.start)
        self.btn_stop.clicked.connect(self.cancel)

        top = QHBoxLayout()
        top.setSpacing(10)
        top.addWidget(title)
        top.addWidget(sub)
        top.addStretch(1)
        top.addWidget(self.chip)
        top.addWidget(self.btn_stop)
        top.addWidget(self.btn_run)
        lay.addLayout(top)

        # ---- 左栏：参数卡片 ----
        card = QFrame()
        card.setObjectName('card')
        cl = QVBoxLayout(card)
        cl.setContentsMargins(10, 10, 10, 12)
        cl.setSpacing(8)
        cap = QLabel('参数')
        cap.setObjectName('cardTitle')
        cl.addWidget(cap)
        self.panel = ParamPanel()
        cl.addWidget(self.panel)
        cl.addStretch(1)

        sc = QScrollArea()
        sc.setObjectName('paramScroll')
        sc.setWidgetResizable(True)
        sc.setWidget(card)
        sc.setFixedWidth(404)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        # ---- 右栏：进度卡 ----
        pc_card = QFrame()
        pc_card.setObjectName('card')
        pc = QVBoxLayout(pc_card)
        pc.setContentsMargins(16, 14, 16, 14)
        pc.setSpacing(8)
        self.lbl_pct = QLabel('0%')
        self.lbl_pct.setObjectName('pct')
        self.lbl_pct.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_phase = QLabel('等待开始')
        self.lbl_phase.setObjectName('phase')
        self.lbl_phase.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.lbl_detail = QLabel('选择素材目录后点「开始叠加」')
        self.lbl_detail.setObjectName('detail')
        self.lbl_detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pc.addWidget(self.lbl_pct)
        pc.addWidget(self.lbl_phase)
        pc.addWidget(self.bar)
        pc.addWidget(self.lbl_detail)

        # ---- 右栏：日志卡 ----
        lg_card = QFrame()
        lg_card.setObjectName('card')
        lg = QVBoxLayout(lg_card)
        lg.setContentsMargins(12, 10, 12, 10)
        lg.setSpacing(8)
        cap2 = QLabel('日志')
        cap2.setObjectName('cardTitle')
        self.log = LogView()
        lg.addWidget(cap2)
        lg.addWidget(self.log, 1)

        right = QVBoxLayout()
        right.setSpacing(10)
        right.addWidget(pc_card)
        right.addWidget(lg_card, 1)

        content = QHBoxLayout()
        content.setSpacing(12)
        content.addWidget(sc)
        content.addLayout(right, 1)
        lay.addLayout(content, 1)

        # ---- 底栏 ----
        self.foot_path = QLabel('输出：—')
        self.foot_path.setObjectName('footPath')
        self.foot_info = QLabel('XISF · Float32 线性 · —')
        self.foot_info.setObjectName('footInfo')
        foot = QHBoxLayout()
        foot.addWidget(self.foot_path)
        foot.addStretch(1)
        foot.addWidget(self.foot_info)
        lay.addLayout(foot)

    # ---------------------------------------------------------------- 状态
    def _set_state(self, state: str, text: str) -> None:
        """状态胶囊：idle / run / done / err（QSS 按 state 属性配色）"""
        self.chip.setText(text)
        self.chip.setProperty('state', state)
        self.chip.style().unpolish(self.chip)
        self.chip.style().polish(self.chip)

    def _refresh_foot(self) -> None:
        """底栏显示当前输出目录（参数改动后由 start() 再刷新一次）"""
        try:
            p = self.panel.values()
        except Exception:
            return
        out = (p.out or '').strip()
        self.foot_path.setText(f'输出：{out}' if out else '输出：—（留空将自动放在素材目录旁）')

    def _fail(self, msg: str) -> None:
        """启动前的校验：错误信息写入日志，不创建任何文件。"""
        self.log.append(f'[错误] {msg}')
        self._set_state('err', '错误')

    # ---------------------------------------------------------------- 运行
    def start(self) -> None:
        p = self.panel.values()
        photos = Path(p.photos.strip())
        if not photos.is_dir():
            self._fail(f'素材目录不存在：{photos}')
            return
        if not (next(photos.glob('*.xisf'), None) or next(photos.glob('*.fit*'), None)):
            self._fail(f'素材目录里没有 XISF / FITS 帧：{photos}')
            return
        if not (p.out or '').strip() or p.out == self._auto_out:
            # 输出目录留空时默认取素材目录的同级目录，以免成品被下一轮当作帧读入。
            # 上一次为自动推导值（随素材变化）时，按当前素材重新推导；
            # 用户手动填写的自定义目录予以保留（其值不等于上一次的自动推导值）。
            p.out = str(photos.parent / f'{photos.name}_DWT')
            self._auto_out = p.out
            self.panel.set_values(p)

        self.log.reset_clock()
        self.log.clear()
        self.log.append(f'[帧] 素材 {photos}')
        self.log.append(f'[输出] 成品目录 {p.out}')
        self._prog = ('读帧', 0.0, '准备')
        self._t0 = time.perf_counter()
        self.bar.setValue(0)
        self.lbl_pct.setText('0%')
        self.lbl_phase.setText(PHASE_TEXT['读帧'])
        self.lbl_detail.setText('已用 00:00')
        self.panel.set_locked(True)
        self.btn_run.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._set_state('run', '运行中')
        self._refresh_foot()

        self.worker = StackWorker(p, self)
        self.worker.sig_log.connect(self.log.append)
        self.worker.sig_progress.connect(self._on_progress)
        self.worker.sig_done.connect(self._on_done)
        self.worker.sig_cancelled.connect(self._on_cancelled)
        self.worker.sig_error.connect(self._on_error)
        self.worker.finished.connect(self.worker.deleteLater)
        self._tick.start()
        self.worker.start()

    def cancel(self) -> None:
        """仅置取消标志：引擎在检查点退出并清除临时帧文件。"""
        if self.worker is not None and self.worker.isRunning():
            self.btn_stop.setEnabled(False)
            self.log.append('[终止] 已请求终止，等当前步骤结束…')
            self.worker.cancel()

    def _on_progress(self, phase: str, frac: float, text: str) -> None:
        self._prog = (phase, float(frac), text)

    def _flush_progress(self) -> None:
        phase, frac, text = self._prog
        if not phase:
            return
        self.bar.setValue(int(round(frac * 1000)))
        self.lbl_pct.setText(f'{frac * 100:.0f}%')
        self.lbl_phase.setText(PHASE_TEXT.get(phase, phase))
        el = time.perf_counter() - self._t0
        eta = ''
        if 0.03 <= frac < 1.0:
            eta = f' · 预计剩余 {mmss(el * (1.0 - frac) / frac)}'
        self.lbl_detail.setText(f'{text} · 已用 {mmss(el)}{eta}')

    def _settle(self) -> None:
        """收尾：停止计时，恢复界面可操作状态。"""
        self._tick.stop()
        self._flush_progress()
        self.panel.set_locked(False)
        self.btn_run.setEnabled(True)
        self.btn_stop.setEnabled(False)

    def _on_done(self, res: dict) -> None:
        self._prog = ('验收', 1.0, '完成')
        self._settle()
        h, w = res['shape']
        self.log.append(f'[验收] 成品 {w}×{h}  星点 FWHM {res["fwhm_out"]:.2f}px'
                        f'（单帧中位 {res["fwhm_med"]:.2f}px）  '
                        f'排异剔除率 {res["rej_rate"]:.3%}')
        self.foot_info.setText(f'XISF · Float32 线性 · {w}×{h}')
        self._set_state('done', '完成')

    def _on_cancelled(self) -> None:
        self._prog = ('', 0.0, '已终止')
        self._settle()
        self.log.append('[终止] 已终止；临时帧文件已清理')
        self._set_state('idle', '已终止')

    def _on_error(self, text: str) -> None:
        self._settle()
        for line in (text or '').rstrip().splitlines() or ['未知错误']:
            self.log.append(f'[错误] {line}')
        self._set_state('err', '错误')

    # ---------------------------------------------------------------- 持久化
    def _load_settings(self) -> None:
        """恢复窗口几何与参数；PARAMS_VER 变化时整体回退为新的默认值。"""
        s = QSettings('DWT', 'DWT')
        geo = s.value('geometry')
        if geo is not None:
            self.restoreGeometry(geo)
        p = PwsParams()
        if s.value('params_ver', '') == PARAMS_VER:
            for prm in PARAMS:
                if prm.key in VOLATILE_KEYS:
                    continue         # 素材/输出目录不跨次继承，以免沿用到旧数据集
                key = f'params/{prm.key}'
                if not s.contains(key):
                    continue
                v = s.value(key)
                try:
                    if prm.kind == 'float':
                        v = float(v)
                    elif prm.kind == 'int':
                        v = int(v)
                    elif prm.kind == 'check':
                        v = str(v).strip().lower() in ('true', '1', 'yes', 'on')
                    else:
                        v = str(v)
                except (TypeError, ValueError):
                    continue
                setattr(p, prm.key, v)
        self.panel.set_values(p)
        s.setValue('params_ver', PARAMS_VER)

    def _save_settings(self) -> None:
        s = QSettings('DWT', 'DWT')
        s.setValue('geometry', self.saveGeometry())
        p = self.panel.values()
        for prm in PARAMS:
            if prm.key in VOLATILE_KEYS:
                continue             # 素材/输出目录不写盘，下次启动从空白开始选择
            s.setValue(f'params/{prm.key}', getattr(p, prm.key))

    def closeEvent(self, event: QCloseEvent) -> None:   # noqa: N802 - Qt 命名
        """关闭窗口前先停止引擎，避免 memmap 临时文件残留在磁盘上。"""
        w = self.worker
        if w is not None:
            try:
                if w.isRunning():
                    w.cancel()
                    w.wait(8000)
            except RuntimeError:
                pass          # 线程对象已被 deleteLater 回收，无需再等
        self._save_settings()
        event.accept()


def _theme_qss() -> str:
    """主题样式表：源码运行取 ui/theme.qss，打包（PyInstaller）运行取包内资源目录"""
    base = Path(getattr(sys, '_MEIPASS', _HERE))
    p = base / 'theme.qss'
    return (p if p.is_file() else _HERE / 'theme.qss').read_text(encoding='utf-8')


def main(argv=None) -> int:
    """GUI 入口（命令行入口在 core/pws.py，两者共用同一份参数定义）"""
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName('DWT')
    app.setStyleSheet(_theme_qss())
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
