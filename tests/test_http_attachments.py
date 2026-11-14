"""服务器资料（附件）的 HTTP 全链路测试：

1. 上传落在 data/archive/<序列号>/ 下，可多个文件一次上传
2. 列表 / 下载 / 重命名 / 删除四个动作闭环
3. 按序列号归属：上下架请求、机位IP分配/具体实施、服务器表看到同一份档案
4. 权限：业务人员只对自己提交的请求/本单位的服务器有权限，机房人员限本机房
5. 导出的 Excel 不携带资料列
"""
import io
import json

from openpyxl import load_workbook

from app import services, storage
from app.core import ENTITIES, normalize_row


def add_official(entity, row):
    """绕过审批直接写正式表（测试铺底数据用）。"""
    records = storage.read_records(entity)
    records.append(normalize_row(entity, row))
    storage.write_records(entity, records)


def _login(client, username="admin", password="pw"):
    resp = client.post("/login", data={"username": username, "password": password})
    assert resp.status_code == 302


def _setup(app_env):
    add_official("customers", {"客户id": "1", "名称": "客户一"})
    add_official("cabinets", {
        "机房": "机房A", "机柜": "C1", "机位": "1-10",
        "管理ip网段": "10.1.1.*", "交易ip网段": "10.1.2.*", "带外ip网段": "10.1.3.*",
    })
    add_official("cabinets", {"机房": "机房B", "机柜": "C1", "机位": "1-10"})
    add_official("servers", {
        "序列号": "SN-1", "客户id": "1", "客户名称": "客户一", "归属单位": "单位A",
        "所在机房": "机房A", "所属机柜": "C1", "所属机位": "1-1",
    })
    add_official("servers", {
        "序列号": "SN-2", "客户id": "1", "客户名称": "客户一", "归属单位": "单位B",
        "所在机房": "机房B", "所属机柜": "C1", "所属机位": "1-1",
    })
    return app_env["app"].test_client()


def _upload(client, serial, files, request_id=""):
    """files = [(内容bytes, 文件名), ...]"""
    data = {"serial": serial, "files": [(io.BytesIO(content), name) for content, name in files]}
    if request_id:
        data["request_id"] = request_id
    return client.post("/api/attachments", data=data, content_type="multipart/form-data")


def _archive_files(app_env):
    archive = app_env["data_dir"] / "archive"
    return sorted(path.name for path in archive.rglob("*") if path.is_file())


class TestUploadAndArchive:
    def test_upload_multiple_into_archive_dir(self, app_env, admin):
        client = _setup(app_env)
        _login(client)
        resp = _upload(client, "SN-1", [(b"hello", "说明.txt"), (b"pdf-bytes", "交付单.pdf")])
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body["message"] == "已上传 2 个资料"
        assert len(body["items"]) == 2

        folder = app_env["data_dir"] / "archive" / "SN-1"
        assert folder.is_dir()
        names = sorted(path.name for path in folder.iterdir())
        assert len(names) == 2
        # 落盘名 = <附件id>_<原文件名>，id 随机，因此只校验后缀
        assert sorted(name.split("_", 1)[1] for name in names) == ["交付单.pdf", "说明.txt"]
        assert all(len(name.split("_", 1)[0]) == 12 for name in names)

        listing = client.get("/api/attachments?serial=SN-1").get_json()["items"]
        assert {item["filename"] for item in listing} == {"说明.txt", "交付单.pdf"}
        assert {item["size"] for item in listing} == {5, 9}
        assert all(item["serial_number"] == "SN-1" and item["can_manage"] for item in listing)

    def test_upload_requires_serial_and_file(self, app_env, admin):
        client = _setup(app_env)
        _login(client)
        assert client.post("/api/attachments", data={"serial": "SN-1"},
                           content_type="multipart/form-data").status_code == 400
        assert _upload(client, "", [(b"x", "a.txt")]).status_code == 400

    def test_upload_rejects_unknown_serial_for_business(self, app_env):
        client = _setup(app_env)
        _login(client, "biz1")
        resp = _upload(client, "SN-2", [(b"x", "a.txt")])
        assert resp.status_code == 403

    def test_filename_is_sanitized(self, app_env, admin):
        client = _setup(app_env)
        _login(client)
        resp = _upload(client, "SN-1", [(b"x", "../../etc/passwd")])
        assert resp.status_code == 200
        item = resp.get_json()["items"][0]
        assert "/" not in item["filename"] and ".." not in item["filename"]
        folder = app_env["data_dir"] / "archive" / "SN-1"
        assert len(list(folder.iterdir())) == 1


class TestDownloadRenameDelete:
    def test_download_returns_original_name_and_content(self, app_env, admin):
        client = _setup(app_env)
        _login(client)
        item = _upload(client, "SN-1", [(b"file-body", "巡检记录.txt")]).get_json()["items"][0]
        resp = client.get(f"/api/attachments/{item['id']}/download")
        assert resp.status_code == 200
        assert resp.data == b"file-body"
        assert "attachment" in resp.headers["Content-Disposition"]

    def test_rename_keeps_download_working(self, app_env, admin):
        client = _setup(app_env)
        _login(client)
        item = _upload(client, "SN-1", [(b"v1", "旧名.txt")]).get_json()["items"][0]
        resp = client.post(f"/api/attachments/{item['id']}/rename", json={"filename": "新名.txt"})
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["attachment"]["filename"] == "新名.txt"
        names = _archive_files(app_env)
        assert len(names) == 1 and names[0].endswith("_新名.txt")
        assert client.get(f"/api/attachments/{item['id']}/download").data == b"v1"

    def test_delete_removes_row_and_file(self, app_env, admin):
        client = _setup(app_env)
        _login(client)
        item = _upload(client, "SN-1", [(b"v1", "待删.txt")]).get_json()["items"][0]
        assert client.post(f"/api/attachments/{item['id']}/delete").status_code == 200
        assert _archive_files(app_env) == []
        assert client.get("/api/attachments?serial=SN-1").get_json()["items"] == []
        assert client.get(f"/api/attachments/{item['id']}/download").status_code == 404

    def test_delete_unknown_attachment(self, app_env, admin):
        client = _setup(app_env)
        _login(client)
        assert client.post("/api/attachments/not-exist/delete").status_code == 404


class TestSerialOwnership:
    def test_attachments_follow_serial_into_server_table(self, app_env, admin):
        """上架请求 → 机位IP分配 → 具体实施 落库后，服务器表拿到同一份资料。"""
        client = _setup(app_env)
        _login(client)
        data = services.normalize_rack_request({
            "序列号": "SN-NEW", "请求类型": "上架", "是否重装系统": "是",
            "客户id": "1", "所在机房": "机房A",
        })
        with storage.db() as conn:
            request_id = services.insert_rack_request(conn, data, admin)
        item = _upload(client, "SN-NEW", [(b"spec", "验收报告.txt")], request_id=request_id).get_json()["items"][0]
        assert item["request_id"] == request_id

        # 机位IP分配 + 具体实施
        server_data = {
            "客户id": "1", "所在机房": "机房A", "所属机柜": "C1", "所属机位": "5-6",
            "管理ip": "10.1.1.5", "交易ip": "10.1.2.5", "带外ip": "10.1.3.5",
        }
        assert client.post(f"/api/rack-requests/{request_id}/review",
                           json={"action": "approve", "server_data": server_data}).status_code == 200
        assert client.post(f"/api/rack-requests/{request_id}/review",
                           json={"action": "approve", "server_data": server_data}).status_code == 200

        # 服务器表接口拿到该序列号后即可取资料
        servers = client.get("/api/servers").get_json()["records"]
        assert any(row["序列号"] == "SN-NEW" for row in servers)
        items = client.get("/api/attachments?serial=SN-NEW").get_json()["items"]
        assert [x["filename"] for x in items] == ["验收报告.txt"]
        counts = client.get("/api/attachments/summary").get_json()["counts"]
        assert counts.get("SN-NEW") == 1

    def test_business_can_manage_own_request_attachments_only(self, app_env):
        client = _setup(app_env)
        _login(client, "biz1")
        created = client.post("/api/rack-requests", json={
            "序列号": "SN-BIZ", "请求类型": "上架", "是否重装系统": "是",
            "客户id": "1", "所在机房": "机房A",
        })
        assert created.status_code == 200, created.get_json()
        request_id = created.get_json()["request_id"]
        resp = _upload(client, "SN-BIZ", [(b"x", "需求说明.txt")], request_id=request_id)
        assert resp.status_code == 200, resp.get_json()

        # 自己可见
        assert len(client.get("/api/attachments?serial=SN-BIZ").get_json()["items"]) == 1
        # 别人（本单位但非提交人）的序列号不可见
        assert client.get("/api/attachments?serial=SN-2").get_json()["items"] == []
        assert len(client.get("/api/attachments").get_json()["items"]) == 1

    def test_room_user_limited_to_own_room(self, app_env):
        client = _setup(app_env)
        _login(client, "room1")
        assert _upload(client, "SN-1", [(b"a", "本机房.txt")]).status_code == 200
        assert _upload(client, "SN-2", [(b"a", "别的机房.txt")]).status_code == 403
        assert client.get("/api/attachments?serial=SN-2").get_json()["items"] == []

    def test_room_user_can_delete_others_upload_in_same_room(self, app_env, admin):
        admin_client = _setup(app_env)
        _login(admin_client)
        item = _upload(admin_client, "SN-1", [(b"a", "机房资料.txt")]).get_json()["items"][0]
        room_client = app_env["app"].test_client()
        _login(room_client, "room1")
        assert room_client.post(f"/api/attachments/{item['id']}/delete").status_code == 200


class TestExportUnaffected:
    def test_server_export_has_no_attachment_column(self, app_env, admin):
        client = _setup(app_env)
        _login(client)
        _upload(client, "SN-1", [(b"a", "资料.txt")])
        resp = client.post("/api/servers/export?format=xlsx", json={})
        assert resp.status_code == 200
        wb = load_workbook(io.BytesIO(resp.data), data_only=True)
        headers = [cell.value for cell in next(wb.active.iter_rows(max_row=1))]
        wb.close()
        # 只比数据列（首列是「在途状态」）
        assert headers[1:] == ENTITIES["servers"]["headers"]
        assert not any("资料" in str(item) or "附件" in str(item) for item in headers)

    def test_rack_request_export_has_no_attachment_column(self, app_env, admin):
        client = _setup(app_env)
        _login(client)
        data = services.normalize_rack_request({
            "序列号": "SN-EXP", "请求类型": "上架", "是否重装系统": "是",
            "客户id": "1", "所在机房": "机房A",
        })
        with storage.db() as conn:
            request_id = services.insert_rack_request(conn, data, admin)
        _upload(client, "SN-EXP", [(b"a", "资料.txt")], request_id=request_id)
        resp = client.post("/api/rack-requests/export", json={"status": "pending"})
        assert resp.status_code == 200
        wb = load_workbook(io.BytesIO(resp.data), data_only=True)
        headers = [cell.value for cell in next(wb.active.iter_rows(max_row=1))]
        wb.close()
        assert not any("资料" in str(item) or "附件" in str(item) for item in headers)
