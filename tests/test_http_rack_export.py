"""上下架请求导出接口测试：按选中条目导出 xlsx、包含机位/IP 分配结果、权限与可见性过滤。"""
import io
import json

from openpyxl import load_workbook

from app import services, storage
from app.core import RACK_REQUEST_EXPORT_HEADERS, normalize_row


def add_official(entity, row):
    records = storage.read_records(entity)
    records.append(normalize_row(entity, row))
    storage.write_records(entity, records)


def _login(client, username="admin"):
    resp = client.post("/login", data={"username": username, "password": "pw"})
    assert resp.status_code == 302


def _make_request(serial, room="机房A", stage="allocation", stage_data=None, submitter="admin"):
    data = services.normalize_rack_request({
        "序列号": serial, "请求类型": "上架", "是否重装系统": "是",
        "客户id": "1", "所在机房": room,
    })
    with storage.db() as conn:
        request_id = services.insert_rack_request(conn, data, {"用户id": submitter, "身份": "管理人员"})
        if stage != "allocation":
            conn.execute(
                "UPDATE rack_requests SET stage=?,stage_data=? WHERE id=?",
                (stage, json.dumps(stage_data or {}, ensure_ascii=False), request_id),
            )
    return request_id


def _setup(app_env):
    add_official("customers", {"客户id": "1", "名称": "客户一"})
    add_official("cabinets", {
        "机房": "机房A", "机柜": "C1", "机位": "1-10",
        "管理ip网段": "10.1.1.*", "交易ip网段": "10.1.2.*", "带外ip网段": "10.1.3.*",
    })


def _read_xlsx(resp):
    wb = load_workbook(io.BytesIO(resp.data), data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    return rows


class TestRackRequestExport:
    def test_export_by_ids_returns_xlsx_with_stage_data(self, app_env, admin):
        _setup(app_env)
        r1 = _make_request("SRV-1", stage="implementation", stage_data={
            "客户id": "1", "所在机房": "机房A", "所属机柜": "C1", "所属机位": "3-4",
            "管理ip": "10.1.1.5", "交易ip": "10.1.2.5", "带外ip": "10.1.3.5",
        })
        _make_request("SRV-2")  # 未选中的另一条

        client = app_env["app"].test_client()
        _login(client)
        resp = client.post("/api/rack-requests/export", json={
            "ids": [r1], "status": "pending", "stage": "implementation", "scope": "review",
        })
        assert resp.status_code == 200
        assert resp.mimetype == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        rows = _read_xlsx(resp)
        assert list(rows[0]) == RACK_REQUEST_EXPORT_HEADERS
        serial_index = RACK_REQUEST_EXPORT_HEADERS.index("序列号")
        slot_index = RACK_REQUEST_EXPORT_HEADERS.index("所属机位")
        mgmt_index = RACK_REQUEST_EXPORT_HEADERS.index("管理ip")
        body = rows[1:]
        assert len(body) == 1
        assert body[0][serial_index] == "SRV-1"
        assert body[0][slot_index] == "3-4"
        assert body[0][mgmt_index] == "10.1.1.5"

    def test_export_without_ids_exports_all_visible(self, app_env, admin):
        _setup(app_env)
        _make_request("SRV-1", stage="implementation", stage_data={"所属机位": "3-4"})
        _make_request("SRV-2", stage="implementation", stage_data={"所属机位": "5-6"})

        client = app_env["app"].test_client()
        _login(client)
        resp = client.post("/api/rack-requests/export", json={
            "status": "pending", "stage": "implementation", "scope": "review",
        })
        assert resp.status_code == 200
        rows = _read_xlsx(resp)
        assert len(rows) - 1 == 2

    def test_export_stage_filter_excludes_other_stage(self, app_env, admin):
        _setup(app_env)
        _make_request("SRV-A")  # allocation 阶段
        _make_request("SRV-B", stage="implementation", stage_data={"所属机位": "3-4"})

        client = app_env["app"].test_client()
        _login(client)
        resp = client.post("/api/rack-requests/export", json={
            "status": "pending", "stage": "allocation", "scope": "review",
        })
        assert resp.status_code == 200
        rows = _read_xlsx(resp)
        serial_index = RACK_REQUEST_EXPORT_HEADERS.index("序列号")
        serials = {row[serial_index] for row in rows[1:]}
        assert serials == {"SRV-A"}

    def test_room_user_only_exports_own_room(self, app_env, admin):
        _setup(app_env)
        add_official("cabinets", {"机房": "机房B", "机柜": "C9", "机位": "1-5"})
        _make_request("SRV-A", room="机房A", stage="implementation", stage_data={"所属机位": "3-4"})
        _make_request("SRV-B", room="机房B", stage="implementation", stage_data={"所属机位": "2-3"})

        client = app_env["app"].test_client()
        _login(client, "room1")  # 所属机房=机房A
        resp = client.post("/api/rack-requests/export", json={
            "status": "pending", "stage": "implementation", "scope": "review",
        })
        assert resp.status_code == 200
        rows = _read_xlsx(resp)
        serial_index = RACK_REQUEST_EXPORT_HEADERS.index("序列号")
        serials = {row[serial_index] for row in rows[1:]}
        assert serials == {"SRV-A"}

    def test_empty_ids_and_no_records_returns_400(self, app_env, admin):
        _setup(app_env)
        client = app_env["app"].test_client()
        _login(client)
        resp = client.post("/api/rack-requests/export", json={
            "ids": [], "status": "pending", "stage": "implementation", "scope": "review",
        })
        assert resp.status_code == 400
