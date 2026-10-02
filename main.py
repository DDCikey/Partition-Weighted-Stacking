# DWT 入口
# 本文件以 MIT 许可证发布，全文见 LICENSE，授权范围见 README.md。
#
#     python main.py                图形界面
#     python main.py --cli <参数…>   命令行（批处理）
#
# 两条入口共用 core/pws_params.py 的参数定义。

from __future__ import annotations

import multiprocessing
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent                      # 仓库根
for _p in (str(_HERE), str(_HERE / 'core'), str(_HERE / 'ui')):
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
