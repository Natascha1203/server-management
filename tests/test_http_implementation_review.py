"""具体实施阶段审核的 HTTP 层测试：
1. /api/cabinet-options 槽位数据携带 request_id（前端据此把本请求机位显示为绿色可选）
2. 实施审核不得占用其他待实施请求已分配的机位/IP
3. 本请求自身资源不受影响，可正常完成
"""
import io
import json

from app import services, storage
from app.core import normalize_row


def add_official(entity, row):
    """绕过审批直接写正式表（测试铺底数据用）。"""
    records = storage.read_records(entity)
    records.append(normalize_row(entity, row))
    storage.write_records(entity, records)


def _login(client):
    resp = client.post("/login", data={"username": "admin", "password": "pw"})
    assert resp.status_code == 302


def _login_as(client, username, password="pw"):
    resp = client.post("/login", data={"username": username, "password": password})
    assert resp.status_code == 302


def _put_in_implementation(serial, slot, admin_ip, trade_ip, oob_ip):
    data = services.normalize_rack_request({
        "序列号": serial, "请求类型": "上架", "是否重装系统": "是",
        "客户id": "1", "所在机房": "机房A",
    })
    with storage.db() as conn:
        request_id = services.insert_rack_request(conn, data, {"用户id": "admin", "身份": "管理人员"})
    server_data = {
        "客户id": "1", "所在机房": "机房A", "所属机柜": "C1", "所属机位": slot,
        "管理ip": admin_ip, "交易ip": trade_ip, "带外ip": oob_ip,
    }
    with storage.db() as conn:
        conn.execute(
            "UPDATE rack_requests SET stage='implementation',stage_data=? WHERE id=?",
            (json.dumps(server_data, ensure_ascii=False), request_id),
        )
    return request_id, server_data


def _review(client, request_id, server_data):
    return client.post(
        f"/api/rack-requests/{request_id}/review",
        json={"action": "approve", "server_data": server_data},
    )


def _setup(app_env, admin):
    add_official("customers", {"客户id": "1", "名称": "客户一"})
    add_official("cabinets", {
        "机房": "机房A", "机柜": "C1", "机位": "1-10",
        "管理ip网段": "10.1.1.*", "交易ip网段": "10.1.2.*", "带外ip网段": "10.1.3.*",
    })
    return app_env["app"].test_client()


class TestCabinetOptionsRequestId:
    def test_slot_carries_request_id(self, app_env, admin):
        client = _setup(app_env, admin)
        _login(client)
        request_id, _ = _put_in_implementation("SRV-A", "3-4", "10.1.1.5", "10.1.2.5", "10.1.3.5")

        resp = client.get("/api/cabinet-options")
        assert resp.status_code == 200
        options = resp.get_json()
        cabinet = next(
            c for r in options["rooms"] if r["name"] == "机房A"
            for c in r["cabinets"] if c["name"] == "C1"
        )
        slot3 = next(s for s in cabinet["slots"] if s["slot"] == "3")
        assert slot3["status"] == "pending_implementation"
        assert slot3["server"] == "SRV-A"
        assert slot3["selectable"] is False
        assert slot3["request_id"] == request_id
        # 空闲槽位不应带 request_id
        slot6 = next(s for s in cabinet["slots"] if s["slot"] == "6")
        assert slot6["status"] == "free"
        assert "request_id" not in slot6 or not slot6["request_id"]


class TestImplementationReviewConflicts:
    def test_blocks_other_request_slot(self, app_env, admin):
        client = _setup(app_env, admin)
        _login(client)
        _request_a, data_a = _put_in_implementation("SRV-A", "3-4", "10.1.1.5", "10.1.2.5", "10.1.3.5")
        request_b, data_b = _put_in_implementation("SRV-B", "7-8", "10.1.1.9", "10.1.2.9", "10.1.3.9")

        # B 试图改占 A 的机位 3-4
        hijack = {**data_b, "所属机位": "3-4"}
        resp = _review(client, request_b, hijack)
        assert resp.status_code == 400
        assert "已被待具体实施服务器 SRV-A 占用" in resp.get_json()["error"]

    def test_blocks_other_request_ip(self, app_env, admin):
        client = _setup(app_env, admin)
        _login(client)
        _request_a, data_a = _put_in_implementation("SRV-A", "3-4", "10.1.1.5", "10.1.2.5", "10.1.3.5")
        request_b, data_b = _put_in_implementation("SRV-B", "7-8", "10.1.1.9", "10.1.2.9", "10.1.3.9")

        # B 保持自己的机位，但抢占 A 的管理 IP
        hijack = {**data_b, "管理ip": "10.1.1.5"}
        resp = _review(client, request_b, hijack)
        assert resp.status_code == 400
        assert "已被待具体实施服务器 SRV-A 使用" in resp.get_json()["error"]

    def test_allows_own_resources(self, app_env, admin):
        client = _setup(app_env, admin)
        _login(client)
        request_a, data_a = _put_in_implementation("SRV-A", "3-4", "10.1.1.5", "10.1.2.5", "10.1.3.5")

        # A 用自己已分配的机位/IP 完成实施，不应被自己挡住
        resp = _review(client, request_a, data_a)
        assert resp.status_code == 200
        serials = {s["序列号"] for s in storage.read_records("servers")}
        assert "SRV-A" in serials

    def test_both_requests_complete_sequentially(self, app_env, admin):
        client = _setup(app_env, admin)
        _login(client)
        request_a, data_a = _put_in_implementation("SRV-A", "3-4", "10.1.1.5", "10.1.2.5", "10.1.3.5")
        request_b, data_b = _put_in_implementation("SRV-B", "7-8", "10.1.1.9", "10.1.2.9", "10.1.3.9")

        assert _review(client, request_a, data_a).status_code == 200
        # A 完成后进入正式表，B 再完成也互不冲突
        assert _review(client, request_b, data_b).status_code == 200
        serials = {s["序列号"] for s in storage.read_records("servers")}
        assert {"SRV-A", "SRV-B"} <= serials


class TestImportOverwriteMode:
    """上传导入时「全量覆盖 / 增量覆盖」两种模式的行为。

    用 cabinets 实体（主键=机房+机柜，校验简单）验证合并语义：
    - full：同主键记录被上传内容整体替换，未提供列置空；
    - merge：同主键记录仅覆盖上传文件提供的列，其余列保留旧值。

    以「机房人员」身份登录（不免审批），变更进入待审批表，便于观察合并结果。
    """

    def _existing_cabinet(self):
        add_official("cabinets", {
            "机房": "机房A", "机柜": "C1", "机位": "1-10",
            "瓦数(kw)": "5", "费用(年)": "1000",
            "管理ip网段": "10.1.1.*", "交易ip网段": "10.1.2.*", "带外ip网段": "10.1.3.*",
        })

    def _import(self, client, csv_text, mode):
        return client.post(
            "/api/cabinets/import",
            data={"file": (io.BytesIO(csv_text.encode("utf-8-sig")), "t.csv"), "mode": mode},
            content_type="multipart/form-data",
        )

    def _pending_change(self):
        with storage.db() as conn:
            row = conn.execute(
                "SELECT * FROM changes WHERE status='pending' AND entity='cabinets' ORDER BY id LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def test_full_mode_overwrites_all_fields(self, app_env, admin):
        self._existing_cabinet()
        client = app_env["app"].test_client()
        _login_as(client, "room1")
        # 全量：只提供 机房/机柜/机位，瓦数与费用未提供 → 覆盖后应为空
        csv_text = "机房,机柜,机位,瓦数(kw),费用(年),管理ip网段,交易ip网段,带外ip网段\n机房A,C1,1-20,,,,,\n"
        resp = self._import(client, csv_text, "full")
        assert resp.status_code == 200
        change = self._pending_change()
        new_data = json.loads(change["new_data"])
        assert new_data["机位"] == "1-20"
        assert new_data["瓦数(kw)"] == ""
        assert new_data["费用(年)"] == ""

    def test_merge_mode_preserves_unprovided_fields(self, app_env, admin):
        self._existing_cabinet()
        client = app_env["app"].test_client()
        _login_as(client, "room1")
        # 增量：只提供 机位，瓦数与费用未提供 → 应保留旧值
        csv_text = "机房,机柜,机位,瓦数(kw),费用(年),管理ip网段,交易ip网段,带外ip网段\n机房A,C1,1-20,,,,,\n"
        resp = self._import(client, csv_text, "merge")
        assert resp.status_code == 200
        change = self._pending_change()
        new_data = json.loads(change["new_data"])
        assert new_data["机位"] == "1-20"
        assert new_data["瓦数(kw)"] == "5"
        assert new_data["费用(年)"] == "1000"

    def test_merge_mode_overrides_provided_fields(self, app_env, admin):
        self._existing_cabinet()
        client = app_env["app"].test_client()
        _login_as(client, "room1")
        # 增量：提供了 瓦数，则瓦数被覆盖，费用保留旧值
        csv_text = "机房,机柜,机位,瓦数(kw),费用(年),管理ip网段,交易ip网段,带外ip网段\n机房A,C1,1-10,8,,,,\n"
        resp = self._import(client, csv_text, "merge")
        assert resp.status_code == 200
        change = self._pending_change()
        new_data = json.loads(change["new_data"])
        assert new_data["瓦数(kw)"] == "8"
        assert new_data["费用(年)"] == "1000"

    def test_invalid_mode_rejected(self, app_env, admin):
        self._existing_cabinet()
        client = app_env["app"].test_client()
        _login(client)
        csv_text = "机房,机柜,机位,瓦数(kw),费用(年),管理ip网段,交易ip网段,带外ip网段\n机房A,C1,1-20,,,,,\n"
        resp = self._import(client, csv_text, "bogus")
        assert resp.status_code == 400


class TestAdminDirectMutate:
    """管理员（管理人员）对资产数据的变更免审批直接生效；其他角色仍走待审批。

    覆盖 add / update / delete / delete-batch / import 五个入口，
    并校验直改仍保留留痕（changes 表 approved 记录 + versions 版本记录）。
    """

    def _existing_cabinet(self):
        add_official("cabinets", {
            "机房": "机房A", "机柜": "C1", "机位": "1-10",
            "瓦数(kw)": "5", "费用(年)": "1000",
            "管理ip网段": "10.1.1.*", "交易ip网段": "10.1.2.*", "带外ip网段": "10.1.3.*",
        })

    def _cabinet(self, key="机房A|C1"):
        for row in storage.read_records("cabinets"):
            if f"{row['机房']}|{row['机柜']}" == key:
                return row
        return None

    def _count(self, sql, args=()):
        with storage.db() as conn:
            return conn.execute(sql, args).fetchone()[0]

    def _pending_count(self, entity="cabinets"):
        return self._count("SELECT COUNT(*) FROM changes WHERE status='pending' AND entity=?", (entity,))

    def _approved_count(self, entity="cabinets"):
        return self._count("SELECT COUNT(*) FROM changes WHERE status='approved' AND entity=?", (entity,))

    def _version_count(self, entity="cabinets"):
        return self._count("SELECT COUNT(*) FROM versions WHERE entity=?", (entity,))

    def test_admin_add_takes_effect_immediately(self, app_env, admin):
        client = app_env["app"].test_client()
        _login(client)
        row = {
            "机房": "机房B", "机柜": "C9", "机位": "1-5", "瓦数(kw)": "3", "费用(年)": "500",
            "管理ip网段": "10.9.1.*", "交易ip网段": "10.9.2.*", "带外ip网段": "10.9.3.*",
        }
        resp = client.post("/api/cabinets/add", json=row)
        assert resp.status_code == 200
        assert "直接生效" in resp.get_json()["message"]
        assert self._cabinet("机房B|C9") is not None
        assert self._pending_count() == 0
        assert self._approved_count() == 1

    def test_admin_update_takes_effect_immediately(self, app_env, admin):
        self._existing_cabinet()
        client = app_env["app"].test_client()
        _login(client)
        resp = client.post("/api/cabinets/update", json={
            "old": {"机房": "机房A", "机柜": "C1", "机位": "1-10"},
            "new": {
                "机房": "机房A", "机柜": "C1", "机位": "1-20", "瓦数(kw)": "5", "费用(年)": "1000",
                "管理ip网段": "10.1.1.*", "交易ip网段": "10.1.2.*", "带外ip网段": "10.1.3.*",
            },
        })
        assert resp.status_code == 200
        assert "直接生效" in resp.get_json()["message"]
        assert self._cabinet()["机位"] == "1-20"
        assert self._pending_count() == 0
        assert self._approved_count() == 1
        assert self._version_count() == 1

    def test_admin_delete_takes_effect_immediately(self, app_env, admin):
        self._existing_cabinet()
        client = app_env["app"].test_client()
        _login(client)
        resp = client.post("/api/cabinets/delete", json={"机房": "机房A", "机柜": "C1"})
        assert resp.status_code == 200
        assert "直接生效" in resp.get_json()["message"]
        assert self._cabinet() is None
        assert self._pending_count() == 0
        assert self._approved_count() == 1

    def test_admin_delete_batch_takes_effect_immediately(self, app_env, admin):
        for cabinet, slot in (("C1", "1-10"), ("C2", "11-20")):
            add_official("cabinets", {
                "机房": "机房A", "机柜": cabinet, "机位": slot,
                "管理ip网段": "10.1.1.*", "交易ip网段": "10.1.2.*", "带外ip网段": "10.1.3.*",
            })
        client = app_env["app"].test_client()
        _login(client)
        resp = client.post("/api/cabinets/delete-batch", json={
            "records": [{"机房": "机房A", "机柜": "C1"}, {"机房": "机房A", "机柜": "C2"}],
        })
        assert resp.status_code == 200
        assert "直接生效" in resp.get_json()["message"]
        assert self._cabinet("机房A|C1") is None
        assert self._cabinet("机房A|C2") is None
        assert self._pending_count() == 0

    def test_admin_import_takes_effect_immediately(self, app_env, admin):
        self._existing_cabinet()
        client = app_env["app"].test_client()
        _login(client)
        csv_text = "机房,机柜,机位,瓦数(kw),费用(年),管理ip网段,交易ip网段,带外ip网段\n机房A,C1,1-20,5,1000,10.1.1.*,10.1.2.*,10.1.3.*\n"
        resp = client.post(
            "/api/cabinets/import",
            data={"file": (io.BytesIO(csv_text.encode("utf-8-sig")), "t.csv"), "mode": "full"},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200
        assert "直接生效" in resp.get_json()["message"]
        assert self._cabinet()["机位"] == "1-20"
        assert self._pending_count() == 0

    def test_admin_update_server_takes_effect_immediately(self, app_env, admin):
        """servers 直改：覆盖客户id派生、机位/IP 校验路径。"""
        add_official("customers", {"客户id": "1", "名称": "客户一"})
        add_official("cabinets", {
            "机房": "机房A", "机柜": "C1", "机位": "1-10",
            "管理ip网段": "10.1.1.*", "交易ip网段": "10.1.2.*", "带外ip网段": "10.1.3.*",
        })
        add_official("servers", {
            "序列号": "SRV-1", "客户id": "1", "所在机房": "机房A", "所属机柜": "C1", "所属机位": "1-2",
            "管理ip": "10.1.1.5", "交易ip": "10.1.2.5", "带外ip": "10.1.3.5", "用途": "旧用途",
        })
        client = app_env["app"].test_client()
        _login(client)
        resp = client.post("/api/servers/update", json={
            "old": {"序列号": "SRV-1"},
            "new": {
                "序列号": "SRV-1", "客户id": "1", "所在机房": "机房A", "所属机柜": "C1", "所属机位": "1-2",
                "管理ip": "10.1.1.5", "交易ip": "10.1.2.5", "带外ip": "10.1.3.5", "用途": "新用途",
            },
        })
        assert resp.status_code == 200
        assert "直接生效" in resp.get_json()["message"]
        server = next(r for r in storage.read_records("servers") if r["序列号"] == "SRV-1")
        assert server["用途"] == "新用途"
        assert server["客户名称"] == "客户一"
        assert self._pending_count("servers") == 0
        assert self._version_count("servers") == 1

    def test_room_user_still_requires_review(self, app_env, room_user):
        """对照组：机房人员的变更仍进入待审批，正式表不变。"""
        self._existing_cabinet()
        client = app_env["app"].test_client()
        _login_as(client, "room1")
        resp = client.post("/api/cabinets/update", json={
            "old": {"机房": "机房A", "机柜": "C1", "机位": "1-10"},
            "new": {
                "机房": "机房A", "机柜": "C1", "机位": "1-20", "瓦数(kw)": "5", "费用(年)": "1000",
                "管理ip网段": "10.1.1.*", "交易ip网段": "10.1.2.*", "带外ip网段": "10.1.3.*",
            },
        })
        assert resp.status_code == 200
        assert "已提交" in resp.get_json()["message"]
        assert self._cabinet()["机位"] == "1-10"
        assert self._pending_count() == 1
