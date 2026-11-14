"""服务器表新增「备注」列：

1. 上下架请求里填写的「备注」会随「机位IP分配 → 具体实施」落库到服务器记录
2. 机位IP分配 / 具体实施表单可覆盖「备注」（server_data 优先）
3. 服务器记录的增/改/查询/导出都带「备注」列
4. 表头口径：servers 共 27 列，末列为「备注」，且上下架请求表头本身已含「备注」
"""
from app import services, storage
from app.core import ENTITIES, RACK_REQUEST_HEADERS, normalize_row


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


def _make_request(serial="SRV-REMARK", remark="请求备注：双路供电，需 A/B 两路 PDU"):
    data = services.normalize_rack_request({
        "序列号": serial, "请求类型": "上架", "是否重装系统": "是",
        "客户id": "1", "所在机房": "机房A", "备注": remark,
    })
    with storage.db() as conn:
        request_id = services.insert_rack_request(conn, data, {"用户id": "admin", "身份": "管理人员"})
    return request_id


class TestRemarkHeaders:
    def test_servers_headers_end_with_remark(self):
        headers = ENTITIES["servers"]["headers"]
        assert len(headers) == 27
        assert headers[-1] == "备注"
        assert len(set(headers)) == len(headers)

    def test_rack_request_headers_already_have_remark(self):
        assert "备注" in RACK_REQUEST_HEADERS

    def test_normalize_row_keeps_remark(self):
        row = normalize_row("servers", {"序列号": "S1", "备注": "  机房冷通道  "})
        assert row["备注"] == "机房冷通道"


class TestRemarkFromRackRequest:
    def test_remark_flows_through_allocation_and_implementation(self, app_env, admin):
        client = _setup(app_env)
        _login_as(client, "admin")
        request_id = _make_request(remark="上下架备注：需接入行情")

        # 阶段一：机位IP分配（不显式传备注，应沿用请求里的备注）
        resp = client.post(f"/api/rack-requests/{request_id}/review", json={
            "action": "approve",
            "server_data": {
                "客户id": "1", "所在机房": "机房A", "所属机柜": "C1", "所属机位": "3-4",
                "管理ip": "10.1.1.5", "带外ip": "10.1.3.5",
            },
        })
        assert resp.status_code == 200, resp.get_json()
        # 分配完成后 stage_data 已带上备注
        with storage.db() as conn:
            row = conn.execute("SELECT stage_data FROM rack_requests WHERE id=?", (request_id,)).fetchone()
        assert services.parse_json_dict(row["stage_data"])["备注"] == "上下架备注：需接入行情"

        # 阶段二：具体实施（同样不显式传备注）
        resp = client.post(f"/api/rack-requests/{request_id}/review", json={"action": "approve"})
        assert resp.status_code == 200, resp.get_json()

        server = next(x for x in storage.read_records("servers") if x["序列号"] == "SRV-REMARK")
        assert server["备注"] == "上下架备注：需接入行情"

    def test_allocation_override_wins(self, app_env, admin):
        client = _setup(app_env)
        _login_as(client, "admin")
        request_id = _make_request(remark="请求原始备注")

        resp = client.post(f"/api/rack-requests/{request_id}/review", json={
            "action": "approve",
            "server_data": {
                "客户id": "1", "所在机房": "机房A", "所属机柜": "C1", "所属机位": "5-5",
                "管理ip": "10.1.1.6", "带外ip": "10.1.3.6",
                "备注": "分配阶段改写：单路供电",
            },
        })
        assert resp.status_code == 200, resp.get_json()
        resp = client.post(f"/api/rack-requests/{request_id}/review", json={"action": "approve"})
        assert resp.status_code == 200, resp.get_json()

        server = next(x for x in storage.read_records("servers") if x["序列号"] == "SRV-REMARK")
        assert server["备注"] == "分配阶段改写：单路供电"

    def test_empty_remark_stays_empty(self, app_env, admin):
        client = _setup(app_env)
        _login_as(client, "admin")
        request_id = _make_request(remark="")
        client.post(f"/api/rack-requests/{request_id}/review", json={
            "action": "approve",
            "server_data": {
                "客户id": "1", "所在机房": "机房A", "所属机柜": "C1", "所属机位": "6-6",
                "管理ip": "10.1.1.7", "带外ip": "10.1.3.7",
            },
        })
        client.post(f"/api/rack-requests/{request_id}/review", json={"action": "approve"})
        server = next(x for x in storage.read_records("servers") if x["序列号"] == "SRV-REMARK")
        assert server["备注"] == ""


class TestRemarkOnServerApi:
    def test_search_returns_remark_column(self, app_env, admin):
        client = _setup(app_env)
        _login_as(client, "admin")
        add_official("servers", {
            "序列号": "S-1", "客户id": "1", "客户名称": "客户一",
            "所在机房": "机房A", "所属机柜": "C1", "所属机位": "1-1",
            "备注": "手工录入备注",
        })
        resp = client.post("/api/servers/search", json={})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["meta"]["headers"][-1] == "备注"
        record = next(x for x in data["records"] if x["序列号"] == "S-1")
        assert record["备注"] == "手工录入备注"

    def test_add_and_update_carry_remark(self, app_env, admin):
        client = _setup(app_env)
        _login_as(client, "admin")
        resp = client.post("/api/servers/add", json={
            "序列号": "S-2", "客户id": "1", "客户名称": "客户一",
            "所在机房": "机房A", "所属机柜": "C1", "所属机位": "2-2",
            "管理ip": "10.1.1.8", "备注": "新增时备注",
        })
        assert resp.status_code == 200, resp.get_json()
        server = next(x for x in storage.read_records("servers") if x["序列号"] == "S-2")
        assert server["备注"] == "新增时备注"

        resp = client.post("/api/servers/update", json={
            "old": server,
            "new": {**server, "备注": "修改后备注"},
        })
        assert resp.status_code == 200, resp.get_json()
        server = next(x for x in storage.read_records("servers") if x["序列号"] == "S-2")
        assert server["备注"] == "修改后备注"

    def test_remark_is_filterable(self, app_env, admin):
        client = _setup(app_env)
        _login_as(client, "admin")
        add_official("servers", {"序列号": "S-3", "所在机房": "机房A", "所属机柜": "C1", "所属机位": "3", "备注": "液冷机房专用"})
        add_official("servers", {"序列号": "S-4", "所在机房": "机房A", "所属机柜": "C1", "所属机位": "4", "备注": "普通"})
        resp = client.post("/api/servers/search", json={"filters": [{"field": "备注", "pattern": "液冷"}]})
        assert resp.status_code == 200, resp.get_json()
        serials = [x["序列号"] for x in resp.get_json()["records"]]
        assert serials == ["S-3"]

    def test_multiline_remark_and_whitelist_round_trip(self, app_env, admin):
        """「备注」「防火墙白名单」为可调整大小的多行编辑框，换行内容须原样落库并可读回。"""
        client = _setup(app_env)
        _login_as(client, "admin")
        remark = "第1行：机柜 U 位贴标\n第2行：双路 PDU 供电\n第3行：需接入行情"
        whitelist = "10.0.0.0/8\n192.168.1.0/24\n172.16.0.0/12"
        resp = client.post("/api/servers/add", json={
            "序列号": "S-ML", "客户id": "1", "客户名称": "客户一",
            "所在机房": "机房A", "所属机柜": "C1", "所属机位": "5-5",
            "防火墙白名单": whitelist, "备注": remark,
        })
        assert resp.status_code == 200, resp.get_json()
        server = next(x for x in storage.read_records("servers") if x["序列号"] == "S-ML")
        assert server["备注"] == remark
        assert server["防火墙白名单"] == whitelist

        # 经 HTTP 查询读回，换行与行序保持不变
        resp = client.post("/api/servers/search", json={"filters": [{"field": "序列号", "pattern": "S-ML"}]})
        assert resp.status_code == 200, resp.get_json()
        record = next(x for x in resp.get_json()["records"] if x["序列号"] == "S-ML")
        assert record["备注"] == remark
        assert record["防火墙白名单"] == whitelist
        # 多行内容仍可按其中一行做过滤
        resp = client.post("/api/servers/search", json={"filters": [{"field": "防火墙白名单", "pattern": "192.168"}]})
        assert [x["序列号"] for x in resp.get_json()["records"]] == ["S-ML"]
