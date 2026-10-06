# 20261005 DWT 后台线程：跑 PWS 引擎，把三钩子转成线程安全队列
#
# 为什么必须另起线程：一次叠加是分钟到小时级；跑在主线程会让窗口假死，
#   而"终止"按钮恰恰要在那时候能用。
# 引擎不 import 任何界面代码（命令行下也能跑），两者只经 self.q 传递消息：
#   ('log', str) / ('progress', phase, frac, text) / ('done', dict)
#   / ('cancelled',) / ('error', str)
# 界面在自己的定时器里 pump() 取回，不存在跨线程直接改 UI。

from __future__ import annotations

import queue
import threading
import traceback

import pws
from pws_params import PwsParams


class StackWorker(threading.Thread):
    """一次叠加 = 一个 StackWorker（不可复用）"""

    def __init__(self, params: PwsParams):
        super().__init__(daemon=True)
        self.params = params
        self.q: queue.Queue = queue.Queue()
        self._cancel = threading.Event()

    def cancel(self) -> None:
        """只置位；引擎在自己的检查点读它并抛出 Cancelled"""
        self._cancel.set()

    def pump(self):
        """取空队列（界面线程调用）"""
        out = []
        while True:
            try:
                out.append(self.q.get_nowait())
            except queue.Empty:
                break
        return out

    def run(self) -> None:
        try:
            res = pws.run(
                self.params,
                on_log=lambda s: self.q.put(('log', s)),
                on_progress=lambda phase, frac, text:
                    self.q.put(('progress', phase, float(frac), text)),
                should_cancel=self._cancel.is_set,
            )
        except pws.Cancelled:
            self.q.put(('cancelled',))
            return
        except SystemExit as e:           # 引擎的参数/环境类错误（挑盘失败、星表不足…）
            self.q.put(('error', str(e)))
            return
        except Exception:
            self.q.put(('error', traceback.format_exc()))
            return
        self.q.put(('done', res))
