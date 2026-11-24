"""JSON 数组导入端点：与文件 /import 同校验、同审批流，但无需 multipart。

这条链路服务「agent 解析文件→转 JSON→提交」：client 端只发 JSON，
后端必须复用同一套校验/覆盖模式/审批语义，且不能被未提供的列坑到。

用 cabinets 实体（主键=机房+机柜，校验简单、无客户/机柜依赖）验证端点语义。
"""
import json

from app.core import ENTITIES
from app import storage
from app.core import normalize_row


def add_official(entity, row):
    """绕过审批直接写正式表（测试铺底数据用）。"""
    records = storage.read_records(entity)
    records.append(normalize_row(entity, row))
    storage.write_records(entity, records)


def login(app, username="admin", password="pw"):
    client = app.test_client()
    r = client.post("/login", data={"username": username, "password": password})
    assert r.status_code in (200, 302)
    return client


def cabinet_row(overrides=None):
    row = {h: "" for h in ENTITIES["cabinets"]["headers"]}
    row.update({"机房": "机房A", "机柜": "C-JSON"})
    if overrides:
        row.update(overrides)
    return row


class TestImportJsonValidation:
    def test_bad_mode_rejected(self, app_env):
        client = login(app_env["app"])
        r = client.post(
            "/api/cabinets/import-json",
            json={"rows": [cabinet_row()], "mode": "overwrite"},
        )
        assert r.status_code == 400
        assert "full" in r.get_json()["error"]

    def test_rows_must_be_array(self, app_env):
        client = login(app_env["app"])
        r = client.post("/api/cabinets/import-json", json={"rows": {"机房": "机房A"}})
        assert r.status_code == 400
        assert "数组" in r.get_json()["error"]

    def test_row_item_must_be_object(self, app_env):
        client = login(app_env["app"])
        r = client.post("/api/cabinets/import-json", json={"rows": ["not-an-object"]})
        assert r.status_code == 400
        assert "对象" in r.get_json()["error"]

    def test_row_count_limit(self, app_env):
        client = login(app_env["app"])
        r = client.post("/api/cabinets/import-json", json={"rows": [{}] * 2001})
        assert r.status_code == 400
        assert "2000" in r.get_json()["error"]

    def test_empty_rows_noop(self, app_env):
        client = login(app_env["app"])
        r = client.post("/api/cabinets/import-json", json={"rows": []})
        assert r.status_code == 200
        assert r.get_json()["imported"] == 0


class TestImportJsonApply:
    def test_admin_import_applies_directly(self, app_env):
        """管理人员：批量新增直接生效，返回统计字段给 agent 汇报。"""
        client = login(app_env["app"])
        r = client.post(
            "/api/cabinets/import-json",
            json={"rows": [cabinet_row(), cabinet_row({"机柜": "C-JSON-2"})]},
        )
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["imported"] == 2
        assert body["added"] == 2
        assert body["updated"] == 0
        assert len(body["change_ids"]) == 2
        assert "直接生效" in body["message"]
        records = client.get("/api/cabinets").get_json()["records"]
        assert any(x.get("机柜") == "C-JSON" for x in records)

    def test_non_admin_import_is_pending(self, app_env):
        """非管理人员（机房人员有 cabinets 权限）：只产生待审批申请，不直接落库。"""
        client = login(app_env["app"], "room1")
        r = client.post("/api/cabinets/import-json", json={"rows": [cabinet_row()]})
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["imported"] == 1
        assert len(body["change_ids"]) == 1
        assert "申请" in body["message"]
        # 未落库：changes 表里是 pending，official 视图里查不到
        with storage.db() as conn:
            change = conn.execute(
                "SELECT status FROM changes WHERE entity='cabinets' AND id=?",
                (body["change_ids"][0],),
            ).fetchone()
        assert change["status"] == "pending"
        official = client.get("/api/cabinets", query_string={"record_state": "official"}).get_json()["records"]
        assert not any(x.get("机柜") == "C-JSON" for x in official)

    def test_biz_cannot_mutate_cabinets(self, app_env):
        """无权限实体：后端按身份拒绝，与文件导入一致。"""
        client = login(app_env["app"], "biz1")
        r = client.post("/api/cabinets/import-json", json={"rows": [cabinet_row()]})
        assert r.status_code == 403

    def test_duplicate_keys_in_rows_rejected(self, app_env):
        client = login(app_env["app"])
        r = client.post(
            "/api/cabinets/import-json",
            json={"rows": [cabinet_row(), cabinet_row()]},
        )
        assert r.status_code == 400
        assert r.get_json()["error"] == "导入校验失败"
        assert any("重复" in d for e in r.get_json()["details"] for d in e["errors"])

    def test_field_key_whitespace_normalized(self, app_env):
        """键名带首尾空格要能对齐到真实表头（Excel 常见）。"""
        client = login(app_env["app"])
        row = cabinet_row()
        spaced = {(" " + k + " "): v for k, v in row.items()}
        r = client.post("/api/cabinets/import-json", json={"rows": [spaced]})
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["imported"] == 1

    def test_merge_mode_preserves_existing_field(self, app_env):
        """merge：未提供列保留旧值；full：置空。与文件导入语义一致。"""
        add_official("cabinets", {"机房": "机房A", "机柜": "C-JSON", "机位": "1-10", "瓦数(kw)": "5"})
        # 用 room1（走待审批）观察合并结果：只给机位，merge 应保留瓦数=5
        room_client = login(app_env["app"], "room1")
        r = room_client.post(
            "/api/cabinets/import-json",
            json={"rows": [cabinet_row({"机位": "1-20"})], "mode": "merge"},
        )
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["updated"] == 1
        with storage.db() as conn:
            change = conn.execute(
                "SELECT new_data FROM changes WHERE status='pending' AND entity='cabinets' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        new_data = json.loads(change["new_data"])
        assert new_data["机位"] == "1-20"
        assert new_data["瓦数(kw)"] == "5"  # merge 保留了旧值

    def test_requires_login(self, app_env):
        r = app_env["app"].test_client().post(
            "/api/cabinets/import-json", json={"rows": [cabinet_row()]}
        )
        assert r.status_code in (401, 403)
