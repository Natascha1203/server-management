"""MCP server 的单元 + 端到端测试。

分两层：
1. 协议层用 io.StringIO 直接喂 JSON-RPC 行，验证 initialize / tools/list / tools/call
   的返回结构与「stdout 只能有协议消息」这条硬约束。
2. 端到端真的起一个 Flask 服务（127.0.0.1 随机端口），用 SMClient 走 HTTP 调
   sm_bootstrap / sm_whoami / sm_query，验证「MCP → 后端」这条链路。
"""
import io
import json
import threading

import pytest

from mcp_server.sm_mcp import server as mcp_server
from mcp_server.sm_mcp import tools as mcp_tools
from mcp_server.sm_mcp.client import SMClient, SMError


# ---------------------------------------------------------------- 桩客户端

class StubClient:
    """按路径返回固定 fixture，用来隔离网络。"""

    def __init__(self, payloads, texts=None):
        self.payloads = payloads
        self.texts = texts or {}
        self.calls = []

    def json(self, path, params=None, method="GET", payload=None):
        self.calls.append((path, params))
        if path not in self.payloads:
            raise SMError("stub 没有配置路径 %s" % path)
        value = self.payloads[path]
        return value(params) if callable(value) else value

    def text(self, path, params=None):
        self.calls.append((path, params))
        return self.texts.get(path, "")


SCHEMA = {
    "schema_version": 1,
    "entities": {
        "servers": {
            "label": "服务器",
            "key": ["序列号"],
            "headers": ["序列号", "品牌", "所在机房", "所属机位"],
            "aliases": {"归属": "归属单位"},
            "can_view": ["管理人员", "机房人员", "业务人员"],
            "can_mutate": ["管理人员", "机房人员", "业务人员"],
        }
    },
    "rack_headers": ["序列号", "请求类型", "客户id"],
    "rack_export_headers": ["序列号", "请求类型", "客户id"],
    "rack_aliases": {"成本": "服务器成本(万)"},
    "rack_stages": ["allocation", "implementation", "rejected"],
    "rack_statuses": ["pending", "approved", "rejected"],
    "capabilities": {"管理人员": ["manage_all"]},
    "rules": ["所属机位格式必须是 N-N"],
}

MANIFEST = {
    "name": "server-management",
    "version": "abc123def456",
    "updated_at": "2026-10-31 10:00:00",
    "files": [],
    "endpoints": {},
    "counts": {"entities": 1, "fields": {"servers": 4}, "rack_headers": 3},
}

ME = {
    "用户id": "biz1",
    "身份": "业务人员",
    "业务单位": "单位A",
    "所属机房": "",
    "capabilities": ["submit_rack_request"],
}


def stub(**overrides):
    payloads = {
        "/api/skill/manifest": MANIFEST,
        "/api/skill/schema": SCHEMA,
        "/api/me": ME,
    }
    payloads.update(overrides.pop("payloads", {}))
    texts = {"/api/skill/docs": "# SKILL 正文\n铁律若干"}
    texts.update(overrides.pop("texts", {}))
    return StubClient(payloads, texts)


# ---------------------------------------------------------------- 工具层

class TestBootstrap:
    def test_contains_version_doc_fields_and_identity(self):
        text = mcp_tools.call_tool("sm_bootstrap", {}, stub())
        assert "skill vabc123def456" in text
        assert "SKILL 正文" in text
        # 字段清单必须来自 schema，而不是硬编码
        assert "序列号 | 品牌 | 所在机房 | 所属机位" in text
        assert "所属机位格式必须是 N-N" in text
        assert "业务人员" in text
        assert "待审批" in text, "非管理员必须提示写操作会进审批流"

    def test_admin_gets_direct_apply_hint(self):
        text = mcp_tools.call_tool("sm_bootstrap", {}, stub(payloads={
            "/api/me": dict(ME, 身份="管理人员", capabilities=["manage_all"]),
        }))
        assert "直接生效" in text

    def test_works_without_token(self):
        """读文档不需要 token：/api/me 返回 401 时仍应给出文档与字段。"""
        def denied(params):
            raise SMError("token 无效或已失效")

        text = mcp_tools.call_tool("sm_bootstrap", {}, stub(payloads={"/api/me": denied}))
        assert "skill vabc123def456" in text
        assert "序列号 | 品牌" in text
        assert "拿不到身份" in text

    def test_can_skip_rules(self):
        text = mcp_tools.call_tool("sm_bootstrap", {"include_rules": False}, stub())
        assert "所属机位格式必须是 N-N" not in text


class TestSchemaTool:
    def test_full_schema_is_json(self):
        text = mcp_tools.call_tool("sm_schema", {}, stub())
        assert json.loads(text)["schema_version"] == 1

    def test_single_entity_keeps_rules(self):
        body = json.loads(mcp_tools.call_tool("sm_schema", {"entity": "servers"}, stub()))
        assert body["entity"] == "servers"
        assert body["definition"]["headers"][0] == "序列号"
        assert body["rules"]

    def test_unknown_entity_lists_available(self):
        with pytest.raises(SMError) as exc:
            mcp_tools.call_tool("sm_schema", {"entity": "nope"}, stub())
        assert "servers" in str(exc.value)


class TestQueryTool:
    def records(self, params):
        return {
            "records": [
                {"序列号": "SN-1", "品牌": "Dell", "所在机房": "机房A", "所属机位": "9-10"},
                {"序列号": "SN-2", "品牌": "HP", "所在机房": "机房B", "所属机位": "11-12"},
            ],
            "meta": SCHEMA["entities"]["servers"],
            "record_state": "official",
        }

    def test_lists_all_with_counts(self):
        text = mcp_tools.call_tool("sm_query", {"entity": "servers"}, stub(payloads={
            "/api/servers": self.records,
        }))
        assert "共 2 条匹配，显示前 2 条" in text
        assert "SN-1" in text and "SN-2" in text

    def test_filters_are_contains_and_case_insensitive(self):
        text = mcp_tools.call_tool("sm_query", {
            "entity": "servers", "filters": {"品牌": "dell"},
        }, stub(payloads={"/api/servers": self.records}))
        assert "共 1 条匹配" in text
        assert "SN-1" in text and "SN-2" not in text

    def test_field_name_with_spaces_still_matches(self):
        text = mcp_tools.call_tool("sm_query", {
            "entity": "servers", "filters": {"所在 机房": "机房A"},
        }, stub(payloads={"/api/servers": self.records}))
        assert "共 1 条匹配" in text

    def test_unknown_field_does_not_guess(self):
        with pytest.raises(SMError) as exc:
            mcp_tools.call_tool("sm_query", {
                "entity": "servers", "filters": {"不存在的列": "x"},
            }, stub(payloads={"/api/servers": self.records}))
        assert "sm_schema" in str(exc.value)

    def test_limit_truncates_and_says_so(self):
        text = mcp_tools.call_tool("sm_query", {"entity": "servers", "limit": 1}, stub(payloads={
            "/api/servers": self.records,
        }))
        assert "显示前 1 条" in text
        assert "SN-2" not in text
        assert "加大 limit" in text

    def test_missing_entity(self):
        with pytest.raises(SMError) as exc:
            mcp_tools.call_tool("sm_query", {}, stub())
        assert "entity" in str(exc.value)


class TestRackTools:
    def requests(self, params):
        status = (params or {}).get("status", "pending")
        rows = [{
            "id": "req-1", "status": status, "stage": "implementation",
            "request_type": "上架", "serial_number": "SN-9",
            "request_data": json.dumps({"客户名称": "甲客户", "所在机房": "机房A"}),
            "submitter": "biz1", "submitted_at": "2026-10-31 09:00:00",
        }] if status == "approved" else []
        return {"requests": rows}

    def test_list_renders_summary_table(self):
        text = mcp_tools.call_tool("sm_rack_list", {"status": "approved"}, stub(payloads={
            "/api/rack-requests": self.requests,
        }))
        assert "req-1" in text and "SN-9" in text and "甲客户" in text
        assert "sm_rack_get" in text

    def test_get_searches_all_statuses(self):
        """后端 status 是等值过滤，没有 all，所以 tool 必须逐个状态查。"""
        text = mcp_tools.call_tool("sm_rack_get", {"request_id": "req-1"}, stub(payloads={
            "/api/rack-requests": self.requests,
        }))
        assert "req-1" in text and "甲客户" in text

    def test_get_missing_raises(self):
        with pytest.raises(SMError):
            mcp_tools.call_tool("sm_rack_get", {"request_id": "nope"}, stub(payloads={
                "/api/rack-requests": self.requests,
            }))


# ---------------------------------------------------------------- 协议层

def run_stdio(lines, client):
    stdin = io.StringIO("\n".join(lines) + "\n")
    stdout = io.StringIO()
    mcp_server.serve(stdin=stdin, stdout=stdout, client=client)
    return [json.loads(line) for line in stdout.getvalue().strip().split("\n") if line.strip()]


def rpc(msg_id, method, params=None):
    message = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        message["params"] = params
    return json.dumps(message, ensure_ascii=False)


class TestProtocol:
    def test_initialize_echoes_supported_version(self):
        out = run_stdio([rpc(1, "initialize", {"protocolVersion": "2025-06-18"})], stub())
        result = out[0]["result"]
        assert result["protocolVersion"] == "2025-06-18"
        assert result["serverInfo"]["name"] == "server-management"
        assert "tools" in result["capabilities"]
        assert "sm_bootstrap" in result["instructions"]

    def test_unknown_protocol_version_falls_back(self):
        out = run_stdio([rpc(1, "initialize", {"protocolVersion": "1999-01-01"})], stub())
        assert out[0]["result"]["protocolVersion"] == "2024-11-05"

    def test_notifications_get_no_response(self):
        out = run_stdio([
            rpc(1, "initialize", {"protocolVersion": "2024-11-05"}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        ], stub())
        assert len(out) == 1

    def test_tools_list_has_full_json_schema(self):
        out = run_stdio([rpc(1, "tools/list")], stub())
        names = [tool["name"] for tool in out[0]["result"]["tools"]]
        assert "sm_bootstrap" in names
        for tool in out[0]["result"]["tools"]:
            assert tool["description"]
            assert tool["inputSchema"]["type"] == "object"

    def test_tools_call_returns_text_content(self):
        out = run_stdio([rpc(1, "tools/call", {"name": "sm_schema", "arguments": {}})], stub())
        result = out[0]["result"]
        assert result["isError"] is False
        assert result["content"][0]["type"] == "text"
        assert "schema_version" in result["content"][0]["text"]

    def test_tool_error_is_readable_not_crash(self):
        out = run_stdio([rpc(1, "tools/call", {"name": "sm_query", "arguments": {}})], stub())
        result = out[0]["result"]
        assert result["isError"] is True
        assert "entity" in result["content"][0]["text"]

    def test_unknown_tool(self):
        out = run_stdio([rpc(1, "tools/call", {"name": "nope", "arguments": {}})], stub())
        assert out[0]["result"]["isError"] is True
        assert "未知工具" in out[0]["result"]["content"][0]["text"]

    def test_unknown_method_returns_error_code(self):
        out = run_stdio([rpc(1, "no/such")], stub())
        assert out[0]["error"]["code"] == -32601

    def test_bad_json_does_not_kill_server(self):
        out = run_stdio(["{not json", rpc(2, "ping")], stub())
        assert out[0]["error"]["code"] == -32700
        assert out[1]["result"] == {}

    def test_empty_resource_lists_are_success(self):
        out = run_stdio([
            rpc(1, "resources/list"),
            rpc(2, "prompts/list"),
        ], stub())
        assert out[0]["result"] == {"resources": []}
        assert out[1]["result"] == {"prompts": []}

    def test_ping(self):
        out = run_stdio([rpc(1, "ping")], stub())
        assert out[0]["result"] == {}

    def test_stdout_has_only_protocol_messages(self):
        """一行多余输出就会让 host 判定握手失败，所以逐行都必须是合法 JSON。"""
        stdout = io.StringIO()
        mcp_server.serve(
            stdin=io.StringIO(rpc(1, "tools/list") + "\n"),
            stdout=stdout,
            client=stub(),
        )
        for line in stdout.getvalue().splitlines():
            assert json.loads(line)["jsonrpc"] == "2.0"


# ---------------------------------------------------------------- 端到端

@pytest.fixture
def live_backend(app_env):
    """真的起一个 HTTP 服务，让 SMClient 走网络而不是 test_client。"""
    from werkzeug.serving import make_server

    app = app_env["app"]
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_port
    finally:
        server.shutdown()
        thread.join(timeout=5)


class TestEndToEnd:
    def _token(self, app_env, user_id="admin"):
        from app import storage

        return storage.get_users()[user_id]["api_token"] or storage.generate_user_api_token(user_id)

    def test_bootstrap_over_http(self, app_env, live_backend):
        client = SMClient(base_url=live_backend, token=self._token(app_env))
        text = mcp_tools.call_tool("sm_bootstrap", {}, client)
        assert "skill v" in text
        assert "序列号" in text
        assert "管理人员" in text

    def test_whoami_over_http(self, app_env, live_backend):
        client = SMClient(base_url=live_backend, token=self._token(app_env))
        me = json.loads(mcp_tools.call_tool("sm_whoami", {}, client))
        assert me["用户id"] == "admin"
        assert me["身份"] == "管理人员"

    def test_query_over_http(self, app_env, live_backend):
        client = SMClient(base_url=live_backend, token=self._token(app_env))
        text = mcp_tools.call_tool("sm_query", {"entity": "servers"}, client)
        assert "共 0 条匹配" in text

    def test_bad_token_is_readable(self, live_backend):
        client = SMClient(base_url=live_backend, token="wrong-token")
        with pytest.raises(SMError) as exc:
            mcp_tools.call_tool("sm_whoami", {}, client)
        assert "token 无效" in str(exc.value)

    def test_unreachable_backend_is_readable(self):
        client = SMClient(base_url="http://127.0.0.1:9", token="x", timeout=2)
        with pytest.raises(SMError) as exc:
            mcp_tools.call_tool("sm_whoami", {}, client)
        assert "后端不可达" in str(exc.value)

    def test_schema_reflects_backend_entities(self, app_env, live_backend):
        """真实后端返回的字段数必须等于 core.ENTITIES，证明没有写死副本。"""
        from app.core import ENTITIES

        client = SMClient(base_url=live_backend, token=self._token(app_env))
        schema = json.loads(mcp_tools.call_tool("sm_schema", {}, client))
        for key, meta in ENTITIES.items():
            assert schema["entities"][key]["headers"] == list(meta["headers"])
