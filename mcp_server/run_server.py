#!/usr/bin/env python
"""MCP 入口 + 自更新引导器：``python /abs/path/mcp_server/run_server.py``

这个文件有两层身份
------------------
1. **host 配置里唯一需要写死的路径**。所有 host（WorkBuddy / CodeBuddy / Claude Code）
   的 args 都指向它，所以它的位置一旦装好就不能再变——变一次就得让所有用户改配置。
2. **启动期的版本检查与热替换**。真正的业务代码放在用户主目录的「版本库」里：

       ~/.server-management-mcp/            （可用 SM_MCP_HOME 改位置）
       ├── versions/
       │   ├── 1a2b3c4d5e6f/               ← 一份完整的包（目录名 = 内容哈希）
       │   │   ├── sm_mcp/ ... run_server.py
       │   │   └── ...
       │   └── 9f8e7d6c5b4a/
       ├── current.txt                      ← 一行文本：现在跑哪一份
       └── last_check.txt                   ← 上次问后端的时刻（去抖用）

为什么要搞这一层，而不是就地覆盖解压目录
--------------------------------------
- **覆盖不生效还没人发现**：MCP 是常驻进程，模块启动时已加载，覆盖磁盘文件后进程仍跑旧代码，
  直到重启。用户以为更新了，其实没有。
- **可能读到半新半旧**：另一个 host 恰好在覆盖途中启动，会 import 到混合版本，报莫名的 ImportError。
- **删掉的模块会残留**：解压是「覆盖已有文件」，不会删掉包里已不存在的文件，幽灵模块会一直被 import。
- **无法回滚**：覆盖完旧代码就没了。
- **多 host 互踩**：同一台机器的三个 host 往往指向同一个解压目录。

新版本一律解压到**全新目录**，切换只改 current.txt 一行（原子替换），旧版本原样留着，
回滚 = 把 current.txt 改回去。策略上：只保留最近 N 份（默认 3），且永不删除当前在用的那份。

失败时绝不阻断启动
------------------
后端不可达、token 失效、包坏了 —— 一律退回「继续用现有版本」并在 stderr 记一行，
绝不让版本检查拖垮 MCP 握手。只有在版本库为空（首次安装）且下载也失败时才没有代码可跑。

可调的环境变量
--------------
    SM_MCP_HOME          版本库位置，默认 ~/.server-management-mcp
    SM_MCP_AUTO_UPDATE   0 关闭自动更新：仍然问版本、日志里提示有新版本，但不下载
    SM_MCP_CHECK_TTL     两次版本检查的最小间隔秒数，默认 60；0 = 每次都查
    SM_MCP_KEEP_VERSIONS 保留几份历史版本，默认 3
    SM_BASE_URL/SM_TOKEN 与业务代码共用，这里用它们去问 /api/mcp/version
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_BASE_URL = "http://127.0.0.1:18888"
PACKAGE_ROOT = "server_management_mcp"
VERSION_HEADER = "X-MCP-Version"
# 版本检查要快：后端挂了也别拖住 MCP 握手
CHECK_TIMEOUT = 3.0
DOWNLOAD_TIMEOUT = 30.0
DEFAULT_KEEP = 3


def log(message):
    sys.stderr.write("[sm_mcp.launcher] %s\n" % message)
    sys.stderr.flush()


# ---------------------------------------------------------------- 环境

def home_dir():
    configured = (os.environ.get("SM_MCP_HOME") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".server-management-mcp"


def base_url():
    return (os.environ.get("SM_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def token():
    return (os.environ.get("SM_TOKEN") or "").strip()


def _env_flag(name, default=True):
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def auto_update_enabled():
    return _env_flag("SM_MCP_AUTO_UPDATE", True)


def check_ttl():
    try:
        return max(0.0, float((os.environ.get("SM_MCP_CHECK_TTL") or "60").strip()))
    except ValueError:
        return 60.0


def keep_versions():
    try:
        return max(1, int((os.environ.get("SM_MCP_KEEP_VERSIONS") or str(DEFAULT_KEEP)).strip()))
    except ValueError:
        return DEFAULT_KEEP


# ---------------------------------------------------------------- 版本库读写

def versions_dir(home=None):
    return (home or home_dir()) / "versions"


def read_current(home=None):
    try:
        return ((home or home_dir()) / "current.txt").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def write_current(home, version):
    """原子替换 current.txt：绝不能出现「文件存在但内容为空」的中间态。"""
    target = Path(home) / "current.txt"
    temp = target.with_name("current.txt.tmp-%d" % os.getpid())
    temp.write_text(version + "\n", encoding="utf-8")
    os.replace(temp, target)


def installed_dir(home, version):
    """返回该版本可用的代码目录；版本未知或目录不完整时返回 None。"""
    if not version:
        return None
    path = versions_dir(home) / version
    if (path / "sm_mcp" / "__init__.py").is_file():
        return path
    return None


def prune_versions(home, current):
    """只保留最近 N 份，且永不删除正在用的那份。"""
    root = versions_dir(home)
    if not root.is_dir():
        return
    candidates = [
        path for path in root.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    ]
    candidates.sort(key=lambda path: _mtime(path), reverse=True)
    for path in candidates[keep_versions():]:
        if path.name == current:
            continue
        shutil.rmtree(path, ignore_errors=True)


def _mtime(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def install_version(home, version, blob):
    """把 zip 解压成 versions/<version>/。

    先解到临时目录再 rename：rename 到「已存在」的目录会失败（Windows 与 POSIX 都是），
    正好用来发现「另一个 host 进程刚装好同一版本」这个竞态，失败就直接复用它。
    """
    root = versions_dir(home)
    root.mkdir(parents=True, exist_ok=True)
    target = root / version
    if target.is_dir():
        return target
    temp = Path(tempfile.mkdtemp(prefix=".tmp-", dir=str(root)))
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            for name in archive.namelist():
                parts = Path(name).parts
                if name.startswith("/") or ".." in parts or (parts and ":" in parts[0]):
                    raise ValueError("包内路径非法：%s" % name)
            archive.extractall(temp)
        inner = temp / PACKAGE_ROOT
        if not (inner / "sm_mcp" / "__init__.py").is_file():
            raise ValueError("包里缺少 %s/sm_mcp/__init__.py，可能不是 MCP 包" % PACKAGE_ROOT)
        try:
            os.rename(inner, target)
        except OSError:
            if not target.is_dir():
                raise
    finally:
        shutil.rmtree(temp, ignore_errors=True)
    return target


# ---------------------------------------------------------------- 与后端通信

def _headers():
    headers = {"Accept": "application/json"}
    value = token()
    if value:
        headers["Authorization"] = "Bearer " + value
    return headers


def fetch_remote_version(timeout=CHECK_TIMEOUT):
    request = urllib.request.Request(base_url() + "/api/mcp/version", headers=_headers())
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return str(payload.get("version") or "")


def download_package(timeout=DOWNLOAD_TIMEOUT):
    """返回 (zip 字节, 响应头里的版本号)。"""
    request = urllib.request.Request(base_url() + "/api/mcp/download", headers=_headers())
    with urllib.request.urlopen(request, timeout=timeout) as response:
        blob = response.read()
        version = (response.headers.get(VERSION_HEADER) or "").strip()
    if not blob.startswith(b"PK"):
        raise ValueError("下载到的内容不是 zip（可能被网关改写）")
    return blob, version


def _stamp_path(home):
    return Path(home) / "last_check.txt"


def _checked_recently(home, ttl):
    if ttl <= 0:
        return False
    path = _stamp_path(home)
    try:
        return (time.time() - path.stat().st_mtime) < ttl
    except OSError:
        return False


def _write_stamp(home):
    try:
        home = Path(home)
        home.mkdir(parents=True, exist_ok=True)
        _stamp_path(home).write_text(str(int(time.time())), encoding="utf-8")
    except OSError:
        pass


def check_and_update(home, current):
    """问一次后端，必要时下载安装。返回新的版本号（未变化/失败时原样返回 current）。"""
    if _checked_recently(home, check_ttl()):
        return current
    try:
        remote = fetch_remote_version()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # 后端不可达 / 401 / 返回非 JSON：都不该拦住启动，下次再试
        log("版本检查失败，继续使用现有代码：%s" % exc)
        _write_stamp(home)
        return current
    _write_stamp(home)
    if not remote:
        log("后端没有返回版本号，跳过更新")
        return current
    if remote == current and installed_dir(home, remote) is not None:
        return current

    if not auto_update_enabled():
        log("发现新版本 %s（当前 %s），但 SM_MCP_AUTO_UPDATE 已关闭；"
            "重新下载 MCP 包，或去掉该变量后重启。" % (remote, current or "无"))
        return current

    try:
        blob, header_version = download_package()
        if header_version and header_version != remote:
            # 检查与下载之间后端刚好又发了一版：以包里自带版本为准，避免目录名与内容不符
            remote = header_version
        install_version(home, remote, blob)
        write_current(home, remote)
        prune_versions(home, remote)
    except (urllib.error.URLError, OSError, ValueError, zipfile.BadZipFile) as exc:
        log("更新到 %s 失败，继续使用 %s：%s" % (remote, current or "解压目录（兜底）", exc))
        return current
    log("已更新 %s -> %s（%s）" % (current or "(首次安装)", remote, versions_dir(home) / remote))
    return remote


# ---------------------------------------------------------------- 启动

def resolve_code_dir():
    """决定这次该跑哪份代码，返回目录路径；同时把版本号写进环境变量供业务代码上报。"""
    home = home_dir()
    current = read_current(home)
    code_dir = installed_dir(home, current)
    if code_dir is None:
        if current:
            log("current.txt 指向的版本 %s 不存在或已损坏，按未安装处理" % current)
        current = ""

    # 总是走一次检查：关掉自动更新时它只问版本、记一行日志，不下载（见 check_and_update）
    current = check_and_update(home, current)
    code_dir = installed_dir(home, current)

    if code_dir is None:
        # 版本库还没内容（首次启动且下载没成功）：跑解压目录这一份，功能照常
        os.environ["SM_MCP_RUNNING_VERSION"] = ""
        log("使用解压目录（尚未装入版本库）：%s" % HERE)
        return HERE

    os.environ["SM_MCP_RUNNING_VERSION"] = current
    return code_dir


def main():
    code_dir = resolve_code_dir()
    # 必须排在 sys.path 最前：python 默认把「脚本所在目录」(这里是 HERE) 放在 sys.path[0]，
    # 若版本库里也有 sm_mcp，不抢先就会 import 到解压目录那份旧代码。
    if str(code_dir) not in sys.path:
        sys.path.insert(0, str(code_dir))
    from sm_mcp.__main__ import main as run_server

    run_server()


if __name__ == "__main__":
    main()
