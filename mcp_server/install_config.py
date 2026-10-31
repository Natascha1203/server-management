#!/usr/bin/env python
"""把 server-management MCP 注册到本机 host 的配置里（幂等、先备份）。

用法：
    python install_config.py --host workbuddy --base-url http://127.0.0.1:18888 --token xxx
    python install_config.py --host claude     --base-url https://sm.example.com --token xxx
    python install_config.py --host both       --base-url ... --token ...
    python install_config.py --host workbuddy  ... --dry-run    # 只看要写什么

为什么不用 JSON 手改：这两个文件都可能在跑着（Claude Code 会重写 ~/.claude.json），
手改容易把别的配置覆盖掉或写坏格式。这里读-合并-原子替换，并且先备份。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENTRY = HERE / "run_server.py"
SERVER_KEY = "server-management"

HOSTS = {
    "claude": {"label": "Claude Code CLI", "path": lambda: Path.home() / ".claude.json"},
    "workbuddy": {"label": "WorkBuddy", "path": lambda: Path.home() / ".workbuddy" / "mcp.json"},
}


def entry_for(base_url, token, python_exe):
    return {
        "type": "stdio",
        "command": python_exe,
        "args": [str(ENTRY)],
        "env": {"SM_BASE_URL": base_url, "SM_TOKEN": token},
    }


def load(path):
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError) as exc:
        raise SystemExit("无法读取 %s（%s）。文件可能是坏的，请先手工修好再跑本脚本。" % (path, exc))


def write_atomic(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(path.name + ".bak-" + stamp)
        shutil.copy2(path, backup)
        print("  已备份 -> %s" % backup)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def install(host_key, base_url, token, python_exe, dry_run):
    spec = HOSTS[host_key]
    path = spec["path"]()
    print("\n== %s" % spec["label"])
    print("  配置文件: %s" % path)
    payload = load(path)
    servers = payload.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        payload["mcpServers"] = servers
    existed = SERVER_KEY in servers
    servers[SERVER_KEY] = entry_for(base_url, token, python_exe)
    print("  %s %s" % ("更新已有条目" if existed else "新增条目", SERVER_KEY))
    if dry_run:
        print("  [dry-run] 将要写入:")
        print(json.dumps({SERVER_KEY: servers[SERVER_KEY]}, ensure_ascii=False, indent=2))
        return
    write_atomic(path, payload)
    print("  已写入")
    if host_key == "claude":
        print("  下一步：重启 Claude Code（或新开会话），用 /mcp 确认已连接。")
    else:
        print("  下一步：打开连接器管理页 → 右上角「自定义连接器」→ 对 server-management 点「信任」，然后新开会话。")


def main(argv=None):
    parser = argparse.ArgumentParser(description="注册 server-management MCP 到本机 host 配置")
    parser.add_argument("--host", required=True, choices=["claude", "workbuddy", "both"])
    parser.add_argument("--base-url", required=True, help="后端地址，如 http://127.0.0.1:18888")
    parser.add_argument("--token", required=True, help="账号的 API token（页面左下角钥匙 →「获取 Token」）")
    parser.add_argument("--python", default=sys.executable, help="启动 MCP 用的 python 绝对路径，默认当前解释器")
    parser.add_argument("--dry-run", action="store_true", help="只打印将写入的内容，不落盘")
    args = parser.parse_args(argv)

    if not ENTRY.is_file():
        raise SystemExit("找不到入口脚本：%s" % ENTRY)
    python_exe = args.python
    if not Path(python_exe).is_file():
        raise SystemExit("python 路径不存在：%s（用 --python 指定绝对路径）" % python_exe)

    base_url = args.base_url.rstrip("/")
    print("入口: %s" % ENTRY)
    print("python: %s" % python_exe)
    print("后端: %s" % base_url)
    print("token: %s…%s（%d 字符）" % (args.token[:4], args.token[-4:], len(args.token)))

    targets = ["claude", "workbuddy"] if args.host == "both" else [args.host]
    for host_key in targets:
        install(host_key, base_url, args.token, python_exe, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
