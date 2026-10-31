"""把 ``mcp_server/`` 目录实时打成 zip，供页面「下载 MCP」按钮使用。

为什么不预先生成一个 ``server_management_mcp.zip`` 提交在仓库里：那样每改一次
``tools.py`` 就得记得重打包，忘一次就发出旧包（agent 拿到的工具列表和接口对不上，
而且从外观上完全看不出来）。这里改为**请求时从磁盘打包**，下载到的永远是当前
正在跑的这份代码，版本号由文件内容哈希算出，可以和 ``/api/skill/manifest`` 一样
用来核对「手上这份是不是最新」。

零第三方依赖（只用标准库），与 MCP 服务器本身的约束一致。
"""
from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

# 解压后的顶层目录名。必须固定：host 配置里的 args 路径会变成
# <解压目录>/server_management_mcp/run_server.py
PACKAGE_ROOT = "server_management_mcp"

# 不该进包的目录 / 后缀（pyc 与解释器版本绑定，带过去只会添乱）
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".git", ".idea", "__MACOSX"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".swp"}
SKIP_NAMES = {".DS_Store"}


def mcp_dir(config=None):
    """定位 mcp_server 目录。

    默认取配置里的 ``mcp_dir``（app.ini，相对路径相对项目根目录），
    没配就退回项目根下的 ``mcp_server/``。
    """
    config = config or {}
    base = Path(config.get("base_dir") or Path(__file__).resolve().parent.parent)
    configured = config.get("mcp_dir")
    return Path(configured) if configured else base / "mcp_server"


def iter_files(root):
    """按稳定顺序产出要入包的文件（跳过缓存与隐藏垃圾文件）。"""
    root = Path(root)
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES or path.name in SKIP_NAMES:
            continue
        yield path


def package_version(root):
    """文件内容哈希的前 12 位。

    只看相对路径 + 内容，不看 mtime，所以「同样的代码在任何机器上版本一致」，
    便于把「agent 说我这边没有 sm_delete」这类问题定位成旧包还是旧进程。
    """
    digest = hashlib.sha256()
    root = Path(root)
    for path in iter_files(root):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:12]


def build_package(root):
    """返回 (zip 字节流, 版本号)。"""
    root = Path(root)
    files = list(iter_files(root))
    if not files:
        raise FileNotFoundError("MCP 服务目录里没有可打包的文件：%s" % root)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, "%s/%s" % (PACKAGE_ROOT, path.relative_to(root).as_posix()))
    return buffer.getvalue(), package_version(root)
