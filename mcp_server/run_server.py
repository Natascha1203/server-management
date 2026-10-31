#!/usr/bin/env python
"""直接可执行的入口：``python /abs/path/mcp_server/run_server.py``

存在的意义是让 host 配置不必依赖 ``PYTHONPATH`` 或 ``cwd``——很多 MCP 客户端
只支持 ``command`` + ``args`` + ``env``，不保证工作目录。这里自己把包目录塞进
``sys.path``，于是「一个绝对路径」就能启动，Windows / Linux 写法一致。
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from sm_mcp.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main()
