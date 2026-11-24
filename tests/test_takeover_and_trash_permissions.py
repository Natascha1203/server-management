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


class TestSameSerialReplacement:
    """要上架的序列号 == 被占用机位上那台服务器：原地覆盖那一行，不进回收站。"""

    def submit_up(self, client, serial="SRV-OLD"):
        return client.post("/api/rack-requests", json={
            "请求类型": "上架", "序列号": serial, "客户id": "1",
            "所在机房": "机房A", "是否重装系统": "是",
        })

    def review(self, client, request_id, data, takeover=False):
        body = {"action": "approve", "server_data": data}
        if takeover:
            body["takeover"] = True
        return client.post(f"/api/rack-requests/{request_id}/review", json=body)

    def test_submit_accepts_existing_serial(self, app_env, admin):
        """提交阶段不再因为序列号已存在而拒绝——机位到分配阶段才定，重复留给分配判定。"""
        client = setup_base(app_env)
        login(client)
        resp = self.submit_up(client)
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["request_id"]

    def test_allocation_blocks_without_takeover(self, app_env, admin):
        client = setup_base(app_env)
        login(client)
        request_id = self.submit_up(client).get_json()["request_id"]

        resp = self.review(client, request_id, new_server_data(slot="3-4"))
        assert resp.status_code == 400
        error = resp.get_json()["error"]
        # 提示要能直接指导操作：位置在哪、怎么替换、否则用什么入口
        assert "已在 机房A / C1 / 3-4 使用" in error
        assert "占用模式" in error
        assert "变更" in error

    def test_allocation_accepts_takeover(self, app_env, admin):
        client = setup_base(app_env)
        login(client)
        request_id = self.submit_up(client).get_json()["request_id"]

        resp = self.review(client, request_id, new_server_data(slot="3-4"), takeover=True)
        assert resp.status_code == 200, resp.get_json()
        with storage.db() as conn:
            row = conn.execute("SELECT stage,takeover FROM rack_requests WHERE id=?", (request_id,)).fetchone()
        assert row["stage"] == "implementation"
        assert row["takeover"] == 1

    def test_implementation_overwrites_record_in_place(self, app_env, admin):
        client = setup_base(app_env)
        login(client)
        request_id = self.submit_up(client).get_json()["request_id"]
        data = {**new_server_data(slot="3-4"), "品牌": "H3C", "备注": "换机后"}
        assert self.review(client, request_id, data, takeover=True).status_code == 200

        resp = self.review(client, request_id, data, takeover=True)
        assert resp.status_code == 200, resp.get_json()
        message = resp.get_json()["message"]
        assert "原地覆盖" in message
        assert "已下架并移入回收站" not in message

        records = storage.read_records("servers")
        assert len(records) == 1
        assert records[0]["序列号"] == "SRV-OLD"
        # 新数据覆盖旧数据
        assert records[0]["品牌"] == "H3C"
        assert records[0]["备注"] == "换机后"
        assert records[0]["管理ip"] == "10.1.1.9/24"
        # 同一台机器没下架，回收站里不该有它
        assert trash_serials() == set()

    def test_direct_add_same_serial_without_takeover_is_explained(self, app_env, admin):
        """没开占用模式时，同序列号要给「怎么替换 / 该用哪个入口」的提示，而不是干巴巴的主键重复。"""
        client = setup_base(app_env)
        login(client)
        body = {**new_server_data(slot="3-4"), "序列号": "SRV-OLD"}

        resp = client.post("/api/servers/add", json=body)
        assert resp.status_code == 400
        error = resp.get_json()["error"]
        assert "已存在于 机房A / C1 / 3-4" in error
        assert "占用模式" in error
        assert "修改" in error

    def test_direct_add_same_serial_overwrites(self, app_env, admin):
        client = setup_base(app_env)
        login(client)
        body = {**new_server_data(slot="3-4"), "序列号": "SRV-OLD", "品牌": "H3C", "takeover": True}

        resp = client.post("/api/servers/add", json=body)
        assert resp.status_code == 200, resp.get_json()
        message = resp.get_json()["message"]
        assert "原地覆盖" in message
        assert "已下架并移入回收站" not in message

        records = storage.read_records("servers")
        assert [row["序列号"] for row in records] == ["SRV-OLD"]
        assert records[0]["品牌"] == "H3C"
        assert trash_serials() == set()


class TestTrashUpdate:
    """回收站「保存修改」：只更新快照并留在回收站，不做完整服务器校验。"""

    def _move_down(self, client, serial="SRV-DOWN"):
        add_official("servers", {
            "序列号": serial, "客户id": "1", "所在机房": "机房A", "所属机柜": "C1",
            "所属机位": "7-8", "管理ip": "10.1.1.30/24", "带外ip": "10.1.3.30/24",
            "品牌": "HPE", "备注": "待上架",
        })
        client.post("/api/servers/delete", json={"序列号": serial})
        return next(row["id"] for row in services.trash_rows() if row["serial_number"] == serial)

    def test_saves_fields_without_restoring(self, app_env, admin):
        client = setup_base(app_env)
        login(client)
        trash_id = self._move_down(client)

        resp = client.patch(f"/api/trash/{trash_id}", json={"server_data": {
            "品牌": "Dell", "型号": "R750", "所属机位": "9-10",
        }})
        assert resp.status_code == 200
        assert "仍在回收站" in resp.get_json()["message"]
        # 服务器表不动，记录还在回收站
        assert "SRV-DOWN" not in {row["序列号"] for row in storage.read_records("servers")}
        assert "SRV-DOWN" in trash_serials()
        item = services.trash_entry(trash_id)
        assert (item["品牌"], item["型号"], item["所属机位"]) == ("Dell", "R750", "9-10")
        # 没提交的字段保留快照原值
        assert item["备注"] == "待上架"

    def test_saves_draft_that_would_fail_restore(self, app_env, admin):
        """客户未登记 / 机房越界也能先保存下来；完整校验留到重新上架那一刻。"""
        client = setup_base(app_env)
        login(client)
        trash_id = self._move_down(client)

        resp = client.patch(f"/api/trash/{trash_id}", json={"server_data": {
            "客户id": "999", "所在机房": "机房Z", "所属机柜": "C9", "所属机位": "99-100",
        }})
        assert resp.status_code == 200
        assert services.trash_entry(trash_id)["客户id"] == "999"

        blocked = client.post(f"/api/trash/{trash_id}/restore", json={})
        assert blocked.status_code == 400
        assert "SRV-DOWN" in trash_serials()

    def test_change_serial_moves_attachments(self, app_env, admin):
        client = setup_base(app_env)
        login(client)
        trash_id = self._move_down(client)
        with storage.db() as conn:
            conn.execute(
                "INSERT INTO attachments (id,serial_number,filename,stored_name,size,uploader,uploaded_at)"
                " VALUES (?,?,?,?,?,?,?)",
                ("att1", "SRV-DOWN", "a.pdf", "stored-a.pdf", 12, "admin", "2026-11-14 10:00:00"),
            )

        resp = client.patch(f"/api/trash/{trash_id}", json={"server_data": {"序列号": "SRV-RENAMED"}})
        assert resp.status_code == 200
        item = services.trash_entry(trash_id)
        assert item["serial_number"] == "SRV-RENAMED"
        assert item["序列号"] == "SRV-RENAMED"
        with storage.db() as conn:
            row = conn.execute("SELECT serial_number FROM attachments WHERE id='att1'").fetchone()
        assert row["serial_number"] == "SRV-RENAMED"

    def test_blank_serial_keeps_original(self, app_env, admin):
        client = setup_base(app_env)
        login(client)
        trash_id = self._move_down(client)
        resp = client.patch(f"/api/trash/{trash_id}", json={"server_data": {"序列号": "", "品牌": "Lenovo"}})
        assert resp.status_code == 200
        item = services.trash_entry(trash_id)
        assert item["serial_number"] == "SRV-DOWN"
        assert item["品牌"] == "Lenovo"

    def test_rejects_serial_used_by_server(self, app_env, admin):
        client = setup_base(app_env)
        login(client)
        trash_id = self._move_down(client)
        resp = client.patch(f"/api/trash/{trash_id}", json={"server_data": {"序列号": "SRV-OLD"}})
        assert resp.status_code == 400
        assert "服务器表" in resp.get_json()["error"]
        assert services.trash_entry(trash_id)["serial_number"] == "SRV-DOWN"

    def test_rejects_serial_used_by_other_trash_entry(self, app_env, admin):
        client = setup_base(app_env)
        login(client)
        trash_id = self._move_down(client)
        assert client.post("/api/trash", json={"序列号": "SRV-MANUAL"}).status_code == 200

        resp = client.patch(f"/api/trash/{trash_id}", json={"server_data": {"序列号": "SRV-MANUAL"}})
        assert resp.status_code == 400
        assert "回收站" in resp.get_json()["error"]

    def test_missing_entry_returns_400(self, app_env, admin):
        client = setup_base(app_env)
        login(client)
        resp = client.patch("/api/trash/not-exist", json={"server_data": {"品牌": "X"}})
        assert resp.status_code == 400
        assert "不存在" in resp.get_json()["error"]


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

    def test_room_can_save_own_room_record(self, app_env, admin, room_user):
        """机房人员也能改本机房记录的信息并保存（不上架）。"""
        admin_client = setup_base(app_env)
        login(admin_client)
        self._seed(admin_client, admin_client)

        client = app_env["app"].test_client()
        login(client, "room1")
        items = client.get("/api/trash").get_json()["items"]
        target = next(item for item in items if item["serial_number"] == "SRV-A1")
        resp = client.patch(f"/api/trash/{target['id']}", json={"server_data": {
            "品牌": "HPE", "型号": "DL380", "备注": "机房补录",
        }})
        assert resp.status_code == 200
        item = services.trash_entry(target["id"])
        assert (item["品牌"], item["型号"], item["备注"]) == ("HPE", "DL380", "机房补录")
        # 只改快照，没上架
        assert "SRV-A1" not in {row["序列号"] for row in storage.read_records("servers")}

    def test_room_cannot_save_record_into_other_room(self, app_env, admin, room_user):
        admin_client = setup_base(app_env)
        login(admin_client)
        self._seed(admin_client, admin_client)

        client = app_env["app"].test_client()
        login(client, "room1")
        items = client.get("/api/trash").get_json()["items"]
        target = next(item for item in items if item["serial_number"] == "SRV-A1")
        resp = client.patch(f"/api/trash/{target['id']}", json={"server_data": {"所在机房": "机房B"}})
        assert resp.status_code == 400
        assert "本机房" in resp.get_json()["error"]
        assert services.trash_entry(target["id"])["所在机房"] == "机房A"

    def test_room_cannot_save_other_room_record(self, app_env, admin, room_user):
        admin_client = setup_base(app_env)
        login(admin_client)
        self._seed(admin_client, admin_client)

        admin_items = admin_client.get("/api/trash").get_json()["items"]
        other = next(item for item in admin_items if item["serial_number"] == "SRV-B1")

        client = app_env["app"].test_client()
        login(client, "room1")
        resp = client.patch(f"/api/trash/{other['id']}", json={"server_data": {"品牌": "X"}})
        assert resp.status_code == 403

    def test_business_user_cannot_save(self, app_env, admin, biz_user):
        admin_client = setup_base(app_env)
        login(admin_client)
        self._seed(admin_client, admin_client)
        target = admin_client.get("/api/trash").get_json()["items"][0]

        client = app_env["app"].test_client()
        login(client, "biz1")
        assert client.patch(f"/api/trash/{target['id']}", json={"server_data": {"品牌": "X"}}).status_code == 403

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
        # 该用例已被需求 1 取代：业务人员可读回收站（本单位）。保留方法名仅为兼容，
        # 断言改为「空回收站时能正常读取、且不能做任何写操作」。
        client = setup_base(app_env)
        login(client, "biz1")
        resp = client.get("/api/trash")
        assert resp.status_code == 200
        assert resp.get_json()["can_edit"] is False
        assert resp.get_json()["can_manage"] is False


class TestBusinessUserTrashAccess:
    """业务人员只读回收站：只看归属单位=本单位，不能改信息 / 重新上架 / 登记 / 删除。"""

    def _seed(self):
        """铺两台服务器各进回收站：单位A（本单位）和单位B（外单位）。"""
        add_official("cabinets", {"机房": "机房B", "机柜": "C1", "机位": "1-10"})
        for serial, unit in (("SRV-UA", "单位A"), ("SRV-UB", "单位B")):
            add_official("servers", {
                "序列号": serial, "客户id": "1", "归属单位": unit,
                "所在机房": "机房A", "所属机柜": "C1", "所属机位": "1-2",
                "管理ip": "10.1.1.15/24", "带外ip": "10.1.3.15/24",
            })
            server = services.find_record("servers", serial)
            with storage.db() as conn:
                services.move_server_to_trash(conn, server, reason="测试", deleted_by="admin")
            storage.write_records(
                "servers", [r for r in storage.read_records("servers") if r["序列号"] != serial]
            )

    def test_business_sees_only_own_unit(self, app_env, admin, biz_user):
        self._seed()
        client = app_env["app"].test_client()
        login(client, "biz1")
        resp = client.get("/api/trash")
        assert resp.status_code == 200
        body = resp.get_json()
        assert {item["serial_number"] for item in body["items"]} == {"SRV-UA"}
        assert body["can_edit"] is False
        assert body["can_manage"] is False

    def test_admin_sees_all_units(self, app_env, admin, biz_user):
        self._seed()
        client = setup_base(app_env)
        login(client)
        body = client.get("/api/trash").get_json()
        serials = {item["serial_number"] for item in body["items"]}
        assert {"SRV-UA", "SRV-UB"}.issubset(serials)
        assert body["can_edit"] is True

    def test_business_cannot_mutate_trash(self, app_env, admin, biz_user):
        self._seed()
        admin_client = setup_base(app_env)
        login(admin_client)
        target = next(item for item in admin_client.get("/api/trash").get_json()["items"]
                      if item["serial_number"] == "SRV-UA")

        client = app_env["app"].test_client()
        login(client, "biz1")
        # 改信息 / 重新上架：即使知道自己单位的记录 id 也必须被挡
        assert client.patch(f"/api/trash/{target['id']}", json={"server_data": {"品牌": "X"}}).status_code == 403
        assert client.post(f"/api/trash/{target['id']}/restore", json={}).status_code == 403
        # 手动登记与永久删除本来就是管理人员专属
        assert client.post("/api/trash", json={"序列号": "SRV-NEW"}).status_code == 403
        assert client.delete(f"/api/trash/{target['id']}").status_code == 403
        # 记录未被改动，也没有被上架
        assert services.trash_entry(target["id"])["品牌"] != "X"
        assert "SRV-UA" not in {row["序列号"] for row in storage.read_records("servers")}
