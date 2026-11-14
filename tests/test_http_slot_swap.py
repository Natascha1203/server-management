"""机位交换（交换机位单）测试：

1. 管理员交换两台正式服务器：直接生效，两侧机位对调且留有版本/审计留痕
2. 非管理员提交交换：进入待审批，机位不变；管理员批准后两侧同时生效
3. 交换对象为「待审核变更」时，批准后改写对方 new_data 的机位
4. 交换对象为「待实施请求」时，批准后改写对方 stage_data 的机位
5. 目标机位空闲 / 我方已有待审核变更 等异常路径
6. /api/statistics 按机房分组
7. 对方占用多行时机位范围整段对调（含待审核多行对象）
8. /api/cabinet-options 暴露机位占用者的完整范围 owner_range
"""
import json

from app import services, storage
from app.core import normalize_row


def add_official(entity, row):
    """绕过审批直接写正式表（测试铺底数据用）。"""
    records = storage.read_records(entity)
    records.append(normalize_row(entity, row))
    storage.write_records(entity, records)


def _login_as(client, username, password="pw"):
    resp = client.post("/login", data={"username": username, "password": password})
    assert resp.status_code == 302


def _setup():
    add_official("customers", {"客户id": "1", "名称": "客户一"})
    add_official("cabinets", {
        "机房": "机房A", "机柜": "C1", "机位": "1-10",
        "管理ip网段": "10.1.1.*", "交易ip网段": "10.1.2.*", "带外ip网段": "10.1.3.*",
    })
    add_official("cabinets", {
        "机房": "机房B", "机柜": "C2", "机位": "1-10",
        "管理ip网段": "10.2.1.*", "交易ip网段": "10.2.2.*", "带外ip网段": "10.2.3.*",
    })
    server_a = {
        "序列号": "SRV-A", "客户id": "1", "归属单位": "单位A",
        "所在机房": "机房A", "所属机柜": "C1", "所属机位": "3-3",
        "管理ip": "10.1.1.5", "交易ip": "10.1.2.5", "带外ip": "10.1.3.5",
    }
    server_b = {
        "序列号": "SRV-B", "客户id": "1", "归属单位": "单位A",
        "所在机房": "机房A", "所属机柜": "C1", "所属机位": "7-7",
        "管理ip": "10.1.1.9", "交易ip": "10.1.2.9", "带外ip": "10.1.3.9",
    }
    add_official("servers", server_a)
    add_official("servers", server_b)
    return server_a, server_b


def _server(serial):
    return next((row for row in storage.read_records("servers") if row["序列号"] == serial), None)


def _swap_body(old, new_slot, room="机房A", cabinet="C1"):
    return {
        "old": old,
        "new": {**old, "所在机房": room, "所属机柜": cabinet, "所属机位": new_slot},
        "target": {"room": room, "cabinet": cabinet, "slot": new_slot.split("-")[0]},
    }


def _pending_swap_change():
    with storage.db() as conn:
        return conn.execute(
            "SELECT * FROM changes WHERE status='pending' AND operation=? ORDER BY submitted_at DESC LIMIT 1",
            (services.SWAP_OPERATION,),
        ).fetchone()


class TestAdminSwap:
    def test_admin_swap_takes_effect_immediately(self, app_env, admin):
        server_a, _server_b = _setup()
        client = app_env["app"].test_client()
        _login_as(client, "admin")

        resp = client.post("/api/servers/swap-slot", json=_swap_body(server_a, "7-7"))
        assert resp.status_code == 200, resp.get_json()
        assert "直接生效" in resp.get_json()["message"]
        assert _server("SRV-A")["所属机位"] == "7-7"
        assert _server("SRV-B")["所属机位"] == "3-3"
        # 留痕：一条 approved 交换机位记录 + 版本记录
        with storage.db() as conn:
            approved = conn.execute(
                "SELECT COUNT(*) FROM changes WHERE status='approved' AND operation=?",
                (services.SWAP_OPERATION,),
            ).fetchone()[0]
            versions = conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0]
        assert approved == 1
        assert versions >= 1

    def test_admin_cross_room_swap(self, app_env, admin):
        server_a, _ = _setup()
        add_official("servers", {
            "序列号": "SRV-C", "客户id": "1", "归属单位": "单位A",
            "所在机房": "机房B", "所属机柜": "C2", "所属机位": "2-2",
            "管理ip": "10.2.1.2", "交易ip": "10.2.2.2", "带外ip": "10.2.3.2",
        })
        client = app_env["app"].test_client()
        _login_as(client, "admin")
        resp = client.post("/api/servers/swap-slot", json=_swap_body(server_a, "2-2", room="机房B", cabinet="C2"))
        assert resp.status_code == 200, resp.get_json()
        assert _server("SRV-A")["所在机房"] == "机房B"
        assert _server("SRV-A")["所属机位"] == "2-2"
        assert _server("SRV-C")["所在机房"] == "机房A"
        assert _server("SRV-C")["所属机位"] == "3-3"


class TestPendingSwap:
    def test_non_admin_swap_requires_review(self, app_env, admin):
        server_a, _ = _setup()
        client = app_env["app"].test_client()
        _login_as(client, "room1")

        resp = client.post("/api/servers/swap-slot", json=_swap_body(server_a, "7-7"))
        assert resp.status_code == 200, resp.get_json()
        assert "已提交机位交换申请" in resp.get_json()["message"]
        # 审批前机位保持不变
        assert _server("SRV-A")["所属机位"] == "3-3"
        assert _server("SRV-B")["所属机位"] == "7-7"

        change = _pending_swap_change()
        assert change is not None
        assert change["record_key"] == "SRV-A"

        admin_client = app_env["app"].test_client()
        _login_as(admin_client, "admin")
        approve = admin_client.post(f"/api/changes/{change['id']}/review", json={"action": "approve"})
        assert approve.status_code == 200, approve.get_json()
        assert _server("SRV-A")["所属机位"] == "7-7"
        assert _server("SRV-B")["所属机位"] == "3-3"

    def test_swap_with_pending_change_target(self, app_env, admin):
        server_a, _ = _setup()
        biz = app_env["app"].test_client()
        _login_as(biz, "biz1")
        # 业务人员新增 SRV-D 到机位 5-5，形成待审核变更
        resp = biz.post("/api/servers/add", json={
            "序列号": "SRV-D", "客户id": "1", "归属单位": "单位A",
            "所在机房": "机房A", "所属机柜": "C1", "所属机位": "5-5",
            "管理ip": "10.1.1.50", "交易ip": "10.1.2.50", "带外ip": "10.1.3.50",
        })
        assert resp.status_code == 200, resp.get_json()
        new_change_id = resp.get_json()["change_id"]

        client = app_env["app"].test_client()
        _login_as(client, "room1")
        resp = client.post("/api/servers/swap-slot", json=_swap_body(server_a, "5-5"))
        assert resp.status_code == 200, resp.get_json()
        swap = _pending_swap_change()
        assert swap is not None

        admin_client = app_env["app"].test_client()
        _login_as(admin_client, "admin")
        resp = admin_client.post(f"/api/changes/{swap['id']}/review", json={"action": "approve"})
        assert resp.status_code == 200, resp.get_json()
        assert _server("SRV-A")["所属机位"] == "5-5"
        # 对方（待审核新增）的 new_data 机位被改写为我方原机位
        with storage.db() as conn:
            row = conn.execute("SELECT new_data,status FROM changes WHERE id=?", (new_change_id,)).fetchone()
        data = json.loads(row["new_data"])
        assert row["status"] == "pending"
        assert data["所属机位"] == "3-3"

    def test_swap_with_implementation_request_target(self, app_env, admin):
        server_a, _ = _setup()
        request_data = services.normalize_rack_request({
            "序列号": "SRV-E", "请求类型": "上架", "是否重装系统": "是",
            "客户id": "1", "所在机房": "机房A",
        })
        with storage.db() as conn:
            request_id = services.insert_rack_request(
                conn, request_data, {"用户id": "admin", "身份": "管理人员"}
            )
            stage = {
                "客户id": "1", "所在机房": "机房A", "所属机柜": "C1", "所属机位": "9-9",
                "管理ip": "10.1.1.90", "交易ip": "10.1.2.90", "带外ip": "10.1.3.90",
            }
            conn.execute(
                "UPDATE rack_requests SET stage='implementation',stage_data=? WHERE id=?",
                (json.dumps(stage, ensure_ascii=False), request_id),
            )

        client = app_env["app"].test_client()
        _login_as(client, "room1")
        resp = client.post("/api/servers/swap-slot", json=_swap_body(server_a, "9-9"))
        assert resp.status_code == 200, resp.get_json()
        swap = _pending_swap_change()

        admin_client = app_env["app"].test_client()
        _login_as(admin_client, "admin")
        resp = admin_client.post(f"/api/changes/{swap['id']}/review", json={"action": "approve"})
        assert resp.status_code == 200, resp.get_json()
        assert _server("SRV-A")["所属机位"] == "9-9"
        with storage.db() as conn:
            row = conn.execute("SELECT stage_data,status FROM rack_requests WHERE id=?", (request_id,)).fetchone()
        stage = json.loads(row["stage_data"])
        assert row["status"] == "pending"
        assert stage["所属机位"] == "3-3"
        assert stage["所在机房"] == "机房A"
        assert stage["所属机柜"] == "C1"


class TestSwapValidation:
    def test_rejects_free_target(self, app_env, admin):
        server_a, _ = _setup()
        client = app_env["app"].test_client()
        _login_as(client, "admin")
        resp = client.post("/api/servers/swap-slot", json=_swap_body(server_a, "6-6"))
        assert resp.status_code == 400
        assert "未被占用" in resp.get_json()["error"]

    def test_rejects_same_slot(self, app_env, admin):
        server_a, _ = _setup()
        client = app_env["app"].test_client()
        _login_as(client, "admin")
        resp = client.post("/api/servers/swap-slot", json=_swap_body(server_a, "3-3"))
        assert resp.status_code == 400

    def test_rejects_when_self_has_pending_change(self, app_env, admin):
        server_a, _ = _setup()
        biz = app_env["app"].test_client()
        _login_as(biz, "biz1")
        resp = biz.post("/api/servers/update", json={
            "old": server_a,
            "new": {**server_a, "型号": "X100"},
        })
        assert resp.status_code == 200, resp.get_json()

        client = app_env["app"].test_client()
        _login_as(client, "room1")
        resp = client.post("/api/servers/swap-slot", json=_swap_body(server_a, "7-7"))
        assert resp.status_code == 409
        assert "待审批" in resp.get_json()["error"]


class TestStatisticsByRoom:
    def test_statistics_returns_room_groups(self, app_env, admin):
        _setup()
        client = app_env["app"].test_client()
        _login_as(client, "admin")
        resp = client.get("/api/statistics")
        assert resp.status_code == 200
        data = resp.get_json()
        names = {room["name"] for room in data["rooms"]}
        assert {"机房A", "机房B"} <= names
        room_a = next(room for room in data["rooms"] if room["name"] == "机房A")
        assert room_a["cabinet_count"] == 1
        cabinet = room_a["cabinets"][0]
        assert cabinet["name"] == "C1"
        slot3 = next(s for s in cabinet["slots"] if s["slot"] == "3")
        assert slot3["servers"][0]["序列号"] == "SRV-A"


def _set_slots(serial, slot_range):
    records = storage.read_records("servers")
    for row in records:
        if row["序列号"] == serial:
            row["所属机位"] = slot_range
    storage.write_records("servers", records)


class TestMultiSlotSwap:
    """对方占用多行时机位范围必须整段对调（回归：原先只换被点击的单格）。"""

    def test_admin_swap_replaces_full_ranges(self, app_env, admin):
        _setup()
        _set_slots("SRV-A", "2-3")
        _set_slots("SRV-B", "7-8")
        client = app_env["app"].test_client()
        _login_as(client, "admin")
        # 模拟旧前端：new 只带被点击的单格 7-7
        body = {
            "old": _server("SRV-A"),
            "new": {**_server("SRV-A"), "所属机位": "7-7"},
            "target": {"room": "机房A", "cabinet": "C1", "slot": "7"},
        }
        resp = client.post("/api/servers/swap-slot", json=body)
        assert resp.status_code == 200, resp.get_json()
        assert _server("SRV-A")["所属机位"] == "7-8"
        assert _server("SRV-B")["所属机位"] == "2-3"

    def test_swap_expands_to_peer_multi_slot(self, app_env, admin):
        _setup()
        _set_slots("SRV-A", "3-3")
        _set_slots("SRV-B", "7-9")
        client = app_env["app"].test_client()
        _login_as(client, "admin")
        body = {
            "old": _server("SRV-A"),
            "new": {**_server("SRV-A"), "所属机位": "8-8"},
            "target": {"room": "机房A", "cabinet": "C1", "slot": "8"},
        }
        resp = client.post("/api/servers/swap-slot", json=body)
        assert resp.status_code == 200, resp.get_json()
        # 我方扩为对方整段，对方收为我方单格
        assert _server("SRV-A")["所属机位"] == "7-9"
        assert _server("SRV-B")["所属机位"] == "3-3"

    def test_swap_full_range_with_pending_multi_slot_target(self, app_env, admin):
        server_a, _ = _setup()
        _set_slots("SRV-A", "2-3")
        biz = app_env["app"].test_client()
        _login_as(biz, "biz1")
        resp = biz.post("/api/servers/add", json={
            "序列号": "SRV-F", "客户id": "1", "归属单位": "单位A",
            "所在机房": "机房A", "所属机柜": "C1", "所属机位": "8-9",
            "管理ip": "10.1.1.80", "交易ip": "10.1.2.80", "带外ip": "10.1.3.80",
        })
        assert resp.status_code == 200, resp.get_json()
        new_change_id = resp.get_json()["change_id"]

        client = app_env["app"].test_client()
        _login_as(client, "room1")
        resp = client.post("/api/servers/swap-slot", json={
            "old": _server("SRV-A"),
            "new": {**_server("SRV-A"), "所属机位": "8-8"},
            "target": {"room": "机房A", "cabinet": "C1", "slot": "8"},
        })
        assert resp.status_code == 200, resp.get_json()
        swap = _pending_swap_change()

        admin_client = app_env["app"].test_client()
        _login_as(admin_client, "admin")
        resp = admin_client.post(f"/api/changes/{swap['id']}/review", json={"action": "approve"})
        assert resp.status_code == 200, resp.get_json()
        assert _server("SRV-A")["所属机位"] == "8-9"
        with storage.db() as conn:
            row = conn.execute("SELECT new_data FROM changes WHERE id=?", (new_change_id,)).fetchone()
        assert json.loads(row["new_data"])["所属机位"] == "2-3"

    def test_cabinet_options_exposes_owner_range(self, app_env, admin):
        _setup()
        _set_slots("SRV-B", "7-8")
        client = app_env["app"].test_client()
        _login_as(client, "admin")
        resp = client.get("/api/cabinet-options")
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()
        cabinet = next(c for room in data["rooms"] if room["name"] == "机房A" for c in room["cabinets"] if c["name"] == "C1")
        slot7 = next(s for s in cabinet["slots"] if s["slot"] == "7")
        slot8 = next(s for s in cabinet["slots"] if s["slot"] == "8")
        slot5 = next(s for s in cabinet["slots"] if s["slot"] == "5")
        assert slot7["server"] == "SRV-B" and slot7["owner_range"] == "7-8"
        assert slot8["owner_range"] == "7-8"
        assert slot5["status"] == "free" and slot5["owner_range"] == ""
