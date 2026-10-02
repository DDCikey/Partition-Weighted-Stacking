# DWT 参数面板：三档折叠，控件全部由 pws_params.PARAMS 生成。
# 本文件以 MIT 许可证发布，全文见 LICENSE，授权范围见 README.md。
#
# 每一项右侧一个「?」，悬停显示"用途 / 取值与影响"（由 tip() 提供的两段文本）。

from __future__ import annotations

from typing import Dict

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QSpinBox, QToolButton, QVBoxLayout, QWidget,
)

from pws_params import GROUP_ORDER, PARAMS, PwsParams, params_of, tip

_NAME_W = 84          # 名称列固定宽度，保证各档左边缘对齐
_HELP_W = 16


def make_editor(prm):
    """按 Param.kind 造控件：dir / text / float / int / choice / check"""
    if prm.kind == 'float':
        w = QDoubleSpinBox()
        w.setDecimals(prm.dec)
        w.setRange(float(prm.lo), float(prm.hi))
        w.setSingleStep(float(prm.step))
        if prm.unit:
            w.setSuffix(' ' + prm.unit)
        return w
    if prm.kind == 'int':
        w = QSpinBox()
        w.setRange(int(prm.lo), int(prm.hi))
        w.setSingleStep(int(prm.step))
        if prm.unit:
            w.setSuffix(' ' + prm.unit)
        return w
    if prm.kind == 'choice':
        w = QComboBox()
        for text, value in prm.choices:
            w.addItem(text, value)
        return w
    if prm.kind == 'check':
        return QCheckBox()
    return QLineEdit()


def read_editor(prm, w):
    """从控件读回参数值（与 make_editor 一一对应）"""
    if prm.kind in ('float', 'int'):
        return w.value()
    if prm.kind == 'choice':
        return w.currentData()
    if prm.kind == 'check':
        return w.isChecked()
    return w.text()


def write_editor(prm, w, value) -> None:
    """把参数值写进控件"""
    if prm.kind in ('float', 'int'):
        w.setValue(value)
    elif prm.kind == 'choice':
        i = w.findData(value)
        w.setCurrentIndex(i if i >= 0 else 0)
    elif prm.kind == 'check':
        w.setChecked(bool(value))
    else:
        w.setText('' if value is None else str(value))


class Section(QWidget):
    """一个折叠档：标题行（可点）+ 参数网格"""

    def __init__(self, group: str, panel: 'ParamPanel', expanded: bool = True):
        super().__init__(panel)
        self.group = group
        items = params_of(group)

        self.head = QToolButton()
        self.head.setObjectName('group')
        self.head.setCheckable(True)
        self.head.setChecked(expanded)
        self.head.setText(f'{group}')
        self.head.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.head.setCursor(Qt.CursorShape.PointingHandCursor)

        cnt = QLabel(f'{len(items)} 项')
        cnt.setObjectName('groupCount')

        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(6)
        bar.addWidget(self.head)
        bar.addWidget(cnt)
        bar.addStretch(1)

        self.body = QWidget(self)
        grid = QGridLayout(self.body)
        grid.setContentsMargins(4, 0, 0, 6)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(6)
        grid.setColumnMinimumWidth(0, _NAME_W)
        grid.setColumnStretch(1, 1)

        panel.editors[group] = {}
        panel.browse[group] = []
        for row, prm in enumerate(items):
            name = QLabel(prm.label)
            name.setObjectName('pname')
            name.setFixedWidth(_NAME_W)
            editor = make_editor(prm)
            editor.setToolTip(tip(prm))
            name.setToolTip(tip(prm))

            help_btn = QToolButton()
            help_btn.setObjectName('help')
            help_btn.setText('?')
            help_btn.setCursor(Qt.CursorShape.WhatsThisCursor)
            help_btn.setToolTip(tip(prm))
            help_btn.setFixedSize(_HELP_W, _HELP_W)

            grid.addWidget(name, row, 0)
            grid.addWidget(editor, row, 1)
            if prm.kind == 'dir':
                btn = QPushButton('…')
                btn.setObjectName('browse')
                btn.setFixedWidth(30)
                btn.setToolTip('选择目录')
                btn.clicked.connect(lambda _=False, k=prm.key: panel.pick_dir(k))
                grid.addWidget(btn, row, 2)
                panel.browse[group].append(btn)
            grid.addWidget(help_btn, row, 3)
            panel.editors[group][prm.key] = editor

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addLayout(bar)
        lay.addWidget(self.body)

        self.head.toggled.connect(self._on_toggle)
        self._on_toggle(expanded)

    def _on_toggle(self, on: bool) -> None:
        self.head.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)
        self.body.setVisible(on)


class ParamPanel(QWidget):
    """参数面板：三档折叠 + 读写 PwsParams + 运行中整体锁定"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.editors: Dict[str, Dict[str, object]] = {}
        self.browse: Dict[str, list] = {}

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        for i, group in enumerate(GROUP_ORDER):
            # 高级档默认折叠：常规使用仅涉及基础档与标准档
            lay.addWidget(Section(group, self, expanded=(group != '高级')))
            if i < len(GROUP_ORDER) - 1:
                line = QWidget()
                line.setObjectName('hline')
                line.setFixedHeight(1)
                lay.addWidget(line)
        lay.addStretch(1)

    # ---- 交互 ----
    def pick_dir(self, key: str) -> None:
        """「…」选择目录，起始路径取当前输入框中的值。"""
        for group, eds in self.editors.items():
            w = eds.get(key)
            if w is None:
                continue
            cur = w.text().strip()
            path = QFileDialog.getExistingDirectory(self, '选择目录', cur)
            if path:
                w.setText(path)
            return

    # ---- 读写 ----
    def values(self) -> PwsParams:
        """界面 → PwsParams（字段名与 Param.key 一一对应）"""
        data = {}
        for group, eds in self.editors.items():
            for key, w in eds.items():
                data[key] = read_editor(KEY_OF[key], w)
        return PwsParams(**data)

    def set_values(self, p: PwsParams) -> None:
        """PwsParams → 界面"""
        for group, eds in self.editors.items():
            for key, w in eds.items():
                write_editor(KEY_OF[key], w, getattr(p, key))

    def set_locked(self, locked: bool) -> None:
        """运行中锁定参数（「?」说明仍可查看）。"""
        for eds in self.editors.values():
            for w in eds.values():
                w.setEnabled(not locked)
        for btns in self.browse.values():
            for b in btns:
                b.setEnabled(not locked)


# key → Param 的索引（read/write 需要 kind/label 等信息）
KEY_OF = {p.key: p for p in PARAMS}
