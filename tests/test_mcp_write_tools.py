"""P2 写工具的单元测试。

全部用桩客户端隔离网络，逐条盯住写操作最容易出事的几处：
- `applied` / `pending` 两种返回语义有没有翻译对（agent 靠这个决定要不要再审批）
- 删除的二次确认（confirm_keys）是不是真的能挡住不一致的调用
- `sm_rack_review` 的 stage 前置校验有没有拦住错误调用且不产生副作用
- 短时幂等是否真的避免了重复提交
"""
import json

import pytest

from mcp_server.sm_mcp import server as mcp_server
from mcp_server.sm_mcp import tools as mcp_tools
from mcp_server.sm_mcp.client import SMError

SERVERS = {
    "records": [
        {"序列号": "SN-1", "品牌": "Dell", "所在机房": "机房A", "所属机位": "9-10"},
        {"序列号": "SN-2", "品牌": "HP", "所在机房": "机房B", "所属机位": "11-12"},
    ],
    "meta": {
        "label": "服务器",
        "key": ["序列号"],
        "headers": ["序列号", "品牌", "所在机房", "所属机位"],
    },
}

CABINETS = {
    "records": [{"机房": "机房A", "机柜": "D01", "机位": "1-2"}],
    "meta": {"label": "机柜", "key": ["机房", "机柜"], "headers": ["机房", "机柜", "机位"]},
}

ADMIN_SAVED = {"message": "已保存并直接生效", "change_id": "chg-1"}
BIZ_SUBMITTED = {"message": "已提交新增申请", "change_id": "chg-1"}


class WriteStub:
    """记录每次调用（含 method / payload），便于断言「工具到底发了什么、发了几次」。"""

    def __init__(self, gets=None, posts=None, errors=None, me=None):
        self.gets = gets or {}
        self.posts = posts or {}
        self.errors = errors or {}
        self.me = me or {"用户id": "biz1", "身份": "业务人员", "capabilities": ["submit_rack_request"]}
        self.calls = []

    def json(self, path, params=None, method="GET", payload=None):
        self.calls.append({"path": path, "params": params, "method": method, "payload": payload})
        if path in self.errors:
            raise self.errors[path]
        if path == "/api/me":
            return self.me
        table = self.posts if method == "POST" else self.gets
        if path not in table:
            raise SMError("stub 没有配置 %s %s" % (method, path))
        value = table[path]
        if callable(value):
            return value(params, payload)
        return value

    def text(self, path, params=None):
        self.calls.append({"path": path, "params": params, "method": "GET", "payload": None})
        return ""

    def posts_to(self, path):
        return [c for c in self.calls if c["method"] == "POST" and c["path"] == path]


@pytest.fixture(autouse=True)
def clean_idempotency():
    """幂等缓存是模块级的，用例之间必须隔离，否则第二个用例会拿到上一个的结果。"""
    mcp_tools.reset_idempotency()
    yield
    mcp_tools.reset_idempotency()


def stub(**kwargs):
    kwargs.setdefault("gets", {"/api/servers": SERVERS, "/api/cabinets": CABINETS})
    return WriteStub(**kwargs)


# ---------------------------------------------------------------- sm_upsert

class TestUpsert:
    def test_add_admin_returns_applied(self):
        client = stub(posts={"/api/servers/add": ADMIN_SAVED})
        body = json.loads(mcp_tools.call_tool(
            "sm_upsert",
            {"entity": "servers", "row": {"序列号": "SN-1", "品牌": "Dell"}},
            client,
        ))
        assert body["applied"] is True
        assert body["pending"] is False
        assert body["change_id"] == "chg-1"
        assert "复核" in body["next"]

    def test_add_non_admin_returns_pending(self):
        """业务人员提交只产生申请：必须让 agent 明确知道还没生效。"""
        client = stub(posts={"/api/servers/add": BIZ_SUBMITTED})
        body = json.loads(mcp_tools.call_tool(
            "sm_upsert", {"entity": "servers", "row": {"序列号": "SN-1"}}, client
        ))
        assert body["applied"] is False
        assert body["pending"] is True
        assert "sm_review" in body["next"]

    def test_update_merges_existing_record(self):
        """修改时只给要改的字段，工具要把原记录其它字段补齐再提交。"""
        client = stub(posts={"/api/servers/update": ADMIN_SAVED})
        mcp_tools.call_tool(
            "sm_upsert",
            {"entity": "servers", "row": {"品牌": "浪潮"}, "old_key": "SN-1"},
            client,
        )
        payload = client.posts_to("/api/servers/update")[0]["payload"]
        assert payload["old"] == {"序列号": "SN-1"}
        assert payload["new"]["品牌"] == "浪潮"
        # 没传的字段必须保留原值，而不是被清空
        assert payload["new"]["所在机房"] == "机房A"
        assert payload["new"]["所属机位"] == "9-10"

    def test_update_accepts_object_key(self):
        client = stub(posts={"/api/servers/update": ADMIN_SAVED})
        mcp_tools.call_tool(
            "sm_upsert",
            {"entity": "servers", "row": {"品牌": "x"}, "old_key": {"序列号": "SN-2"}},
            client,
        )
        payload = client.posts_to("/api/servers/update")[0]["payload"]
        assert payload["old"] == {"序列号": "SN-2"}

    def test_composite_key_requires_object(self):
        client = stub(posts={"/api/cabinets/update": ADMIN_SAVED})
        with pytest.raises(SMError) as exc:
            mcp_tools.call_tool(
                "sm_upsert", {"entity": "cabinets", "row": {"机位": "3-4"}, "old_key": "D01"}, client
            )
        assert "复合主键" in str(exc.value)

    def test_composite_key_update_works(self):
        client = stub(posts={"/api/cabinets/update": ADMIN_SAVED})
        mcp_tools.call_tool(
            "sm_upsert",
            {"entity": "cabinets", "row": {"机位": "3-4"}, "old_key": {"机房": "机房A", "机柜": "D01"}},
            client,
        )
        payload = client.posts_to("/api/cabinets/update")[0]["payload"]
        assert payload["old"] == {"机房": "机房A", "机柜": "D01"}

    def test_unknown_field_is_rejected_before_any_write(self):
        client = stub(posts={"/api/servers/add": ADMIN_SAVED})
        with pytest.raises(SMError) as exc:
            mcp_tools.call_tool(
                "sm_upsert", {"entity": "servers", "row": {"不存在的列": "x"}}, client
            )
        assert "sm_schema" in str(exc.value)
        assert client.posts_to("/api/servers/add") == []

    def test_update_missing_record_is_rejected(self):
        client = stub(posts={"/api/servers/update": ADMIN_SAVED})
        with pytest.raises(SMError) as exc:
            mcp_tools.call_tool(
                "sm_upsert", {"entity": "servers", "row": {"品牌": "x"}, "old_key": "SN-404"}, client
            )
        assert "sm_query" in str(exc.value)
        assert client.posts_to("/api/servers/update") == []

    def test_unknown_entity(self):
        with pytest.raises(SMError) as exc:
            mcp_tools.call_tool("sm_upsert", {"entity": "nope", "row": {"a": 1}}, stub())
        assert "servers" in str(exc.value)

    def test_same_args_within_window_only_submits_once(self):
        """agent 原样重试不能产生两条待审批申请。"""
        client = stub(posts={"/api/servers/add": BIZ_SUBMITTED})
        args = {"entity": "servers", "row": {"序列号": "SN-1"}}
        first = mcp_tools.call_tool("sm_upsert", args, client)
        second = mcp_tools.call_tool("sm_upsert", args, client)
        assert first == second
        assert len(client.posts_to("/api/servers/add")) == 1


class TestUpsertTakeover:
    """takeover 是请求级参数，只能走 body 顶层，不能混进 row（row 只放真实表头）。"""

    def test_add_sends_takeover_flag(self):
        client = stub(posts={"/api/servers/add": ADMIN_SAVED})
        mcp_tools.call_tool(
            "sm_upsert",
            {"entity": "servers", "row": {"序列号": "SN-1"}, "takeover": True},
            client,
        )
        payload = client.posts_to("/api/servers/add")[0]["payload"]
        assert payload["takeover"] is True
        assert payload["序列号"] == "SN-1"

    def test_add_omits_flag_by_default(self):
        client = stub(posts={"/api/servers/add": ADMIN_SAVED})
        mcp_tools.call_tool("sm_upsert", {"entity": "servers", "row": {"序列号": "SN-1"}}, client)
        assert "takeover" not in client.posts_to("/api/servers/add")[0]["payload"]

    def test_update_sends_takeover_flag(self):
        client = stub(posts={"/api/servers/update": ADMIN_SAVED})
        mcp_tools.call_tool(
            "sm_upsert",
            {"entity": "servers", "row": {"所属机位": "3-4"}, "old_key": "SN-1", "takeover": True},
            client,
        )
        payload = client.posts_to("/api/servers/update")[0]["payload"]
        assert payload["takeover"] is True
        assert payload["new"]["所属机位"] == "3-4"
        # 合并逻辑不能被 takeover 影响
        assert payload["new"]["品牌"] == "Dell"

    def test_update_omits_flag_by_default(self):
        client = stub(posts={"/api/servers/update": ADMIN_SAVED})
        mcp_tools.call_tool(
            "sm_upsert", {"entity": "servers", "row": {"品牌": "x"}, "old_key": "SN-1"}, client
        )
        assert "takeover" not in client.posts_to("/api/servers/update")[0]["payload"]

    def test_takeover_ignored_for_other_entities(self):
        """cabinets / customers 没有机位占用概念，传了也不该透传。"""
        client = stub(posts={"/api/cabinets/add": ADMIN_SAVED})
        mcp_tools.call_tool(
            "sm_upsert",
            {"entity": "cabinets", "row": {"机房": "机房A", "机柜": "D02", "机位": "1-2"}, "takeover": True},
            client,
        )
        assert "takeover" not in client.posts_to("/api/cabinets/add")[0]["payload"]


# ---------------------------------------------------------------- sm_import_json

class TestImportJson:
    """批量导入：字段校验在 client 侧先挡一道，返回语义与 upsert 一致。"""

    IMPORT_OK = {
        "message": "已导入并直接生效：2 条新增",
        "change_ids": ["c1", "c2"],
        "imported": 2, "added": 2, "updated": 0,
    }

    def test_import_posts_rows_and_mode(self):
        client = stub(posts={"/api/servers/import-json": self.IMPORT_OK})
        body = json.loads(mcp_tools.call_tool(
            "sm_import_json",
            {
                "entity": "servers",
                "rows": [
                    {"序列号": "SN-9", "品牌": "Dell"},
                    {"序列号": "SN-10", "品牌": "HP"},
                ],
                "mode": "merge",
            },
            client,
        ))
        payload = client.posts_to("/api/servers/import-json")[0]["payload"]
        assert payload["rows"][0]["序列号"] == "SN-9"
        assert payload["mode"] == "merge"
        assert body["imported"] == 2 and body["added"] == 2
        assert body["applied"] is True and body["pending"] is False

    def test_import_pending_for_non_admin(self):
        result = dict(self.IMPORT_OK, message="已提交 2 条新增申请")
        client = stub(posts={"/api/servers/import-json": result})
        body = json.loads(mcp_tools.call_tool(
            "sm_import_json", {"entity": "servers", "rows": [{"序列号": "SN-9"}]}, client
        ))
        assert body["applied"] is False and body["pending"] is True
        assert "sm_review" in body["next"]

    def test_import_rejects_unknown_field(self):
        """未知列名要在 client 侧就报出来，不要让后端整批 400 却看不出是哪列。"""
        client = stub(posts={"/api/servers/import-json": self.IMPORT_OK})
        from mcp_server.sm_mcp.client import SMError
        with pytest.raises(SMError, match="未知字段"):
            mcp_tools.call_tool(
                "sm_import_json",
                {"entity": "servers", "rows": [{"序列号": "SN-9", "不存在的列": "x"}]},
                client,
            )
        assert not client.posts_to("/api/servers/import-json")

    def test_import_rejects_empty_rows(self):
        client = stub(posts={"/api/servers/import-json": self.IMPORT_OK})
        with pytest.raises(SMError, match="不能为空"):
            mcp_tools.call_tool("sm_import_json", {"entity": "servers", "rows": []}, client)

    def test_import_default_mode_full(self):
        client = stub(posts={"/api/servers/import-json": self.IMPORT_OK})
        mcp_tools.call_tool(
            "sm_import_json", {"entity": "servers", "rows": [{"序列号": "SN-9"}]}, client
        )
        assert client.posts_to("/api/servers/import-json")[0]["payload"]["mode"] == "full"

    def test_import_idempotent(self):
        client = stub(posts={"/api/servers/import-json": self.IMPORT_OK})
        args = {"entity": "servers", "rows": [{"序列号": "SN-9"}]}
        first = mcp_tools.call_tool("sm_import_json", args, client)
        second = mcp_tools.call_tool("sm_import_json", args, client)
        assert first == second
        assert len(client.posts_to("/api/servers/import-json")) == 1


# ---------------------------------------------------------------- sm_delete

class TestDelete:
    def test_confirm_mismatch_deletes_nothing(self):
        client = stub(posts={"/api/servers/delete": {"message": "已删除并直接生效"}})
        with pytest.raises(SMError) as exc:
            mcp_tools.call_tool(
                "sm_delete",
                {"entity": "servers", "keys": ["SN-1"], "confirm_keys": ["SN-2"]},
                client,
            )
        assert "不一致" in str(exc.value)
        assert client.posts_to("/api/servers/delete") == []

    def test_missing_confirm_is_rejected(self):
        client = stub(posts={"/api/servers/delete": {"message": "已删除并直接生效"}})
        with pytest.raises(SMError) as exc:
            mcp_tools.call_tool("sm_delete", {"entity": "servers", "keys": ["SN-1"]}, client)
        assert "confirm_keys" in str(exc.value)
        assert client.posts_to("/api/servers/delete") == []

    def test_duplicate_keys_rejected(self):
        client = stub()
        with pytest.raises(SMError) as exc:
            mcp_tools.call_tool(
                "sm_delete",
                {"entity": "servers", "keys": ["SN-1", "SN-1"], "confirm_keys": ["SN-1", "SN-1"]},
                client,
            )
        assert "重复" in str(exc.value)

    def test_missing_record_rejected(self):
        client = stub()
        with pytest.raises(SMError) as exc:
            mcp_tools.call_tool(
                "sm_delete",
                {"entity": "servers", "keys": ["SN-404"], "confirm_keys": ["SN-404"]},
                client,
            )
        assert "不存在" in str(exc.value)
        assert client.posts_to("/api/servers/delete") == []

    def test_single_delete_applied(self):
        client = stub(posts={"/api/servers/delete": {"message": "已删除并直接生效", "change_id": "chg-9"}})
        body = json.loads(mcp_tools.call_tool(
            "sm_delete",
            {"entity": "servers", "keys": ["SN-1"], "confirm_keys": ["SN-1"]},
            client,
        ))
        assert body["applied"] is True
        assert client.posts_to("/api/servers/delete")[0]["payload"] == {"序列号": "SN-1"}

    def test_batch_delete_uses_batch_endpoint(self):
        client = stub(posts={
            "/api/servers/delete-batch": {"message": "已提交 2 条删除申请", "change_ids": ["a", "b"]},
        })
        body = json.loads(mcp_tools.call_tool(
            "sm_delete",
            {"entity": "servers", "keys": ["SN-1", "SN-2"], "confirm_keys": ["SN-2", "SN-1"]},
            client,
        ))
        assert body["pending"] is True
        assert body["change_ids"] == ["a", "b"]
        payload = client.posts_to("/api/servers/delete-batch")[0]["payload"]
        assert payload == {"records": [{"序列号": "SN-1"}, {"序列号": "SN-2"}]}

    def test_key_order_does_not_matter(self):
        """confirm_keys 顺序可以不同，内容必须一致——顺序不同不该误拦。"""
        client = stub(posts={"/api/servers/delete-batch": {"message": "已删除并直接生效：2 条"}})
        body = json.loads(mcp_tools.call_tool(
            "sm_delete",
            {"entity": "servers", "keys": ["SN-1", "SN-2"], "confirm_keys": ["SN-2", "SN-1"]},
            client,
        ))
        assert body["applied"] is True

    def test_too_many_keys_rejected(self):
        client = stub()
        with pytest.raises(SMError) as exc:
            mcp_tools.call_tool(
                "sm_delete",
                {"entity": "servers", "keys": ["SN-%d" % i for i in range(21)],
                 "confirm_keys": ["SN-%d" % i for i in range(21)]},
                client,
            )
        assert "20" in str(exc.value)


# ---------------------------------------------------------------- sm_pending / sm_review

class TestPending:
    def test_lists_change_ids(self):
        client = stub(gets={"/api/changes": {"changes": [{
            "id": "chg-1", "entity": "servers", "entity_label": "服务器",
            "operation": "修改", "record_key": "SN-1",
            "submitter": "biz1", "submitted_at": "2026-10-31 09:00:00",
        }]}})
        text = mcp_tools.call_tool("sm_pending", {}, client)
        assert "chg-1" in text and "SN-1" in text and "修改" in text
        assert "sm_review" in text

    def test_empty(self):
        client = stub(gets={"/api/changes": {"changes": []}})
        assert "没有待审批申请" in mcp_tools.call_tool("sm_pending", {}, client)

    def test_entity_filter(self):
        client = stub(gets={"/api/changes": {"changes": [
            {"id": "chg-1", "entity": "servers", "record_key": "SN-1"},
            {"id": "chg-2", "entity": "cabinets", "record_key": "机房A / D01"},
        ]}})
        text = mcp_tools.call_tool("sm_pending", {"entity": "servers"}, client)
        assert "chg-1" in text and "chg-2" not in text

    def test_takeover_flag_is_visible(self):
        """审批人必须能看出这条申请会顶掉别人的机器。"""
        client = stub(gets={"/api/changes": {"changes": [
            {"id": "chg-1", "entity": "servers", "operation": "新增", "record_key": "SN-NEW", "takeover": 1},
            {"id": "chg-2", "entity": "servers", "operation": "修改", "record_key": "SN-2", "takeover": 0},
        ]}})
        text = mcp_tools.call_tool("sm_pending", {}, client)
        assert "占用机位" in text
        row1 = next(line for line in text.splitlines() if "chg-1" in line)
        row2 = next(line for line in text.splitlines() if "chg-2" in line)
        assert "是" in row1
        assert "是" not in row2
        assert "回收站" in text

    def test_403_degrades_to_pending_view(self):
        """业务人员没有 review_operations，/api/changes 403，应降级而不是报错。"""
        client = stub(
            gets={
                "/api/servers": {
                    "records": [{"序列号": "SN-1", "品牌": "Dell", "所在机房": "机房A", "所属机位": "9-10"}],
                    "meta": SERVERS["meta"],
                },
            },
            errors={"/api/changes": SMError("当前身份无此能力（/api/changes）：无权限访问操作审核")},
        )
        text = mcp_tools.call_tool("sm_pending", {}, client)
        assert "没有「操作审核」权限" in text
        assert "SN-1" in text


class TestReview:
    def test_reject_requires_reason(self):
        client = stub()
        with pytest.raises(SMError) as exc:
            mcp_tools.call_tool("sm_review", {"ids": ["chg-1"], "action": "reject"}, client)
        assert "reason" in str(exc.value)

    def test_single_approve_posts_to_single_endpoint(self):
        client = stub(posts={"/api/changes/chg-1/review": {"message": "已通过"}})
        body = json.loads(mcp_tools.call_tool(
            "sm_review", {"ids": ["chg-1"], "action": "approve"}, client
        ))
        assert body["reviewed"][0]["id"] == "chg-1"
        payload = client.posts_to("/api/changes/chg-1/review")[0]["payload"]
        assert payload == {"action": "approve", "reason": ""}

    def test_single_approve_with_new_data(self):
        client = stub(posts={"/api/changes/chg-1/review": {"message": "已通过"}})
        mcp_tools.call_tool(
            "sm_review",
            {"ids": ["chg-1"], "action": "approve", "new_data": {"品牌": "浪潮"}},
            client,
        )
        payload = client.posts_to("/api/changes/chg-1/review")[0]["payload"]
        assert payload["new_data"] == {"品牌": "浪潮"}

    def test_batch_uses_batch_endpoint_and_parses_results(self):
        client = stub(posts={"/api/changes/review-batch": {
            "message": "已通过 2 条申请", "reviewed": 2,
            "results": [{"id": "chg-1", "message": "ok"}, {"id": "chg-2", "message": "ok"}],
        }})
        body = json.loads(mcp_tools.call_tool(
            "sm_review", {"ids": ["chg-1", "chg-2"], "action": "approve"}, client
        ))
        assert body["count"] == 2
        assert [item["id"] for item in body["reviewed"]] == ["chg-1", "chg-2"]

    def test_invalid_action(self):
        with pytest.raises(SMError):
            mcp_tools.call_tool("sm_review", {"ids": ["chg-1"], "action": "nope"}, stub())


# ---------------------------------------------------------------- 上下架请求

class TestRackSubmit:
    def test_submit_returns_request_id(self):
        client = stub(posts={"/api/rack-requests": {"message": "上下架请求已提交", "request_id": "req-1"}})
        body = json.loads(mcp_tools.call_tool(
            "sm_rack_submit", {"row": {"序列号": "SN-1", "请求类型": "上架"}}, client
        ))
        assert body["submitted"] is True
        assert body["request_id"] == "req-1"

    def test_duplicate_retry_is_idempotent(self):
        client = stub(posts={"/api/rack-requests": {"message": "上下架请求已提交", "request_id": "req-1"}})
        args = {"row": {"序列号": "SN-1", "请求类型": "上架"}}
        mcp_tools.call_tool("sm_rack_submit", args, client)
        mcp_tools.call_tool("sm_rack_submit", args, client)
        assert len(client.posts_to("/api/rack-requests")) == 1

    def test_empty_row_rejected(self):
        with pytest.raises(SMError):
            mcp_tools.call_tool("sm_rack_submit", {"row": {}}, stub())


class TestRackReview:
    def rack_gets(self, stage="allocation", status="pending"):
        def handler(params, payload=None):
            want = (params or {}).get("status", "pending")
            rows = [{
                "id": "req-1", "status": status, "stage": stage, "request_type": "上架",
                "request_data": "{}", "submitter_id": "biz1",
            }] if want == status else []
            return {"requests": rows}
        return {"/api/rack-requests": handler}

    def test_stage_mismatch_aborts_without_side_effect(self):
        """最危险的一条：stage 传错绝不能落到后端去走错分支。"""
        client = stub(
            gets=self.rack_gets(stage="allocation"),
            posts={"/api/rack-requests/req-1/review": {"message": "不该被调用"}},
        )
        with pytest.raises(SMError) as exc:
            mcp_tools.call_tool(
                "sm_rack_review",
                {"request_id": "req-1", "stage": "implementation", "action": "approve"},
                client,
            )
        assert "真实阶段是 allocation" in str(exc.value)
        assert client.posts_to("/api/rack-requests/req-1/review") == []

    def test_stage_must_be_explicit(self):
        with pytest.raises(SMError) as exc:
            mcp_tools.call_tool(
                "sm_rack_review", {"request_id": "req-1", "action": "approve"}, stub()
            )
        assert "stage" in str(exc.value)

    def test_unknown_request_id(self):
        client = stub(gets={"/api/rack-requests": {"requests": []}})
        with pytest.raises(SMError) as exc:
            mcp_tools.call_tool(
                "sm_rack_review",
                {"request_id": "req-404", "stage": "allocation", "action": "approve"},
                client,
            )
        assert "未找到" in str(exc.value)

    def test_already_reviewed(self):
        client = stub(gets=self.rack_gets(stage="allocation", status="approved"))
        with pytest.raises(SMError) as exc:
            mcp_tools.call_tool(
                "sm_rack_review",
                {"request_id": "req-1", "stage": "allocation", "action": "approve"},
                client,
            )
        assert "不是待审核" in str(exc.value)

    def test_approve_allocation_passes_server_data(self):
        client = stub(
            gets=self.rack_gets(stage="allocation"),
            posts={"/api/rack-requests/req-1/review": {"message": "服务器机位IP分配已完成"}},
        )
        body = json.loads(mcp_tools.call_tool(
            "sm_rack_review",
            {
                "request_id": "req-1", "stage": "allocation", "action": "approve",
                "server_data": {"所属机柜": "D01", "所属机位": "9-10"},
            },
            client,
        ))
        payload = client.posts_to("/api/rack-requests/req-1/review")[0]["payload"]
        assert payload["server_data"] == {"所属机柜": "D01", "所属机位": "9-10"}
        assert body["message"] == "服务器机位IP分配已完成"

    def test_reject_requires_reason(self):
        client = stub(gets=self.rack_gets())
        with pytest.raises(SMError) as exc:
            mcp_tools.call_tool(
                "sm_rack_review",
                {"request_id": "req-1", "stage": "allocation", "action": "reject"},
                client,
            )
        assert "reason" in str(exc.value)

    def test_approve_with_takeover_passes_flag(self):
        """分配阶段不传 takeover 就分不到已被占用的机位，所以必须能透传。"""
        client = stub(
            gets=self.rack_gets(stage="allocation"),
            posts={"/api/rack-requests/req-1/review": {"message": "服务器机位IP分配已完成"}},
        )
        body = json.loads(mcp_tools.call_tool(
            "sm_rack_review",
            {
                "request_id": "req-1", "stage": "allocation", "action": "approve",
                "server_data": {"所属机柜": "D01", "所属机位": "9-10"},
                "takeover": True,
            },
            client,
        ))
        payload = client.posts_to("/api/rack-requests/req-1/review")[0]["payload"]
        assert payload["takeover"] is True
        assert payload["server_data"]["所属机位"] == "9-10"
        assert body["takeover"] is True

    def test_approve_omits_takeover_by_default(self):
        client = stub(
            gets=self.rack_gets(stage="allocation"),
            posts={"/api/rack-requests/req-1/review": {"message": "ok"}},
        )
        body = json.loads(mcp_tools.call_tool(
            "sm_rack_review",
            {"request_id": "req-1", "stage": "allocation", "action": "approve",
             "server_data": {"所属机位": "9-10"}},
            client,
        ))
        assert "takeover" not in client.posts_to("/api/rack-requests/req-1/review")[0]["payload"]
        assert body["takeover"] is False

    def test_reject_omits_takeover(self):
        client = stub(
            gets=self.rack_gets(stage="allocation"),
            posts={"/api/rack-requests/req-1/review": {"message": "申请已驳回"}},
        )
        mcp_tools.call_tool(
            "sm_rack_review",
            {"request_id": "req-1", "stage": "allocation", "action": "reject",
             "reason": "机位信息不全", "takeover": True},
            client,
        )
        payload = client.posts_to("/api/rack-requests/req-1/review")[0]["payload"]
        assert "takeover" not in payload
        assert payload["reason"] == "机位信息不全"


# ---------------------------------------------------------------- tools/list 裁剪

def run_stdio(lines, client):
    import io

    stdout = io.StringIO()
    mcp_server.serve(stdin=io.StringIO("\n".join(lines) + "\n"), stdout=stdout, client=client)
    return [json.loads(line) for line in stdout.getvalue().strip().split("\n") if line.strip()]


def list_tools(client):
    message = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    return [tool["name"] for tool in run_stdio([message], client)[0]["result"]["tools"]]


class TestToolVisibility:
    def test_business_user_cannot_see_review_or_pending(self):
        names = list_tools(stub(me={
            "用户id": "biz1", "身份": "业务人员", "capabilities": ["submit_rack_request"],
        }))
        assert "sm_review" not in names
        assert "sm_pending" not in names
        assert "sm_rack_review" not in names
        # 能提交的写工具必须还在
        assert "sm_upsert" in names and "sm_rack_submit" in names and "sm_delete" in names

    def test_admin_sees_everything(self):
        names = list_tools(stub(me={
            "用户id": "admin", "身份": "管理人员",
            "capabilities": ["manage_all", "review_operations", "review_rack_requests",
                             "submit_rack_request", "manage_settings", "view_statistics"],
        }))
        for name in mcp_tools.TOOL_NAMES:
            assert name in names

    def test_room_user_sees_rack_review_but_not_operation_review(self):
        names = list_tools(stub(me={
            "用户id": "room1", "身份": "机房人员",
            "capabilities": ["review_rack_requests", "view_statistics"],
        }))
        assert "sm_rack_review" in names
        assert "sm_review" not in names
        assert "sm_pending" not in names

    def test_unknown_identity_shows_all_tools(self):
        """拿不到 /api/me 时不裁剪：让 403 说话，而不是让工具凭空消失。"""
        client = stub(errors={"/api/me": SMError("后端不可达：http://127.0.0.1:9")})
        names = list_tools(client)
        for name in mcp_tools.TOOL_NAMES:
            assert name in names

    def test_identity_is_cached_across_calls(self):
        """身份只查一次：tools/list 是高频调用，同一进程内不能每次都打后端。"""
        client = stub(me={"用户id": "admin", "capabilities": ["manage_all"]})
        messages = [
            json.dumps({"jsonrpc": "2.0", "id": index, "method": "tools/list"})
            for index in (1, 2, 3)
        ]
        run_stdio(messages, client)
        assert len([c for c in client.calls if c["path"] == "/api/me"]) == 1


# ---------------------------------------------------------------- 真实后端端到端
#
# 单测里的 applied/pending 是用桩文案模拟的，这里再走一遍真 HTTP，
# 确认「后端确实按身份返回直接生效/待审批」这个前提成立——否则整个语义翻译就是空的。

@pytest.fixture
def live(app_env):
    import threading

    from werkzeug.serving import make_server

    server = make_server("127.0.0.1", 0, app_env["app"], threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_port
    finally:
        server.shutdown()
        thread.join(timeout=5)


def api_client(base_url, user_id):
    from app import storage
    from mcp_server.sm_mcp.client import SMClient

    token = storage.get_users()[user_id]["api_token"] or storage.generate_user_api_token(user_id)
    return SMClient(base_url=base_url, token=token)


class TestEndToEndWrite:
    def test_admin_add_query_delete_roundtrip(self, live):
        client = api_client(live, "admin")

        created = json.loads(mcp_tools.call_tool(
            "sm_upsert", {"entity": "customers", "row": {"客户id": "90000001", "名称": "端到端客户"}}, client
        ))
        assert created["applied"] is True, created
        assert created["pending"] is False

        # 服务器必须落在已登记的机柜里，所以先建机柜
        cabinet = json.loads(mcp_tools.call_tool(
            "sm_upsert", {"entity": "cabinets", "row": {"机房": "机房A", "机柜": "D01", "机位": "1-20"}}, client
        ))
        assert cabinet["applied"] is True, cabinet

        server = json.loads(mcp_tools.call_tool(
            "sm_upsert",
            {"entity": "servers", "row": {
                "序列号": "SN-E2E-1", "客户id": "90000001",
                "所在机房": "机房A", "所属机柜": "D01", "所属机位": "9-10",
            }},
            client,
        ))
        assert server["applied"] is True, server
        assert "SN-E2E-1" in mcp_tools.call_tool(
            "sm_query", {"entity": "servers", "filters": {"序列号": "SN-E2E-1"}}, client
        )

        # 修改：只给要改的字段，工具要能取回原值合并后通过后端校验
        updated = json.loads(mcp_tools.call_tool(
            "sm_upsert",
            {"entity": "servers", "row": {"备注": "改过了"}, "old_key": "SN-E2E-1"},
            client,
        ))
        assert updated["applied"] is True, updated
        assert "改过了" in mcp_tools.call_tool(
            "sm_query", {"entity": "servers", "filters": {"序列号": "SN-E2E-1"}}, client
        )

        deleted = json.loads(mcp_tools.call_tool(
            "sm_delete", {"entity": "servers", "keys": ["SN-E2E-1"], "confirm_keys": ["SN-E2E-1"]}, client
        ))
        assert deleted["applied"] is True, deleted
        assert "共 0 条匹配" in mcp_tools.call_tool(
            "sm_query", {"entity": "servers", "filters": {"序列号": "SN-E2E-1"}}, client
        )

    def test_takeover_displaces_existing_server(self, live):
        """takeover 端到端：不传要被拦下，传了原服务器进回收站、新记录接手机位。"""
        client = api_client(live, "admin")
        json.loads(mcp_tools.call_tool(
            "sm_upsert", {"entity": "customers", "row": {"客户id": "90000009", "名称": "占用客户"}}, client
        ))
        json.loads(mcp_tools.call_tool(
            "sm_upsert", {"entity": "cabinets", "row": {"机房": "机房T", "机柜": "T01", "机位": "1-20"}}, client
        ))
        json.loads(mcp_tools.call_tool(
            "sm_upsert",
            {"entity": "servers", "row": {
                "序列号": "SN-T-OLD", "客户id": "90000009",
                "所在机房": "机房T", "所属机柜": "T01", "所属机位": "3-4",
            }},
            client,
        ))
        new_row = {
            "序列号": "SN-T-NEW", "客户id": "90000009",
            "所在机房": "机房T", "所属机柜": "T01", "所属机位": "3-4",
        }

        with pytest.raises(SMError) as exc:
            mcp_tools.call_tool("sm_upsert", {"entity": "servers", "row": new_row}, client)
        assert "已被服务器 SN-T-OLD 占用" in str(exc.value)

        created = json.loads(mcp_tools.call_tool(
            "sm_upsert", {"entity": "servers", "row": new_row, "takeover": True}, client
        ))
        assert created["applied"] is True, created
        assert "SN-T-OLD" in created["message"] and "回收站" in created["message"]

        assert "共 0 条匹配" in mcp_tools.call_tool(
            "sm_query", {"entity": "servers", "filters": {"序列号": "SN-T-OLD"}}, client
        )
        assert "SN-T-NEW" in mcp_tools.call_tool(
            "sm_query", {"entity": "servers", "filters": {"序列号": "SN-T-NEW"}}, client
        )
        trash = client.json("/api/trash")
        assert "SN-T-OLD" in {item["serial_number"] for item in trash["items"]}

    def test_non_admin_write_goes_pending(self, live):
        """业务人员的写操作只产生申请：applied 必须是 False 且带 change_id。"""
        admin = api_client(live, "admin")
        json.loads(mcp_tools.call_tool(
            "sm_upsert", {"entity": "customers", "row": {"客户id": "90000002", "名称": "端到端客户2"}}, admin
        ))
        json.loads(mcp_tools.call_tool(
            "sm_upsert", {"entity": "cabinets", "row": {"机房": "机房A", "机柜": "D01", "机位": "1-20"}}, admin
        ))
        client = api_client(live, "biz1")
        body = json.loads(mcp_tools.call_tool(
            "sm_upsert",
            {"entity": "servers", "row": {
                "序列号": "SN-E2E-2", "客户id": "90000002",
                "所在机房": "机房A", "所属机柜": "D01", "所属机位": "11-12",
            }},
            client,
        ))
        assert body["applied"] is False, body
        assert body["pending"] is True
        assert body["change_id"], "非管理人员必须拿到 change_id 才能后续审批"
        assert "sm_review" in body["next"]

    def test_tools_list_reflects_real_identity(self, live):
        """真实身份 → tools/list 裁剪：业务人员看不到审核类工具。"""
        names = list_tools(api_client(live, "biz1"))
        assert "sm_review" not in names and "sm_pending" not in names
        admin_names = list_tools(api_client(live, "admin"))
        assert "sm_review" in admin_names and "sm_pending" in admin_names


class TestAutoBackupBeforeWrite:
    """一轮 agent 操作（一个 SMClient 实例）只在最开始留一份快照，全轮复用它当撤回点。"""

    def test_one_snapshot_per_round_reused_by_all_writes(self, live):
        from app import storage
        from openpyxl import load_workbook

        client = api_client(live, "admin")
        first = json.loads(mcp_tools.call_tool(
            "sm_upsert", {"entity": "customers", "row": {"客户id": "91000001", "名称": "备份验证"}}, client
        ))
        second = json.loads(mcp_tools.call_tool(
            "sm_upsert", {"entity": "cabinets", "row": {"机房": "机房B", "机柜": "B01", "机位": "1-10"}}, client
        ))
        # 一轮只留一份，且两次写返回的都是同一份（整轮「操作前」的快照）
        assert len(list(storage.auto_backup_dir().glob("B*"))) == 1
        assert first.get("auto_backup") == second.get("auto_backup")
        name = second["auto_backup"]
        assert name.startswith("B")
        backup_dir = storage.auto_backup_dir() / name
        assert backup_dir.is_dir()
        # 快照是整轮操作前的状态：新建的客户、机柜都还没有
        wb = load_workbook(backup_dir / "机柜信息表.xlsx", data_only=True)
        cabinets = [row[0] for row in wb[wb.sheetnames[0]].iter_rows(min_row=2, values_only=True)]
        wb.close()
        assert "机房B" not in cabinets
        wb = load_workbook(backup_dir / "客户表.xlsx", data_only=True)
        cells = [c for row in wb[wb.sheetnames[0]].iter_rows(values_only=True) for c in row]
        wb.close()
        assert "备份验证" not in cells
        assert "restore_hint" in second and "数据恢复" in second["restore_hint"]

    def test_new_client_round_creates_another_snapshot(self, live):
        """新一轮（新 SMClient 实例，对应 agent 新一次 query）会再留一份。"""
        from app import storage

        first_client = api_client(live, "admin")
        first = json.loads(mcp_tools.call_tool(
            "sm_upsert", {"entity": "customers", "row": {"客户id": "91000011", "名称": "轮一"}}, first_client
        ))
        second_client = api_client(live, "admin")
        second = json.loads(mcp_tools.call_tool(
            "sm_upsert", {"entity": "customers", "row": {"客户id": "91000012", "名称": "轮二"}}, second_client
        ))
        assert second["auto_backup"] != first["auto_backup"]
        assert len(list(storage.auto_backup_dir().glob("B*"))) == 2

    def test_read_tools_do_not_trigger_backup(self, live):
        from app import storage

        client = api_client(live, "admin")
        mcp_tools.call_tool("sm_stats", {}, client)
        mcp_tools.call_tool("sm_query", {"entity": "servers"}, client)
        assert not list(storage.auto_backup_dir().glob("B*"))

    def test_delete_reuses_round_snapshot(self, live):
        from app import storage

        client = api_client(live, "admin")
        created = json.loads(mcp_tools.call_tool(
            "sm_upsert", {"entity": "cabinets", "row": {"机房": "机房C", "机柜": "C01", "机位": "1-10"}}, client
        ))
        deleted = json.loads(mcp_tools.call_tool(
            "sm_delete", {"entity": "cabinets", "keys": [{"机房": "机房C", "机柜": "C01"}],
                          "confirm_keys": [{"机房": "机房C", "机柜": "C01"}]},
            client,
        ))
        assert deleted["applied"] is True, deleted
        # 整轮仍只有一份，delete 复用首份作为退路，不再额外堆一份中间态
        assert deleted.get("auto_backup") == created["auto_backup"]
        assert len(list(storage.auto_backup_dir().glob("B*"))) == 1

    def test_env_can_disable_auto_backup(self, live, monkeypatch):
        """SM_AUTO_BACKUP=0 时不带这个头（磁盘紧张时的逃生口）。"""
        from app import storage
        from mcp_server.sm_mcp.client import SMClient

        monkeypatch.setenv("SM_AUTO_BACKUP", "0")
        token = storage.get_users()["admin"]["api_token"] or storage.generate_user_api_token("admin")
        client = SMClient(base_url=live, token=token)
        assert client.auto_backup is False
        json.loads(mcp_tools.call_tool(
            "sm_upsert", {"entity": "customers", "row": {"客户id": "91000003", "名称": "关备份"}}, client
        ))
        json.loads(mcp_tools.call_tool(
            "sm_upsert", {"entity": "cabinets", "row": {"机房": "机房D", "机柜": "D01", "机位": "1-10"}}, client
        ))
        assert not list(storage.auto_backup_dir().glob("B*"))
