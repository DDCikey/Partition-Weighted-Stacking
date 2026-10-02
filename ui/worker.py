# DWT 后台线程：执行 PWS 引擎，将引擎的三个钩子转换为 Qt 信号。
# 本文件以 MIT 许可证发布，全文见 LICENSE，授权范围见 README.md。
#
# 引擎运行耗时在分钟至小时量级，须置于独立线程，以免阻塞界面主线程；
# 终止操作同样在该线程运行期间可用。
# 引擎不引入任何 Qt 依赖（命令行下亦可运行），两者经由下列五个信号连接。

from __future__ import annotations

import threading
import traceback

from PySide6.QtCore import QThread, Signal

import pws
from pws_params import PwsParams


class StackWorker(QThread):
    """一次叠加对应一个 StackWorker，不可复用。"""

    sig_log = Signal(str)                 # 引擎输出的每一行日志
    sig_progress = Signal(str, float, str)  # (阶段, 全局百分比 0~1, 说明)
    sig_done = Signal(dict)               # 正常结束，携带引擎返回的结果字典
    sig_cancelled = Signal()              # 收到终止请求后引擎退出
    sig_error = Signal(str)               # 异常文本（含 traceback）

    def __init__(self, params: PwsParams, parent=None):
        super().__init__(parent)
        self.params = params
        self._cancel = threading.Event()

    def cancel(self) -> None:
        """仅置位取消标志；引擎在自身的检查点读取该标志并抛出 Cancelled。"""
        self._cancel.set()

    def run(self) -> None:                # noqa: D102 - QThread 入口
        try:
            res = pws.run(
                self.params,
                on_log=self.sig_log.emit,
                on_progress=lambda phase, frac, text:
                    self.sig_progress.emit(phase, frac, text),
                should_cancel=self._cancel.is_set,
            )
        except pws.Cancelled:
            self.sig_cancelled.emit()
            return
        except SystemExit as e:           # 引擎的参数/环境类错误（选盘失败、星表不足等）
            self.sig_error.emit(str(e))
            return
        except Exception:
            self.sig_error.emit(traceback.format_exc())
            return
        self.sig_done.emit(res)
