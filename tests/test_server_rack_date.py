"""「上架时间」字段在上下架请求里的自动填充与改写：

1. 提交上下架请求后落库时，`上架时间` 自动等于请求提交日期（请求表头里没有该字段，无需填写）
2. 机位IP分配阶段可以改日期，改后写入 stage_data 并最终落库
3. 具体实施阶段可以再改，以最后一次提交值为准
4. 日期统一归一成 YYYY-MM-DD；表单清空则回落到提交日期
5. 手工增/改服务器记录也能带该字段，并可参与筛选
"""
from app import services, storage
from app.core import ENTITIES, RACK_REQUEST_HEADERS, date_text, normalize_row


def add_official(entity, row):
    records = storage.read_records(entity)
    records.append(normalize_row(entity, row))
    storage.write_records(entity, records)


def _login_as(client, username, password="pw"):
    resp = client.post("/login", data={"username": username, "password": password})
    assert resp.status_code == 302


def _setup(app_env):
    add_official("customers", {"客户id": "1", "名称": "客户一"})
    add_official("cabinets", {
        "机房": "机房A", "机柜": "C1", "机位": "1-10",
        "管理ip网段": "10.1.1.*", "交易ip网段": "10.1.2.*", "带外ip网段": "10.1.3.*",
    })
    return app_env["app"].test_client()


def _make_request(serial="SRV-RACKDATE", submitted_at=None):
    data = services.normalize_rack_request({
        "序列号": serial, "请求类型": "上架", "是否重装系统": "是",
        "客户id": "1", "所在机房": "机房A",
    })
    with storage.db() as conn:
        request_id = services.insert_rack_request(
            conn, data, {"用户id": "admin", "身份": "管理人员"}, submitted_at=submitted_at,
        )
    return request_id


def _server(serial="SRV-RACKDATE"):
    return next(x for x in storage.read_records("servers") if x["序列号"] == serial)


def _stage_data(request_id):
    with storage.db() as conn:
        row = conn.execute("SELECT stage_data FROM rack_requests WHERE id=?", (request_id,)).fetchone()
    return services.parse_json_dict(row["stage_data"])


class TestRackDateField:
    def test_field_already_in_headers(self):
        """复用既有「上架时间」列，不新增字段。"""
        headers = ENTITIES["servers"]["headers"]
        assert len(headers) == 27
        assert headers[headers.index("所属机位") + 1] == "上架时间"

    def test_request_headers_do_not_have_rack_time(self):
        """上下架请求表头没有「上架时间」：它由后端按提交日期自动填，请求导入契约不变。"""
        assert "上架时间" not in RACK_REQUEST_HEADERS

    def test_date_text_normalizes_and_falls_back(self):
        assert date_text("2026-10-29 21:40:00") == "2026-10-29"
        assert date_text("2026/9/1") == "2026-09-01"
        assert date_text("2026.10.9") == "2026-10-09"
        assert date_text("") == ""
        assert date_text("不是日期", "2026-01-02") == "2026-01-02"


class TestRackDateFromRackRequest:
    def test_allocation_fills_submitted_date(self, app_env, admin):
        """分配阶段不填日期时，自动取请求提交日期。"""
        client = _setup(app_env)
        _login_as(client, "admin")
        request_id = _make_request(submitted_at="2026-10-29 21:40:00")

        resp = client.post(f"/api/rack-requests/{request_id}/review", json={
            "action": "approve",
            "server_data": {
                "客户id": "1", "所在机房": "机房A", "所属机柜": "C1", "所属机位": "3-4",
                "管理ip": "10.1.1.5", "带外ip": "10.1.3.5",
            },
        })
        assert resp.status_code == 200, resp.get_json()
        assert _stage_data(request_id)["上架时间"] == "2026-10-29"

        resp = client.post(f"/api/rack-requests/{request_id}/review", json={"action": "approve"})
        assert resp.status_code == 200, resp.get_json()
        assert _server()["上架时间"] == "2026-10-29"

    def test_allocation_can_override_date(self, app_env, admin):
        client = _setup(app_env)
        _login_as(client, "admin")
        request_id = _make_request(submitted_at="2026-10-29 21:40:00")

        resp = client.post(f"/api/rack-requests/{request_id}/review", json={
            "action": "approve",
            "server_data": {
                "客户id": "1", "所在机房": "机房A", "所属机柜": "C1", "所属机位": "5-5",
                "管理ip": "10.1.1.6", "带外ip": "10.1.3.6",
                "上架时间": "2026-09-01",
            },
        })
        assert resp.status_code == 200, resp.get_json()
        assert _stage_data(request_id)["上架时间"] == "2026-09-01"

        resp = client.post(f"/api/rack-requests/{request_id}/review", json={"action": "approve"})
        assert resp.status_code == 200, resp.get_json()
        assert _server()["上架时间"] == "2026-09-01"

    def test_implementation_can_change_date_again(self, app_env, admin):
        client = _setup(app_env)
        _login_as(client, "admin")
        request_id = _make_request(submitted_at="2026-10-29 21:40:00")

        client.post(f"/api/rack-requests/{request_id}/review", json={
            "action": "approve",
            "server_data": {
                "客户id": "1", "所在机房": "机房A", "所属机柜": "C1", "所属机位": "6-6",
                "管理ip": "10.1.1.7", "带外ip": "10.1.3.7",
                "上架时间": "2026-09-01",
            },
        })
        # 具体实施阶段再改一次，最后一次提交值生效
        resp = client.post(f"/api/rack-requests/{request_id}/review", json={
            "action": "approve",
            "server_data": {"上架时间": "2026-10-08"},
        })
        assert resp.status_code == 200, resp.get_json()
        assert _server()["上架时间"] == "2026-10-08"

    def test_implementation_keeps_allocation_date_when_not_touched(self, app_env, admin):
        client = _setup(app_env)
        _login_as(client, "admin")
        request_id = _make_request(submitted_at="2026-10-29 21:40:00")
        client.post(f"/api/rack-requests/{request_id}/review", json={
            "action": "approve",
            "server_data": {
                "客户id": "1", "所在机房": "机房A", "所属机柜": "C1", "所属机位": "7-7",
                "管理ip": "10.1.1.8", "带外ip": "10.1.3.8",
                "上架时间": "2026-09-01",
            },
        })
        resp = client.post(f"/api/rack-requests/{request_id}/review", json={"action": "approve"})
        assert resp.status_code == 200, resp.get_json()
        assert _server()["上架时间"] == "2026-09-01"

    def test_slash_date_is_normalized(self, app_env, admin):
        client = _setup(app_env)
        _login_as(client, "admin")
        request_id = _make_request(submitted_at="2026-10-29 21:40:00")
        client.post(f"/api/rack-requests/{request_id}/review", json={
            "action": "approve",
            "server_data": {
                "客户id": "1", "所在机房": "机房A", "所属机柜": "C1", "所属机位": "8-8",
                "管理ip": "10.1.1.9", "带外ip": "10.1.3.9",
                "上架时间": "2026/9/1",
            },
        })
        client.post(f"/api/rack-requests/{request_id}/review", json={"action": "approve"})
        assert _server()["上架时间"] == "2026-09-01"

    def test_empty_date_falls_back_to_submitted_date(self, app_env, admin):
        """表单被清空时不让上架时间变成空，回落到请求提交日期。"""
        client = _setup(app_env)
        _login_as(client, "admin")
        request_id = _make_request(submitted_at="2026-10-29 21:40:00")
        client.post(f"/api/rack-requests/{request_id}/review", json={
            "action": "approve",
            "server_data": {
                "客户id": "1", "所在机房": "机房A", "所属机柜": "C1", "所属机位": "9-9",
                "管理ip": "10.1.1.10", "带外ip": "10.1.3.10",
                "上架时间": "",
            },
        })
        client.post(f"/api/rack-requests/{request_id}/review", json={"action": "approve"})
        assert _server()["上架时间"] == "2026-10-29"


class TestRackDateOnServerApi:
    def test_search_returns_rack_time(self, app_env, admin):
        client = _setup(app_env)
        _login_as(client, "admin")
        add_official("servers", {
            "序列号": "S-1", "客户id": "1", "客户名称": "客户一",
            "所在机房": "机房A", "所属机柜": "C1", "所属机位": "1-1",
            "上架时间": "2026-09-01",
        })
        resp = client.post("/api/servers/search", json={})
        assert resp.status_code == 200
        record = next(x for x in resp.get_json()["records"] if x["序列号"] == "S-1")
        assert record["上架时间"] == "2026-09-01"

    def test_add_and_update_carry_rack_time(self, app_env, admin):
        client = _setup(app_env)
        _login_as(client, "admin")
        resp = client.post("/api/servers/add", json={
            "序列号": "S-2", "客户id": "1", "客户名称": "客户一",
            "所在机房": "机房A", "所属机柜": "C1", "所属机位": "2-2",
            "管理ip": "10.1.1.8", "上架时间": "2026-09-30",
        })
        assert resp.status_code == 200, resp.get_json()
        server = next(x for x in storage.read_records("servers") if x["序列号"] == "S-2")
        assert server["上架时间"] == "2026-09-30"

        resp = client.post("/api/servers/update", json={
            "old": server,
            "new": {**server, "上架时间": "2026-10-20"},
        })
        assert resp.status_code == 200, resp.get_json()
        server = next(x for x in storage.read_records("servers") if x["序列号"] == "S-2")
        assert server["上架时间"] == "2026-10-20"

    def test_rack_time_is_filterable(self, app_env, admin):
        client = _setup(app_env)
        _login_as(client, "admin")
        add_official("servers", {"序列号": "S-3", "所在机房": "机房A", "所属机柜": "C1", "所属机位": "3-3", "上架时间": "2026-10-01"})
        add_official("servers", {"序列号": "S-4", "所在机房": "机房A", "所属机柜": "C1", "所属机位": "4-4", "上架时间": "2026-09-01"})
        resp = client.post("/api/servers/search", json={"filters": [{"field": "上架时间", "pattern": "2026-10"}]})
        assert resp.status_code == 200, resp.get_json()
        assert [x["序列号"] for x in resp.get_json()["records"]] == ["S-3"]
