"""入口：``python -m sm_mcp``

必须把 stdin/stdout 强制成 UTF-8：Windows 默认是 GBK，中文文档写出去会直接
UnicodeEncodeError，而 host 只会看到「握手失败」，极难排查。
"""
import sys

from .server import serve


def _force_utf8(stream, newline=None):
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", newline=newline)
        except (ValueError, OSError):
            pass


def main():
    _force_utf8(sys.stdin)
    _force_utf8(sys.stdout, newline="\n")
    serve()


if __name__ == "__main__":
    main()
