"""请求类型（上架 / 下架 / 变更）与回收站的业务与 HTTP 测试。

覆盖点：
  1. 上架走 allocation → implementation；下架 / 变更直接进 implementation
  2. 下架实施完成：服务器表删行，整行快照进回收站
  3. 页面「删除 / 批量删除」同样进回收站（可恢复）
  4. 变更实施完成：以现行记录为底合并，改完直接写正式表（序列号不可改）
  5. 回收站接口只有管理人员可用
  6. 重新上架：可改客户/机位；序列号或机位冲突时报错且不改数据
  7. 永久删除连带清理资料附件
"""
from __future__ import annotations

import json

import pytest

from app import services, storage
from app.core import normalize_row


def add_official(entity, row):
    records = storage.read_records(entity)
    records.append(normalize_row(entity, row))
    storage.write_records(entity, records)


def login(client, username="admin", password="pw"):
    assert client.post("/login", data={"username": username, "password": password}).status_code == 302


def base_setup():
    add_official("customers", {"客户id": "1", "名称": "客户一"})
    add_official("cabinets", {
        "机房": "机房A", "机柜": "C1", "机位": "1-10",
        "管理ip网段": "10.1.1.*", "交易ip网段": "10.1.2.*", "带外ip网段": "10.1.3.*",
    })


def add_server(serial="SRV-1", slot="1-2", purpose="旧用途"):
    add_official("servers", {
        "序列号": serial, "客户id": "1", "所在机房": "机房A", "所属机柜": "C1", "所属机位": slot,
        "管理ip": f"10.1.1.{int(slot.split('-')[0]) + 3}", "交易ip": "10.1.2.5", "带外ip": "10.1.3.5",
        "用途": purpose,
    })


def submit_request(client, **overrides):
    body = {"请求类型": "上架", "是否重装系统": "是", "客户id": "1", "所在机房": "机房A"}
    body.update(overrides)
    return client.post("/api/rack-requests", json=body)


def request_row(request_id):
    with storage.db() as conn:
        row = conn.execute("SELECT * FROM rack_requests WHERE id=?", (request_id,)).fetchone()
    return dict(row) if row else None


def trash_items():
    with storage.db() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM trash ORDER BY deleted_at").fetchall()]


class TestInitialStage:
    def test_rack_up_starts_at_allocation(self, app_env, admin):
        base_setup()
        client = app_env["app"].test_client()
        login(client)
        resp = submit_request(client, 序列号="SRV-NEW")
        assert resp.status_code == 200
        row = request_row(resp.get_json()["request_id"])
        assert row["request_type"] == "上架"
        assert row["stage"] == "allocation"

    def test_rack_down_skips_allocation(self, app_env, admin):
        base_setup()
        add_server()
        client = app_env["app"].test_client()
        login(client)
        resp = submit_request(client, 序列号="SRV-1", 请求类型="下架")
        assert resp.status_code == 200
        row = request_row(resp.get_json()["request_id"])
        assert row["request_type"] == "下架"
        assert row["stage"] == "implementation"
        # 下架请求不填客户/机房时，从服务器记录补齐
        payload = json.loads(row["request_data"])
        assert payload["客户id"] == "1"
        assert payload["所在机房"] == "机房A"

    def test_change_skips_allocation(self, app_env, admin):
        base_setup()
        add_server()
        client = app_env["app"].test_client()
        login(client)
        resp = submit_request(client, 序列号="SRV-1", 请求类型="变更")
        assert resp.status_code == 200
        row = request_row(resp.get_json()["request_id"])
        assert row["request_type"] == "变更"
        assert row["stage"] == "implementation"

    def test_change_requires_existing_server(self, app_env, admin):
        base_setup()
        client = app_env["app"].test_client()
        login(client)
        resp = submit_request(client, 序列号="SRV-MISSING", 请求类型="变更")
        assert resp.status_code == 400
        assert "不存在" in resp.get_json()["error"]

    def test_rack_down_rejects_different_customer(self, app_env, admin):
        base_setup()
        add_official("customers", {"客户id": "2", "名称": "客户二"})
        add_server()
        client = app_env["app"].test_client()
        login(client)
        resp = submit_request(client, 序列号="SRV-1", 请求类型="下架", 客户id="2")
        assert resp.status_code == 400
        assert "客户id必须与服务器" in resp.get_json()["error"]

    def test_invalid_request_type(self, app_env, admin):
        base_setup()
        client = app_env["app"].test_client()
        login(client)
        resp = submit_request(client, 序列号="SRV-NEW", 请求类型="迁移")
        assert resp.status_code == 400
        assert "请求类型" in resp.get_json()["error"]


class TestCompleteRackDown:
    def test_server_moves_to_trash(self, app_env, admin):
        base_setup()
        add_server()
        client = app_env["app"].test_client()
        login(client)
        request_id = submit_request(client, 序列号="SRV-1", 请求类型="下架").get_json()["request_id"]

        resp = client.post(f"/api/rack-requests/{request_id}/review", json={"action": "approve"})
        assert resp.status_code == 200
        assert not [row for row in storage.read_records("servers") if row["序列号"] == "SRV-1"]

        items = trash_items()
        assert len(items) == 1
        snapshot = json.loads(items[0]["data"])
        assert snapshot["序列号"] == "SRV-1"
        assert snapshot["用途"] == "旧用途"
        assert items[0]["request_id"] == request_id
        assert items[0]["reason"] == "下架实施完成"

        row = request_row(request_id)
        assert row["status"] == "approved"
        assert row["stage"] == "completed"

    def test_missing_server_returns_conflict(self, app_env, admin):
        base_setup()
        add_server()
        client = app_env["app"].test_client()
        login(client)
        request_id = submit_request(client, 序列号="SRV-1", 请求类型="下架").get_json()["request_id"]
        # 提交后服务器被别处删掉：实施时就该报冲突，而不是静默成功
        storage.write_records("servers", [row for row in storage.read_records("servers") if row["序列号"] != "SRV-1"])

        resp = client.post(f"/api/rack-requests/{request_id}/review", json={"action": "approve"})
        assert resp.status_code == 409
        assert trash_items() == []


class TestDeleteMovesToTrash:
    """页面「删除 / 批量删除」不走上下架流程，但同样必须把整行快照留在回收站。"""

    def test_admin_delete_snapshots_server(self, app_env, admin):
        base_setup()
        add_server()
        client = app_env["app"].test_client()
        login(client)

        resp = client.post("/api/servers/delete", json={"序列号": "SRV-1"})
        assert resp.status_code == 200
        assert not [row for row in storage.read_records("servers") if row["序列号"] == "SRV-1"]

        items = trash_items()
        assert len(items) == 1
        assert items[0]["serial_number"] == "SRV-1"
        assert items[0]["reason"] == services.TRASH_REASON_DELETE
        assert items[0]["deleted_by"] == "admin"
        assert items[0]["request_id"] == resp.get_json()["change_id"]
        snapshot = json.loads(items[0]["data"])
        assert snapshot["用途"] == "旧用途"
        assert snapshot["所属机位"] == "1-2"
        assert snapshot["管理ip"] == "10.1.1.4"

    def test_deleted_server_can_be_restored(self, app_env, admin):
        base_setup()
        add_server()
        client = app_env["app"].test_client()
        login(client)
        client.post("/api/servers/delete", json={"序列号": "SRV-1"})
        trash_id = trash_items()[0]["id"]

        resp = client.post(f"/api/trash/{trash_id}/restore", json={})
        assert resp.status_code == 200
        server = next(row for row in storage.read_records("servers") if row["序列号"] == "SRV-1")
        assert server["用途"] == "旧用途"
        assert trash_items() == []

    def test_batch_delete_snapshots_each_server(self, app_env, admin):
        base_setup()
        add_server()
        add_official("servers", {
            "序列号": "SRV-2", "客户id": "1", "所在机房": "机房A", "所属机柜": "C1", "所属机位": "5-6",
            "管理ip": "10.1.1.8", "交易ip": "10.1.2.8", "带外ip": "10.1.3.8", "用途": "旧用途",
        })
        client = app_env["app"].test_client()
        login(client)

        resp = client.post("/api/servers/delete-batch", json={"records": [{"序列号": "SRV-1"}, {"序列号": "SRV-2"}]})
        assert resp.status_code == 200
        assert storage.read_records("servers") == []
        assert {row["serial_number"] for row in trash_items()} == {"SRV-1", "SRV-2"}

    def test_pending_delete_snapshots_after_approval(self, app_env, admin):
        base_setup()
        add_server()
        client = app_env["app"].test_client()
        login(client, "room1")

        resp = client.post("/api/servers/delete", json={"序列号": "SRV-1"})
        assert resp.status_code == 200
        assert resp.get_json()["message"] == "已提交删除申请"
        # 待审批阶段只标记，不动正式表也不落回收站
        assert trash_items() == []
        assert [row for row in storage.read_records("servers") if row["序列号"] == "SRV-1"]

        change_id = resp.get_json()["change_id"]
        admin_client = app_env["app"].test_client()
        login(admin_client)
        assert admin_client.post(f"/api/changes/{change_id}/review", json={"action": "approve"}).status_code == 200

        assert not [row for row in storage.read_records("servers") if row["序列号"] == "SRV-1"]
        items = trash_items()
        assert len(items) == 1
        assert items[0]["serial_number"] == "SRV-1"
        assert items[0]["deleted_by"] == "admin"  # 记录审批人，不是提交人
        assert items[0]["request_id"] == change_id

    def test_delete_only_snapshots_servers(self, app_env, admin):
        base_setup()
        client = app_env["app"].test_client()
        login(client)

        assert client.post("/api/customers/delete", json={"客户id": "1"}).status_code == 200
        assert storage.read_records("customers") == []
        assert trash_items() == []


class TestManualTrashEntry:
    """回收站手动登记：只有序列号必填，其余字段等重新上架时再补。"""

    def test_add_with_serial_only(self, app_env, admin):
        base_setup()
        client = app_env["app"].test_client()
        login(client)

        resp = client.post("/api/trash", json={"序列号": "SN-MANUAL-1"})
        assert resp.status_code == 200
        assert "已加入回收站" in resp.get_json()["message"]

        items = trash_items()
        assert len(items) == 1
        assert items[0]["serial_number"] == "SN-MANUAL-1"
        assert items[0]["origin"] == "manual"
        assert items[0]["reason"] == services.TRASH_REASON_MANUAL
        assert items[0]["deleted_by"] == "admin"
        snapshot = json.loads(items[0]["data"])
        assert snapshot["序列号"] == "SN-MANUAL-1"
        assert snapshot["客户id"] == ""

        listed = client.get("/api/trash").get_json()["items"]
        assert listed[0]["origin"] == "manual"
        assert listed[0]["serial_number"] == "SN-MANUAL-1"

    def test_serial_is_required(self, app_env, admin):
        base_setup()
        client = app_env["app"].test_client()
        login(client)

        assert client.post("/api/trash", json={"序列号": "  "}).status_code == 400
        assert trash_items() == []

    def test_reject_serial_already_in_servers(self, app_env, admin):
        base_setup()
        add_server()
        client = app_env["app"].test_client()
        login(client)

        resp = client.post("/api/trash", json={"序列号": "SRV-1"})
        assert resp.status_code == 400
        assert "已在服务器表中" in resp.get_json()["error"]
        assert trash_items() == []

    def test_reject_duplicate_in_trash(self, app_env, admin):
        base_setup()
        client = app_env["app"].test_client()
        login(client)

        assert client.post("/api/trash", json={"序列号": "SN-MANUAL-1"}).status_code == 200
        resp = client.post("/api/trash", json={"序列号": "SN-MANUAL-1"})
        assert resp.status_code == 400
        assert "已在回收站" in resp.get_json()["error"]
        assert len(trash_items()) == 1

    def test_only_admin_can_add(self, app_env, admin):
        base_setup()
        client = app_env["app"].test_client()
        login(client, "biz1")

        assert client.post("/api/trash", json={"序列号": "SN-MANUAL-1"}).status_code == 403
        assert trash_items() == []

    def test_restore_fills_missing_fields(self, app_env, admin):
        base_setup()
        client = app_env["app"].test_client()
        login(client)
        assert client.post("/api/trash", json={"序列号": "SN-MANUAL-1"}).status_code == 200
        trash_id = trash_items()[0]["id"]

        resp = client.post(f"/api/trash/{trash_id}/restore", json={"server_data": {
            "客户id": "1", "所在机房": "机房A", "所属机柜": "C1", "所属机位": "3-4",
            "管理ip": "10.1.1.20", "交易ip": "10.1.2.20", "带外ip": "10.1.3.20", "用途": "补登记",
        }})
        assert resp.status_code == 200
        server = next(row for row in storage.read_records("servers") if row["序列号"] == "SN-MANUAL-1")
        assert server["客户id"] == "1"
        assert server["客户名称"] == "客户一"  # 从客户表自动补齐
        assert server["所属机位"] == "3-4"
        assert server["管理ip"] == "10.1.1.20"
        assert server["用途"] == "补登记"
        assert trash_items() == []

    def test_restore_without_required_fields_keeps_record(self, app_env, admin):
        base_setup()
        client = app_env["app"].test_client()
        login(client)
        assert client.post("/api/trash", json={"序列号": "SN-MANUAL-1"}).status_code == 200
        trash_id = trash_items()[0]["id"]

        resp = client.post(f"/api/trash/{trash_id}/restore", json={"server_data": {}})
        assert resp.status_code == 400
        assert "手动登记记录信息不完整" in resp.get_json()["error"]
        assert len(trash_items()) == 1
        assert not [row for row in storage.read_records("servers") if row["序列号"] == "SN-MANUAL-1"]

    def test_manual_entry_restores_without_origin_snapshot(self, app_env, admin):
        """手动登记记录只有序列号，重新上架时一次补齐整行字段。"""
        base_setup()
        client = app_env["app"].test_client()
        login(client)
        client.post("/api/trash", json={"序列号": "SN-MANUAL-1"})
        trash_id = trash_items()[0]["id"]

        resp = client.post(f"/api/trash/{trash_id}/restore", json={"server_data": {
            "客户id": "1", "所在机房": "机房A", "所属机柜": "C1", "所属机位": "3-4",
            "品牌": "Dell", "型号": "R750", "CPU型号": "6252*2", "是否液冷": "否",
        }})
        assert resp.status_code == 200
        server = next(row for row in storage.read_records("servers") if row["序列号"] == "SN-MANUAL-1")
        assert (server["品牌"], server["型号"], server["CPU型号"]) == ("Dell", "R750", "6252*2")


class TestTrashRestoreEdits:
    """重新上架时整行服务器字段都能改（不只是客户与机位），序列号也能改名。"""

    def _to_trash(self, client, serial="SRV-1"):
        add_server(serial=serial)
        assert client.post("/api/servers/delete", json={"序列号": serial}).status_code == 200
        return next(item for item in trash_items() if item["serial_number"] == serial)

    def test_snapshot_restore_edits_any_field(self, app_env, admin):
        base_setup()
        client = app_env["app"].test_client()
        login(client)
        item = self._to_trash(client)

        resp = client.post(f"/api/trash/{item['id']}/restore", json={"server_data": {
            "品牌": "HPE", "型号": "DL380", "CPU型号": "6338*2", "服务器成本(万)": "12.5",
            "归属单位": "上海分公司自有", "是否液冷": "否", "用途": "改后的用途",
            "用途分类": "行情交易、结算", "接入行情": "CTP, UDP", "接入柜台": ["CTP"],
            "管理密码": "new-pass", "带外密码": "oob-pass", "防火墙白名单": "10.0.0.0/8",
            "资源申请编号": "REQ-2026-1", "备注": "恢复时补全",
        }})
        assert resp.status_code == 200
        server = next(row for row in storage.read_records("servers") if row["序列号"] == "SRV-1")
        assert (server["品牌"], server["型号"], server["CPU型号"]) == ("HPE", "DL380", "6338*2")
        assert server["服务器成本(万)"] == "12.5"
        assert server["归属单位"] == "上海分公司自有"
        assert server["是否液冷"] == "否"
        assert server["用途"] == "改后的用途"
        assert server["用途分类"] == "行情交易, 结算"
        assert server["接入行情"] == "CTP, UDP"
        assert server["接入柜台"] == "CTP"
        assert server["管理密码"] == "new-pass"
        assert server["带外密码"] == "oob-pass"
        assert server["防火墙白名单"] == "10.0.0.0/8"
        assert server["资源申请编号"] == "REQ-2026-1"
        assert server["备注"] == "恢复时补全"
        assert trash_items() == []

    def test_restore_can_clear_field(self, app_env, admin):
        base_setup()
        client = app_env["app"].test_client()
        login(client)
        item = self._to_trash(client)

        resp = client.post(f"/api/trash/{item['id']}/restore", json={"server_data": {"用途": ""}})
        assert resp.status_code == 200
        server = next(row for row in storage.read_records("servers") if row["序列号"] == "SRV-1")
        assert server["用途"] == ""

    def test_rename_serial_moves_attachments(self, app_env, admin):
        base_setup()
        client = app_env["app"].test_client()
        login(client)
        item = self._to_trash(client)
        with storage.db() as conn:
            conn.execute(
                "INSERT INTO attachments (id,serial_number,filename,stored_name,size,uploader,uploaded_at)"
                " VALUES (?,?,?,?,?,?,?)",
                ("att-1", "SRV-1", "说明.pdf", "stored.pdf", 10, "admin", "2026-11-13 10:00:00"),
            )

        resp = client.post(f"/api/trash/{item['id']}/restore", json={"server_data": {"序列号": "SRV-1-NEW"}})
        assert resp.status_code == 200
        assert resp.get_json()["record"]["序列号"] == "SRV-1-NEW"
        assert [row["序列号"] for row in storage.read_records("servers")] == ["SRV-1-NEW"]
        with storage.db() as conn:
            row = conn.execute("SELECT serial_number FROM attachments WHERE id='att-1'").fetchone()
        assert row["serial_number"] == "SRV-1-NEW"
        assert trash_items() == []

    def test_rename_to_existing_serial_rejected(self, app_env, admin):
        base_setup()
        client = app_env["app"].test_client()
        login(client)
        item = self._to_trash(client)
        add_server(serial="SRV-2", slot="3-4")

        resp = client.post(f"/api/trash/{item['id']}/restore", json={"server_data": {"序列号": "SRV-2"}})
        assert resp.status_code == 400
        assert "已在服务器表" in resp.get_json()["error"]
        assert len(trash_items()) == 1
        assert not [row for row in storage.read_records("servers") if row["序列号"] == "SRV-1"]

    def test_blank_serial_keeps_original(self, app_env, admin):
        base_setup()
        client = app_env["app"].test_client()
        login(client)
        item = self._to_trash(client)

        resp = client.post(f"/api/trash/{item['id']}/restore", json={"server_data": {"序列号": "  "}})
        assert resp.status_code == 200
        assert [row["序列号"] for row in storage.read_records("servers")] == ["SRV-1"]


class TestCompleteChange:
    def test_change_updates_official_record(self, app_env, admin):
        base_setup()
        add_server()
        client = app_env["app"].test_client()
        login(client)
        request_id = submit_request(client, 序列号="SRV-1", 请求类型="变更", 备注="调整用途").get_json()["request_id"]

        resp = client.post(f"/api/rack-requests/{request_id}/review", json={
            "action": "approve",
            "server_data": {"用途": "新用途", "用途分类": "行情交易、结算", "接入行情": ["CTP", "UDP"]},
        })
        assert resp.status_code == 200
        server = next(row for row in storage.read_records("servers") if row["序列号"] == "SRV-1")
        assert server["用途"] == "新用途"
        assert server["用途分类"] == "行情交易, 结算"
        assert server["接入行情"] == "CTP, UDP"
        # 未提交的字段保持原值（以现行记录为底合并）
        assert server["交易ip"] == "10.1.2.5"
        assert server["备注"] == "调整用途"

    def test_change_cannot_rename_serial(self, app_env, admin):
        base_setup()
        add_server()
        client = app_env["app"].test_client()
        login(client)
        request_id = submit_request(client, 序列号="SRV-1", 请求类型="变更").get_json()["request_id"]

        resp = client.post(f"/api/rack-requests/{request_id}/review", json={
            "action": "approve",
            "server_data": {"序列号": "SRV-HACK"},
        })
        assert resp.status_code == 200
        serials = {row["序列号"] for row in storage.read_records("servers")}
        assert serials == {"SRV-1"}

    def test_change_keeps_other_slots_free(self, app_env, admin):
        base_setup()
        add_server(slot="1-2")
        add_server(serial="SRV-2", slot="5-6")
        client = app_env["app"].test_client()
        login(client)
        request_id = submit_request(client, 序列号="SRV-1", 请求类型="变更").get_json()["request_id"]

        # 想把 SRV-1 挪到 SRV-2 的机位：应被机位占用校验拦下
        resp = client.post(f"/api/rack-requests/{request_id}/review", json={
            "action": "approve",
            "server_data": {"所属机位": "5-6"},
        })
        assert resp.status_code == 400
        assert "已被服务器" in resp.get_json()["error"]

    def test_review_payload_carries_base_data(self, app_env, admin):
        base_setup()
        add_server()
        client = app_env["app"].test_client()
        login(client)
        request_id = submit_request(client, 序列号="SRV-1", 请求类型="变更").get_json()["request_id"]

        items = client.get("/api/rack-requests?status=pending&stage=implementation").get_json()["requests"]
        item = next(row for row in items if row["id"] == request_id)
        assert item["base_data"]["用途"] == "旧用途"
        assert item["base_data"]["所属机位"] == "1-2"


class TestTrashApi:
    def _to_trash(self, client, serial="SRV-1"):
        request_id = submit_request(client, 序列号=serial, 请求类型="下架").get_json()["request_id"]
        assert client.post(f"/api/rack-requests/{request_id}/review", json={"action": "approve"}).status_code == 200
        return trash_items()[0]["id"]

    def test_business_reads_but_cannot_mutate(self, app_env, admin):
        base_setup()
        client = app_env["app"].test_client()
        login(client, "biz1")
        resp = client.get("/api/trash")
        assert resp.status_code == 200
        assert resp.get_json()["can_edit"] is False
        assert client.delete("/api/trash/whatever").status_code == 403
        assert client.post("/api/trash/whatever/restore", json={}).status_code == 403

    def test_restore_writes_back(self, app_env, admin):
        base_setup()
        add_server()
        client = app_env["app"].test_client()
        login(client)
        trash_id = self._to_trash(client)

        resp = client.post(f"/api/trash/{trash_id}/restore", json={})
        assert resp.status_code == 200
        server = next(row for row in storage.read_records("servers") if row["序列号"] == "SRV-1")
        assert server["用途"] == "旧用途"
        assert trash_items() == []

    def test_restore_allows_new_slot_and_customer(self, app_env, admin):
        base_setup()
        add_official("customers", {"客户id": "2", "名称": "客户二"})
        add_server()
        client = app_env["app"].test_client()
        login(client)
        trash_id = self._to_trash(client)

        resp = client.post(f"/api/trash/{trash_id}/restore", json={
            "server_data": {"客户id": "2", "所属机位": "8-9"},
        })
        assert resp.status_code == 200
        server = next(row for row in storage.read_records("servers") if row["序列号"] == "SRV-1")
        assert server["客户id"] == "2"
        assert server["客户名称"] == "客户二"
        assert server["所属机位"] == "8-9"

    def test_restore_rejects_slot_conflict(self, app_env, admin):
        base_setup()
        add_server()
        client = app_env["app"].test_client()
        login(client)
        trash_id = self._to_trash(client)
        add_server(serial="SRV-2", slot="1-2")

        resp = client.post(f"/api/trash/{trash_id}/restore", json={})
        assert resp.status_code == 400
        assert "已被服务器" in resp.get_json()["error"]
        assert len(trash_items()) == 1  # 失败不丢回收站记录

    def test_restore_rejects_unknown_customer(self, app_env, admin):
        base_setup()
        add_server()
        client = app_env["app"].test_client()
        login(client)
        trash_id = self._to_trash(client)

        resp = client.post(f"/api/trash/{trash_id}/restore", json={"server_data": {"客户id": "999"}})
        assert resp.status_code == 400
        assert "客户id 999 不存在" in resp.get_json()["error"]

    def test_restore_rejects_existing_serial(self, app_env, admin):
        base_setup()
        add_server()
        client = app_env["app"].test_client()
        login(client)
        trash_id = self._to_trash(client)
        add_server(serial="SRV-1", slot="7-8")

        resp = client.post(f"/api/trash/{trash_id}/restore", json={})
        assert resp.status_code == 400
        assert "已在服务器表中" in resp.get_json()["error"]

    def test_purge_removes_attachments(self, app_env, admin):
        base_setup()
        add_server()
        client = app_env["app"].test_client()
        login(client)
        trash_id = self._to_trash(client)

        archive = storage.STATE["archive_dir"]
        folder = archive / "SRV-1"
        folder.mkdir(parents=True, exist_ok=True)
        stored = folder / "a1_说明.txt"
        stored.write_text("hello", encoding="utf-8")
        with storage.db() as conn:
            conn.execute(
                "INSERT INTO attachments (id,serial_number,filename,stored_name,size,uploader,uploaded_at)"
                " VALUES ('a1','SRV-1','说明.txt','SRV-1/a1_说明.txt',5,'admin','2026-10-01 10:00:00')"
            )

        resp = client.delete(f"/api/trash/{trash_id}")
        assert resp.status_code == 200
        assert trash_items() == []
        assert not stored.exists()
        with storage.db() as conn:
            assert conn.execute("SELECT COUNT(*) FROM attachments WHERE serial_number='SRV-1'").fetchone()[0] == 0

    def test_purge_missing_returns_404(self, app_env, admin):
        base_setup()
        client = app_env["app"].test_client()
        login(client)
        assert client.delete("/api/trash/nope").status_code == 404


class TestServiceHelpers:
    def test_rack_server_draft_keeps_base_when_request_blank(self, app_env):
        base_setup()
        add_server()
        row = {
            "request_data": json.dumps({"序列号": "SRV-1", "请求类型": "变更", "品牌": "", "型号": "新型号"}, ensure_ascii=False),
            "stage_data": "{}",
            "submitted_at": "2026-11-01 10:00:00",
        }
        base = next(item for item in storage.read_records("servers") if item["序列号"] == "SRV-1")
        draft = services.rack_server_draft(row, {}, base=base)
        assert draft["品牌"] == base["品牌"] or draft["品牌"] == ""
        assert draft["型号"] == "新型号"
        assert draft["用途"] == "旧用途"
        assert draft["所属机位"] == "1-2"

    def test_trash_to_json_flattens_snapshot(self, app_env):
        base_setup()
        add_server()
        with storage.db() as conn:
            services.move_server_to_trash(
                conn, next(item for item in storage.read_records("servers") if item["序列号"] == "SRV-1"),
                request_id="r1", deleted_by="admin", reason="测试",
            )
        item = services.trash_to_json(services.trash_rows()[0])
        assert item["序列号"] == "SRV-1"
        assert item["reason"] == "测试"
        assert item["deleted_by"] == "admin"
        assert item["id"]
