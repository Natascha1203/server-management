"""MCP stdio 协议层。

分帧方式：**一行一个 JSON-RPC 消息**（MCP stdio transport 的规定，不是 LSP 的
Content-Length 头）。因此 stdout 只能出现协议消息，任何日志都必须走 stderr——
一行多余输出就会让 host 判定握手失败。

只实现 tools（外加空的 resources/prompts 列表），因为这是各家 host 支持度最一致
的部分；文档自更新由 sm_bootstrap 工具完成，不依赖 InitializeResult.instructions。
instructions 仍然会填，但只是「有则增强」，缺了不影响功能。
"""
from __future__ import annotations

import json
import sys
import traceback

from . import __version__
from .client import SMClient, SMError
from .tools import call_tool, visible_tool_specs

# 客户端报上来的协议版本在列表里就原样回显（tools 能力在这些版本间没变），
# 否则退回最早支持的版本，避免握手中断。
PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")

SERVER_INFO = {"name": "server-management", "version": __version__}

INSTRUCTIONS = (
    "这是「服务器资产管理系统」的 MCP。每次会话先调用 sm_bootstrap：它会返回当前 "
    "skill 正文、版本号和**实时字段清单**。写记录前用 sm_schema 确认列名，不要凭记忆拼字段。"
    "写操作在结果里会显式给出 applied:true（已落库）或 pending:true + change_id（待审批），"
    "不要靠 message 文案判断是否生效。"
)


class MethodNotFound(Exception):
    pass


class IdentityCache:
    """懒加载的「我是谁」，只用于裁剪 tools/list。

    `initialize` 阶段不碰网络（后端慢或挂了不该拖住握手），第一次 `tools/list` 才去取
    `/api/me`，取到就缓存——一个进程一枚 token，身份不会中途变。
    取不到（未配 token / 后端不可达）时 capabilities 为 None，工具列表不裁剪：
    宁可让 agent 看到工具、调用后被后端 403 挡掉，也不要让它以为工具不存在。
    """

    def __init__(self, client):
        self.client = client
        self._loaded = False
        self._capabilities = None

    def capabilities(self):
        if self._loaded:
            return self._capabilities
        self._loaded = True
        try:
            me = self.client.json("/api/me") or {}
            self._capabilities = list(me.get("capabilities") or [])
        except Exception:
            self._capabilities = None
        return self._capabilities


def log(message):
    sys.stderr.write("[sm_mcp] %s\n" % message)
    sys.stderr.flush()


def _send(stdout, payload):
    stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    stdout.flush()


def handle(method, params, client, identity=None):
    if method == "initialize":
        requested = str(params.get("protocolVersion") or "")
        version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        capabilities = identity.capabilities() if identity is not None else None
        return {"tools": visible_tool_specs(capabilities)}
    if method == "tools/call":
        name = str(params.get("name") or "")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        try:
            text = call_tool(name, args, client)
        except SMError as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        except Exception as exc:  # 工具内部异常也要变成可读文本，不能让 agent 只看到堆栈
            log("tool %s crashed: %s" % (name, traceback.format_exc()))
            return {
                "content": [{"type": "text", "text": "工具 %s 执行失败：%s" % (name, exc)}],
                "isError": True,
            }
        return {"content": [{"type": "text", "text": text}], "isError": False}
    if method == "resources/list":
        return {"resources": []}
    if method == "resources/templates/list":
        return {"resourceTemplates": []}
    if method == "prompts/list":
        return {"prompts": []}
    raise MethodNotFound(method)


def serve(stdin=None, stdout=None, client=None):
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    client = client or SMClient()
    identity = IdentityCache(client)
    log("started: base_url=%s token=%s" % (
        getattr(client, "base_url", "-"),
        "configured" if getattr(client, "token", "") else "MISSING",
    ))
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            _send(stdout, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "JSON 解析失败"}})
            continue
        if not isinstance(message, dict) or "id" not in message:
            # 通知（如 notifications/initialized）不需要响应
            continue
        msg_id = message["id"]
        method = str(message.get("method") or "")
        params = message.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        try:
            result = handle(method, params, client, identity)
        except MethodNotFound as exc:
            _send(stdout, {
                "jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32601, "message": "未实现的方法：%s" % exc},
            })
            continue
        except Exception as exc:
            log("handler crashed: %s" % traceback.format_exc())
            _send(stdout, {
                "jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32603, "message": "MCP server 内部错误：%s" % exc},
            })
            continue
        if result is None:
            continue
        _send(stdout, {"jsonrpc": "2.0", "id": msg_id, "result": result})
