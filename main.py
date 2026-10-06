# DWT 入口
# 20261005 界面迁到 WevvMold：sys.path 加入 WevvMoldGUI_for_python\python
#
#     python DWT/main.py                图形界面
#     python DWT/main.py --cli <参数…>   命令行（批处理，以及与 v2 做等价性回归）
#
# 两条入口共用 core/pws_params.py 那一份参数定义，不存在第二套默认值。

from __future__ import annotations

import multiprocessing
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent                      # DWT/
for _p in (str(_HERE.parent), str(_HERE / 'core'), str(_HERE / 'ui'),
           str(_HERE.parent / 'WevvMoldGUI_for_python' / 'python')):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def main(argv=None) -> int:
    # 引擎用多进程并行，打包成 exe 后子进程要靠这句引导回多进程引导程序，
    #   否则 Windows 的 spawn 会把界面整个再开一遍。源码运行时是空操作。
    multiprocessing.freeze_support()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == '--cli':
        from pws import main as cli_main
        return cli_main(argv[1:])
    from app import main as gui_main
    return gui_main([sys.argv[0]])


if __name__ == '__main__':
    raise SystemExit(main())
