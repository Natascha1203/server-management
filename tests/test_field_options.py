"""多值标签字段（接入行情 / 接入柜台）测试。

覆盖：
1. 文本归一：中英文逗号 / 顿号 / 分号混用、去重、数组入参
2. 候选池权限：只有管理人员能新增 / 改名 / 删除，其他身份只能读
3. 删除护栏：仍有服务器记录或待审批在用 → 409，只能改名
4. 改名级联：正式记录 + 待审批 new_data 一起刷新
5. 池外值自动收纳（Excel 手改 / API 写入）
6. 表单提交形态（数组）也能落成 "A, B" 文本
"""
import json

import pytest

from app import services, storage
from app.core import join_tags, normalize_row, split_tags


def _login(client, username, password="pw"):
    resp = client.post("/login", data={"username": username, "password": password})
    assert resp.status_code == 302


def _add_official(entity, row):
    """绕过审批直接写正式表（铺底数据）。"""
    records = storage.read_records(entity)
    records.append(normalize_row(entity, row))
    storage.write_records(entity, records)


def _setup_base(access_market="CTP行情、Level-2", access_counter="CTP柜台"):
    _add_official("customers", {"客户id": "1", "名称": "客户一"})
    _add_official("cabinets", {"机房": "机房A", "机柜": "C1", "机位": "1-10"})
    _add_official("servers", {
        "序列号": "SRV-1", "客户id": "1", "所在机房": "机房A", "所属机柜": "C1", "所属机位": "1-1",
        "接入行情": access_market, "接入柜台": access_counter,
    })


def _server(serial="SRV-1"):
    return next((row for row in storage.read_records("servers") if row["序列号"] == serial), None)


def _options(client, entity="servers"):
    resp = client.get(f"/api/field-options?entity={entity}")
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def _values(payload, field):
    return [item["value"] for item in payload["options"][field]]


class TestTagText:
    def test_mixed_separators_are_normalized(self):
        assert split_tags("CTP行情，Level-2、Level-3;CTP柜台") == ["CTP行情", "Level-2", "Level-3", "CTP柜台"]
        assert join_tags("CTP行情，Level-2、Level-2") == "CTP行情, Level-2"

    def test_list_input_is_accepted(self):
        row = normalize_row("servers", {"序列号": "S1", "接入行情": ["CTP行情", " Level-2 ", ""]})
        assert row["接入行情"] == "CTP行情, Level-2"

    def test_other_fields_untouched(self):
        row = normalize_row("servers", {"序列号": "S1", "备注": "a,b"})
        assert row["备注"] == "a,b"

    def test_empty_value_is_empty_string(self):
        assert normalize_row("servers", {"接入行情": None})["接入行情"] == ""


class TestPoolPermissions:
    def test_read_requires_login(self, app_env):
        client = app_env["app"].test_client()
        assert client.get("/api/field-options?entity=servers").status_code == 401

    def test_business_can_read_but_not_write(self, app_env, biz_user):
        _setup_base()
        client = app_env["app"].test_client()
        _login(client, "biz1")
        payload = _options(client)
        assert "CTP行情" in _values(payload, "接入行情")
        # 非管理人员看不到使用次数（那是设置页的管理信息）
        assert "usage" not in payload["options"]["接入行情"][0]

        body = {"entity": "servers", "field": "接入行情", "value": "新行情"}
        assert client.post("/api/field-options", json=body).status_code == 403
        assert client.patch("/api/field-options", json={**body, "value": "CTP行情", "new_value": "X"}).status_code == 403
        assert client.delete("/api/field-options", json={"entity": "servers", "field": "接入行情", "value": "CTP行情"}).status_code == 403

    def test_admin_can_create_and_list(self, app_env, admin):
        client = app_env["app"].test_client()
        _login(client, "admin")
        resp = client.post("/api/field-options", json={"entity": "servers", "field": "接入行情", "value": "Level-2"})
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["value"] == "Level-2"
        assert "Level-2" in _values(_options(client), "接入行情")
        # 管理人员能看到使用次数
        assert "usage" in _options(client)["options"]["接入行情"][0]

    def test_duplicate_is_rejected(self, app_env, admin):
        client = app_env["app"].test_client()
        _login(client, "admin")
        client.post("/api/field-options", json={"entity": "servers", "field": "接入行情", "value": "Level-2"})
        resp = client.post("/api/field-options", json={"entity": "servers", "field": "接入行情", "value": "Level-2"})
        assert resp.status_code == 400
        assert "已存在" in resp.get_json()["error"]

    def test_separator_and_empty_are_rejected(self, app_env, admin):
        client = app_env["app"].test_client()
        _login(client, "admin")
        for bad in ("A,B", "A、B", "   "):
            resp = client.post("/api/field-options", json={"entity": "servers", "field": "接入行情", "value": bad})
            assert resp.status_code == 400, bad

    def test_unknown_field_is_rejected(self, app_env, admin):
        client = app_env["app"].test_client()
        _login(client, "admin")
        resp = client.post("/api/field-options", json={"entity": "servers", "field": "备注", "value": "X"})
        assert resp.status_code == 400
        assert "不是多值标签字段" in resp.get_json()["error"]

    def test_pools_are_independent(self, app_env, admin):
        client = app_env["app"].test_client()
        _login(client, "admin")
        client.post("/api/field-options", json={"entity": "servers", "field": "接入行情", "value": "仅有行情"})
        payload = _options(client)
        assert "仅有行情" in _values(payload, "接入行情")
        assert "仅有行情" not in _values(payload, "接入柜台")


class TestDeleteGuard:
    def test_unused_option_can_be_deleted(self, app_env, admin):
        client = app_env["app"].test_client()
        _login(client, "admin")
        client.post("/api/field-options", json={"entity": "servers", "field": "接入行情", "value": "临时标签"})
        resp = client.delete("/api/field-options", json={"entity": "servers", "field": "接入行情", "value": "临时标签"})
        assert resp.status_code == 200, resp.get_json()
        assert "临时标签" not in _values(_options(client), "接入行情")

    def test_in_use_option_can_only_be_renamed(self, app_env, admin):
        _setup_base()
        client = app_env["app"].test_client()
        _login(client, "admin")
        resp = client.delete("/api/field-options", json={"entity": "servers", "field": "接入行情", "value": "CTP行情"})
        assert resp.status_code == 409
        assert "只能改名" in resp.get_json()["error"]
        # 记录没被改动
        assert "CTP行情" in split_tags(_server()["接入行情"])

    def test_option_used_only_by_pending_change_is_protected(self, app_env, admin):
        _setup_base(access_market="")
        services.submit_change(
            "servers", "修改", "SRV-1",
            normalize_row("servers", {**_server(), "接入行情": "待审标签"}), submitter="biz1",
        )
        client = app_env["app"].test_client()
        _login(client, "admin")
        resp = client.delete("/api/field-options", json={"entity": "servers", "field": "接入行情", "value": "待审标签"})
        assert resp.status_code == 409


class TestRenameCascade:
    def test_rename_refreshes_records_and_pending(self, app_env, admin):
        _setup_base()
        pending_data = normalize_row("servers", {**_server(), "接入行情": "CTP行情, Level-3"})
        change_id = services.submit_change("servers", "修改", "SRV-1", pending_data, submitter="biz1")

        client = app_env["app"].test_client()
        _login(client, "admin")
        resp = client.patch("/api/field-options", json={
            "entity": "servers", "field": "接入行情", "value": "CTP行情", "new_value": "CTP行情V2",
        })
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body["records"] == 1
        assert body["pending"] == 1

        assert _server()["接入行情"] == "CTP行情V2, Level-2"
        with storage.db() as conn:
            row = conn.execute("SELECT new_data FROM changes WHERE id=?", (change_id,)).fetchone()
        assert json.loads(row["new_data"])["接入行情"] == "CTP行情V2, Level-3"
        assert "CTP行情V2" in _values(_options(client), "接入行情")

    def test_rename_to_existing_is_rejected(self, app_env, admin):
        _setup_base()
        client = app_env["app"].test_client()
        _login(client, "admin")
        resp = client.patch("/api/field-options", json={
            "entity": "servers", "field": "接入行情", "value": "CTP行情", "new_value": "Level-2",
        })
        assert resp.status_code == 400
        assert "已存在" in resp.get_json()["error"]


class TestAutoAbsorb:
    def test_values_written_outside_ui_are_absorbed(self, app_env, admin):
        _setup_base(access_market="Level-2, 外部导入值")
        client = app_env["app"].test_client()
        _login(client, "admin")
        payload = _options(client)
        assert "外部导入值" in _values(payload, "接入行情")
        source = {item["value"]: item["source"] for item in payload["options"]["接入行情"]}
        assert source["外部导入值"] == "data"

    def test_server_add_accepts_array(self, app_env, admin):
        _setup_base()
        client = app_env["app"].test_client()
        _login(client, "admin")
        resp = client.post("/api/servers/add", json={
            "序列号": "SRV-2", "客户id": "1", "所在机房": "机房A", "所属机柜": "C1", "所属机位": "2-2",
            "接入行情": ["CTP行情", "Level-2", "Level-2"], "接入柜台": ["CTP柜台"],
        })
        assert resp.status_code == 200, resp.get_json()
        server = _server("SRV-2")
        assert server["接入行情"] == "CTP行情, Level-2"
        assert server["接入柜台"] == "CTP柜台"


class TestValueValidation:
    def test_too_many_tags_rejected(self, app_env, admin):
        _setup_base()
        client = app_env["app"].test_client()
        _login(client, "admin")
        resp = client.post("/api/servers/add", json={
            "序列号": "SRV-3", "客户id": "1", "所在机房": "机房A", "所属机柜": "C1", "所属机位": "3-3",
            "接入行情": [f"标签{i}" for i in range(21)],
        })
        assert resp.status_code == 400
        assert "最多选" in resp.get_json()["error"]

    def test_too_long_tag_rejected(self, app_env, admin):
        _setup_base()
        client = app_env["app"].test_client()
        _login(client, "admin")
        resp = client.post("/api/servers/add", json={
            "序列号": "SRV-4", "客户id": "1", "所在机房": "机房A", "所属机柜": "C1", "所属机位": "4-4",
            "接入行情": ["长" * 41],
        })
        assert resp.status_code == 400
        assert "超过" in resp.get_json()["error"]


class TestSchemaExposure:
    def test_schema_marks_multi_value_fields(self, app_env):
        client = app_env["app"].test_client()
        schema = client.get("/api/skill/schema").get_json()
        assert schema["entities"]["servers"]["multi_value_fields"] == ["用途分类", "接入行情", "接入柜台"]
        assert schema["entities"]["cabinets"]["multi_value_fields"] == []


class TestSchemaMigration:
    def test_restart_recreates_missing_table(self, app_env):
        """老环境升级：库里没有 field_options 时，重启（init_storage）自动建表。"""
        with storage.db() as conn:
            conn.execute("DROP TABLE field_options")
        storage.init_storage()
        with storage.db() as conn:
            columns = [row["name"] for row in conn.execute("PRAGMA table_info(field_options)")]
            indexes = [row["name"] for row in conn.execute("PRAGMA index_list(field_options)")]
        assert {"entity", "field", "value", "source"} <= set(columns)
        assert "idx_field_options_unique" in indexes
