"""机位占用（takeover）与回收站权限的回归测试。

覆盖三条需求：
1. 上架请求可以直接占用已被占用的机位，原服务器整台下架进回收站
2. 修改服务器（变更请求 / 直接编辑）同样支持占用
3. 机房人员可以访问回收站，但只看本机房、只能重新上架；快照保存服务器全部字段
"""
import json

import pytest

from app import services, storage
from app.core import ENTITIES, normalize_row


def add_official(entity, row):
    """绕过审批直接写正式表（铺底数据）。"""
    records = storage.read_records(entity)
    records.append(normalize_row(entity, row))
    storage.write_records(entity, records)


def login(client, username="admin", password="pw"):
    resp = client.post("/login", data={"username": username, "password": password})
    assert resp.status_code == 302


def make_request(serial, request_type="上架", room="机房A"):
    data = services.normalize_rack_request({
        "序列号": serial, "请求类型": request_type, "客户id": "1", "所在机房": room,
    })
    with storage.db() as conn:
        return services.insert_rack_request(conn, data, {"用户id": "admin", "身份": "管理人员"})


def to_implementation(request_id, server_data):
    with storage.db() as conn:
        conn.execute(
            "UPDATE rack_requests SET stage='implementation',stage_data=? WHERE id=?",
            (json.dumps(server_data, ensure_ascii=False), request_id),
        )


def setup_base(app_env):
    """一套最小环境：机房A/C1（1-10）、客户1、一台占着 3-4 的服务器。"""
    add_official("customers", {"客户id": "1", "名称": "客户一"})
    add_official("cabinets", {
        "机房": "机房A", "机柜": "C1", "机位": "1-10",
        "管理ip网段": "10.1.1.*", "交易ip网段": "10.1.2.*", "带外ip网段": "10.1.3.*",
    })
    add_official("servers", {
        "序列号": "SRV-OLD", "客户id": "1", "所在机房": "机房A", "所属机柜": "C1",
        "所属机位": "3-4", "管理ip": "10.1.1.5/24", "带外ip": "10.1.3.5/24",
        "品牌": "Dell", "备注": "老机器",
    })
    return app_env["app"].test_client()


def new_server_data(slot="3-4", admin_ip="10.1.1.9/24", oob_ip="10.1.3.9/24"):
    return {
        "客户id": "1", "所在机房": "机房A", "所属机柜": "C1", "所属机位": slot,
        "管理ip": admin_ip, "交易ip": "", "带外ip": oob_ip,
    }


def trash_serials():
    return {row["serial_number"] for row in services.trash_rows()}


class TestRackTakeover:
    def test_blocks_when_takeover_not_requested(self, app_env, admin):
        client = setup_base(app_env)
        login(client)
        request_id = make_request("SRV-NEW")
        data = new_server_data()
        to_implementation(request_id, data)

        resp = client.post(f"/api/rack-requests/{request_id}/review",
                           json={"action": "approve", "server_data": data})
        assert resp.status_code == 400
        assert "已被服务器 SRV-OLD 占用" in resp.get_json()["error"]
        # 没确认占用就不该动老机器
        assert "SRV-OLD" in {row["序列号"] for row in storage.read_records("servers")}
        assert trash_serials() == set()

    def test_takeover_moves_original_to_trash(self, app_env, admin):
        client = setup_base(app_env)
        login(client)
        request_id = make_request("SRV-NEW")
        data = new_server_data()
        to_implementation(request_id, data)

        resp = client.post(f"/api/rack-requests/{request_id}/review",
                           json={"action": "approve", "server_data": data, "takeover": True})
        assert resp.status_code == 200
        message = resp.get_json()["message"]
        assert "SRV-OLD" in message and "回收站" in message

        serials = {row["序列号"] for row in storage.read_records("servers")}
        assert "SRV-NEW" in serials
        assert "SRV-OLD" not in serials

        rows = services.trash_rows()
        assert [row["serial_number"] for row in rows] == ["SRV-OLD"]
        snapshot = services.trash_to_json(rows[0])
        assert snapshot["reason"] == services.TRASH_REASON_TAKEOVER
        # 快照必须是整行：老机器的品牌/备注/IP 都在
        assert snapshot["品牌"] == "Dell"
        assert snapshot["备注"] == "老机器"
        assert snapshot["管理ip"] == "10.1.1.5/24"
        assert snapshot["request_id"] == request_id

    def test_takeover_keeps_whole_original_range(self, app_env, admin):
        """老机器占 3-4，新机器只要 3 —— 老机器整台都要下架（不能切成半台）。"""
        client = setup_base(app_env)
        login(client)
        request_id = make_request("SRV-NEW")
        data = new_server_data(slot="3-3")
        to_implementation(request_id, data)

        resp = client.post(f"/api/rack-requests/{request_id}/review",
                           json={"action": "approve", "server_data": data, "takeover": True})
        assert resp.status_code == 200
        assert "SRV-OLD" not in {row["序列号"] for row in storage.read_records("servers")}
        assert trash_serials() == {"SRV-OLD"}

    def test_allocation_stage_accepts_takeover(self, app_env, admin):
        """机位视图在分配阶段就能点已占用机位，因此分配审核也要放行。"""
        client = setup_base(app_env)
        login(client)
        request_id = make_request("SRV-NEW")
        data = new_server_data()

        blocked = client.post(f"/api/rack-requests/{request_id}/review",
                              json={"action": "approve", "server_data": data})
        assert blocked.status_code == 400
        assert "已被服务器 SRV-OLD 占用" in blocked.get_json()["error"]

        resp = client.post(f"/api/rack-requests/{request_id}/review",
                           json={"action": "approve", "server_data": data, "takeover": True})
        assert resp.status_code == 200
        with storage.db() as conn:
            row = conn.execute("SELECT stage,takeover FROM rack_requests WHERE id=?", (request_id,)).fetchone()
        assert row["stage"] == "implementation"
        assert row["takeover"] == 1


class TestChangeTakeover:
    def test_change_blocks_without_takeover(self, app_env, admin):
        client = setup_base(app_env)
        login(client)
        add_official("servers", {
            "序列号": "SRV-A", "客户id": "1", "所在机房": "机房A", "所属机柜": "C1",
            "所属机位": "1-2", "管理ip": "10.1.1.20/24", "带外ip": "10.1.3.20/24",
        })
        resp = client.post("/api/servers/update", json={
            "old": {"序列号": "SRV-A"},
            "new": {"序列号": "SRV-A", "客户id": "1", "所在机房": "机房A", "所属机柜": "C1",
                    "所属机位": "3-4", "管理ip": "10.1.1.20/24", "带外ip": "10.1.3.20/24"},
        })
        assert resp.status_code == 400
        assert "已被服务器 SRV-OLD 占用" in resp.get_json()["error"]

    def test_change_takeover_displaces_original(self, app_env, admin):
        client = setup_base(app_env)
        login(client)
        add_official("servers", {
            "序列号": "SRV-A", "客户id": "1", "所在机房": "机房A", "所属机柜": "C1",
            "所属机位": "1-2", "管理ip": "10.1.1.20/24", "带外ip": "10.1.3.20/24",
        })
        resp = client.post("/api/servers/update", json={
            "old": {"序列号": "SRV-A"},
            "new": {"序列号": "SRV-A", "客户id": "1", "所在机房": "机房A", "所属机柜": "C1",
                    "所属机位": "3-4", "管理ip": "10.1.1.20/24", "带外ip": "10.1.3.20/24"},
            "takeover": True,
        })
        assert resp.status_code == 200
        assert "SRV-OLD" in resp.get_json()["message"]

        records = {row["序列号"]: row for row in storage.read_records("servers")}
        assert records["SRV-A"]["所属机位"] == "3-4"
        assert "SRV-OLD" not in records
        assert trash_serials() == {"SRV-OLD"}

    def test_change_request_takeover_via_review(self, app_env, admin):
        """走上下架请求的「变更」：实施审核时带 takeover 也要生效。"""
        client = setup_base(app_env)
        login(client)
        add_official("servers", {
            "序列号": "SRV-A", "客户id": "1", "所在机房": "机房A", "所属机柜": "C1",
            "所属机位": "1-2", "管理ip": "10.1.1.20/24", "带外ip": "10.1.3.20/24",
        })
        request_id = make_request("SRV-A", request_type="变更")
        data = {
            "客户id": "1", "所在机房": "机房A", "所属机柜": "C1", "所属机位": "3-4",
            "管理ip": "10.1.1.20/24", "交易ip": "", "带外ip": "10.1.3.20/24",
        }
        to_implementation(request_id, data)

        resp = client.post(f"/api/rack-requests/{request_id}/review",
                           json={"action": "approve", "server_data": data, "takeover": True})
        assert resp.status_code == 200
        records = {row["序列号"]: row for row in storage.read_records("servers")}
        assert records["SRV-A"]["所属机位"] == "3-4"
        assert "SRV-OLD" not in records
        assert trash_serials() == {"SRV-OLD"}


class TestAddTakeover:
    """直接新增（不走上下架请求）也要能占用已有机位。"""

    def test_add_blocks_without_takeover(self, app_env, admin):
        client = setup_base(app_env)
        login(client)
        body = {"序列号": "SRV-NEW", **new_server_data()}

        resp = client.post("/api/servers/add", json=body)
        assert resp.status_code == 400
        assert "已被服务器 SRV-OLD 占用" in resp.get_json()["error"]
        assert "SRV-OLD" in {row["序列号"] for row in storage.read_records("servers")}
        assert trash_serials() == set()

    def test_add_with_takeover_displaces_original(self, app_env, admin):
        client = setup_base(app_env)
        login(client)
        body = {"序列号": "SRV-NEW", **new_server_data(), "takeover": True}

        resp = client.post("/api/servers/add", json=body)
        assert resp.status_code == 200
        message = resp.get_json()["message"]
        assert "直接生效" in message
        assert "SRV-OLD" in message and "回收站" in message

        records = {row["序列号"]: row for row in storage.read_records("servers")}
        assert "SRV-NEW" in records
        assert records["SRV-NEW"]["所属机位"] == "3-4"
        assert "SRV-OLD" not in records

        rows = services.trash_rows()
        assert [row["serial_number"] for row in rows] == ["SRV-OLD"]
        snapshot = services.trash_to_json(rows[0])
        assert snapshot["reason"] == services.TRASH_REASON_TAKEOVER
        # 快照还是整行；takeover 是请求级参数，不能混进服务器字段
        assert snapshot["品牌"] == "Dell"
        assert "takeover" not in snapshot

    def test_add_takeover_non_admin_defers_until_approved(self, app_env, admin, biz_user):
        """非管理人员提交时只落意图：审批通过那一刻才真正下架原服务器。"""
        admin_client = setup_base(app_env)
        login(admin_client)

        client = app_env["app"].test_client()
        login(client, "biz1")
        body = {"序列号": "SRV-NEW", **new_server_data(), "takeover": True}
        resp = client.post("/api/servers/add", json=body)
        assert resp.status_code == 200
        payload = resp.get_json()
        assert "已提交新增申请" in payload["message"]
        assert "审批通过后" in payload["message"] and "SRV-OLD" in payload["message"]
        # 还没审批：谁都不该动
        assert "SRV-OLD" in {row["序列号"] for row in storage.read_records("servers")}
        assert "SRV-NEW" not in {row["序列号"] for row in storage.read_records("servers")}
        assert trash_serials() == set()

        review = admin_client.post(
            f"/api/changes/{payload['change_id']}/review", json={"action": "approve"}
        )
        assert review.status_code == 200

        records = {row["序列号"] for row in storage.read_records("servers")}
        assert "SRV-NEW" in records
        assert "SRV-OLD" not in records
        assert trash_serials() == {"SRV-OLD"}

    def test_add_takeover_ignored_for_non_server_entities(self, app_env, admin):
        """takeover 只对 servers 有意义：其它实体传了也不该写进字段。"""
        client = setup_base(app_env)
        login(client)
        resp = client.post("/api/customers/add", json={"客户id": "2", "名称": "客户二", "takeover": True})
        assert resp.status_code == 200
        record = services.find_record("customers", "2")
        assert "takeover" not in record


class TestTrashSnapshotCompleteness:
    def test_snapshot_holds_all_server_headers(self, app_env, admin):
        client = setup_base(app_env)
        login(client)
        add_official("servers", {
            "序列号": "SRV-DOWN", "客户id": "1", "所在机房": "机房A", "所属机柜": "C1",
            "所属机位": "7-8", "管理ip": "10.1.1.30/24", "带外ip": "10.1.3.30/24",
            "品牌": "HPE", "CPU型号": "EPYC", "服务器成本(万)": "8",
        })
        resp = client.delete("/api/trash/xxx")
        assert resp.status_code in (403, 404)

        # 通过删除正式记录进回收站（admin 直接生效）
        client.post("/api/servers/delete", json={"序列号": "SRV-DOWN"})
        rows = services.trash_rows()
        assert [row["serial_number"] for row in rows] == ["SRV-DOWN"]
        snapshot = services.trash_to_json(rows[0])
        missing = [h for h in ENTITIES["servers"]["headers"] if h not in snapshot]
        assert missing == [], f"快照缺字段：{missing}"

    def test_api_returns_header_list(self, app_env, admin):
        client = setup_base(app_env)
        login(client)
        payload = client.get("/api/trash").get_json()
        assert payload["headers"] == ENTITIES["servers"]["headers"]
        assert payload["can_manage"] is True


class TestRoomUserTrashAccess:
    def _seed(self, client, admin_client):
        """机房A 一条、机房B 一条回收站记录。"""
        add_official("cabinets", {"机房": "机房B", "机柜": "C1", "机位": "1-10"})
        for serial, room, admin_ip, oob_ip in (
            ("SRV-A1", "机房A", "10.1.1.15/24", "10.1.3.15/24"),
            ("SRV-B1", "机房B", "10.2.1.5/24", "10.2.3.5/24"),
        ):
            add_official("servers", {
                "序列号": serial, "客户id": "1", "所在机房": room, "所属机柜": "C1",
                "所属机位": "1-2", "管理ip": admin_ip, "带外ip": oob_ip,
            })
        for serial in ("SRV-A1", "SRV-B1"):
            server = services.find_record("servers", serial)
            with storage.db() as conn:
                services.move_server_to_trash(conn, server, reason="测试", deleted_by="admin")
            storage.write_records(
                "servers", [r for r in storage.read_records("servers") if r["序列号"] != serial]
            )

    def test_room_sees_only_own_room(self, app_env, admin, room_user):
        admin_client = setup_base(app_env)
        login(admin_client)
        self._seed(admin_client, admin_client)

        client = app_env["app"].test_client()
        login(client, "room1")
        payload = client.get("/api/trash")
        assert payload.status_code == 200
        serials = {item["serial_number"] for item in payload.get_json()["items"]}
        assert serials == {"SRV-A1"}
        assert payload.get_json()["can_manage"] is False

    def test_room_can_restore_own_room(self, app_env, admin, room_user):
        admin_client = setup_base(app_env)
        login(admin_client)
        self._seed(admin_client, admin_client)

        client = app_env["app"].test_client()
        login(client, "room1")
        items = client.get("/api/trash").get_json()["items"]
        target = next(item for item in items if item["serial_number"] == "SRV-A1")
        resp = client.post(f"/api/trash/{target['id']}/restore", json={})
        assert resp.status_code == 200
        assert "SRV-A1" in {row["序列号"] for row in storage.read_records("servers")}

    def test_room_can_restore_with_field_edits(self, app_env, admin, room_user):
        """机房人员恢复时也能补全/修改整行字段。"""
        admin_client = setup_base(app_env)
        login(admin_client)
        self._seed(admin_client, admin_client)

        client = app_env["app"].test_client()
        login(client, "room1")
        items = client.get("/api/trash").get_json()["items"]
        target = next(item for item in items if item["serial_number"] == "SRV-A1")
        resp = client.post(f"/api/trash/{target['id']}/restore", json={"server_data": {
            "品牌": "HPE", "型号": "DL380", "备注": "机房补录",
        }})
        assert resp.status_code == 200
        server = next(row for row in storage.read_records("servers") if row["序列号"] == "SRV-A1")
        assert (server["品牌"], server["型号"], server["备注"]) == ("HPE", "DL380", "机房补录")

    def test_room_cannot_move_restored_server_to_other_room(self, app_env, admin, room_user):
        """机房人员改「所在机房」把自己的机器挪到别的机房必须被挡。"""
        admin_client = setup_base(app_env)
        login(admin_client)
        self._seed(admin_client, admin_client)

        client = app_env["app"].test_client()
        login(client, "room1")
        items = client.get("/api/trash").get_json()["items"]
        target = next(item for item in items if item["serial_number"] == "SRV-A1")
        resp = client.post(f"/api/trash/{target['id']}/restore", json={"server_data": {
            "所在机房": "机房B", "所属机柜": "C1", "所属机位": "3-4",
        }})
        assert resp.status_code == 400
        assert "本机房" in resp.get_json()["error"]
        assert "SRV-A1" not in {row["序列号"] for row in storage.read_records("servers")}

    def test_room_cannot_purge_or_add(self, app_env, admin, room_user):
        admin_client = setup_base(app_env)
        login(admin_client)
        self._seed(admin_client, admin_client)

        client = app_env["app"].test_client()
        login(client, "room1")
        items = client.get("/api/trash").get_json()["items"]
        target = items[0]
        assert client.delete(f"/api/trash/{target['id']}").status_code == 403
        assert client.post("/api/trash", json={"序列号": "SRV-X"}).status_code == 403

    def test_room_cannot_restore_other_room(self, app_env, admin, room_user):
        admin_client = setup_base(app_env)
        login(admin_client)
        self._seed(admin_client, admin_client)

        admin_items = admin_client.get("/api/trash").get_json()["items"]
        other = next(item for item in admin_items if item["serial_number"] == "SRV-B1")

        client = app_env["app"].test_client()
        login(client, "room1")
        # 机房人员手里没有这条记录的 id，但直接构造请求也必须被挡
        assert client.post(f"/api/trash/{other['id']}/restore", json={}).status_code == 403

    def test_business_user_has_no_trash(self, app_env, admin, biz_user):
        client = setup_base(app_env)
        login(client, "biz1")
        assert client.get("/api/trash").status_code == 403
