"""快照目录扫描与全量恢复的回归测试。

覆盖：
1. 快照根目录可配置（设置 snapshot_dir），手动备份写到 root/manual/
2. 扫描能识别 cron 的 zip、manual/B*、旧版根目录 B*
3. 恢复用快照覆盖三张正式表，且恢复前自动生成一份可撤回的备份
4. 表头不兼容时中止，正式表不受影响
5. app.db 可选；不恢复 users.json
6. 恢复后 pending 申请作废（可关掉）
7. 权限：非管理人员 403；必须手输 RESTORE 确认
"""
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from app import snapshots, storage
from app.core import ENTITIES, normalize_row


def login(client, username="admin", password="pw"):
    resp = client.post("/login", data={"username": username, "password": password})
    assert resp.status_code == 302


def add_official(entity, row):
    records = storage.read_records(entity)
    records.append(normalize_row(entity, row))
    storage.write_records(entity, records)


def seed_servers(serial="SRV-1", brand="Dell"):
    add_official("customers", {"客户id": "1", "名称": "客户一"})
    add_official("cabinets", {
        "机房": "机房A", "机柜": "C1", "机位": "1-10",
        "管理ip网段": "10.1.1.*", "交易ip网段": "10.1.2.*", "带外ip网段": "10.1.3.*",
    })
    add_official("servers", {
        "序列号": serial, "客户id": "1", "所在机房": "机房A", "所属机柜": "C1",
        "所属机位": "3-4", "管理ip": "10.1.1.5/24", "带外ip": "10.1.3.5/24", "品牌": brand,
    })


def make_cron_zip(path, data_dir):
    """模拟 scripts/backup.sh 的产物：data/official/*.xlsx + data/app.db。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for meta in ENTITIES.values():
            archive.write(data_dir / "official" / meta["filename"], f"data/official/{meta['filename']}")
        if (data_dir / "app.db").exists():
            archive.write(data_dir / "app.db", "data/app.db")
        archive.write(data_dir / "users.json", "data/users.json")


def set_snapshot_dir(client, path):
    resp = client.put("/api/settings", json={"snapshot_dir": str(path)})
    assert resp.status_code == 200
    return resp


class TestSnapshotRoot:
    def test_defaults_to_data_backups(self, app_env, admin):
        assert storage.snapshot_root() == app_env["data_dir"] / "backups"
        assert storage.manual_backup_dir() == app_env["data_dir"] / "backups" / "manual"

    def test_settings_can_point_elsewhere(self, app_env, admin, tmp_path):
        client = app_env["app"].test_client()
        login(client)
        target = tmp_path / "ext-backups"
        set_snapshot_dir(client, target)
        assert storage.snapshot_root() == target.resolve()
        assert storage.manual_backup_dir() == target.resolve() / "manual"

    def test_relative_snapshot_dir_resolves_under_base_dir(self, app_env, admin):
        client = app_env["app"].test_client()
        login(client)
        resp = client.put("/api/settings", json={"snapshot_dir": "backup-area"})
        assert resp.status_code == 200
        assert storage.snapshot_root() == Path(storage.STATE["config"]["base_dir"]) / "backup-area"

    def test_rejects_unusable_path(self, app_env, admin, tmp_path):
        client = app_env["app"].test_client()
        login(client)
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("x", encoding="utf-8")
        resp = client.put("/api/settings", json={"snapshot_dir": str(blocker / "sub")})
        assert resp.status_code == 400
        assert "快照目录" in resp.get_json()["error"]

    def test_manual_backup_goes_into_manual_subdir(self, app_env, admin, tmp_path):
        client = app_env["app"].test_client()
        login(client)
        target = tmp_path / "ext-backups"
        set_snapshot_dir(client, target)
        resp = client.post("/api/backups/run")
        assert resp.status_code == 200
        created = list((target / "manual").glob("B*"))
        assert len(created) == 1
        assert (created[0] / "服务器信息表.xlsx").is_file()


class TestSnapshotScan:
    def test_lists_zip_and_manual_and_legacy(self, app_env, admin):
        client = app_env["app"].test_client()
        login(client)
        data_dir = app_env["data_dir"]
        root = storage.snapshot_root()
        root.mkdir(parents=True, exist_ok=True)
        make_cron_zip(root / "data_20261103.zip", data_dir)
        storage.create_backup("manual")
        legacy = root / "B20260101-000000-0001"
        legacy.mkdir()
        for meta in ENTITIES.values():
            (legacy / meta["filename"]).write_bytes((data_dir / "official" / meta["filename"]).read_bytes())

        payload = client.get("/api/snapshots").get_json()
        names = {item["name"] for item in payload["items"]}
        assert "data_20261103.zip" in names
        assert any(name.startswith("manual/B") for name in names)
        assert "B20260101-000000-0001" in names
        origins = {item["name"]: item["origin"] for item in payload["items"]}
        assert origins["data_20261103.zip"] == "cron"
        assert origins["B20260101-000000-0001"] == "legacy"
        assert all(item["valid"] for item in payload["items"])

    def test_flags_incomplete_zip(self, app_env, admin):
        client = app_env["app"].test_client()
        login(client)
        root = storage.snapshot_root()
        root.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(root / "data_20260101.zip", "w") as archive:
            archive.writestr("data/official/客户表.xlsx", b"x")
        item = next(i for i in client.get("/api/snapshots").get_json()["items"]
                    if i["name"] == "data_20260101.zip")
        assert item["valid"] is False
        assert "缺少" in item["errors"][0]

    def test_ignores_unrelated_files(self, app_env, admin):
        client = app_env["app"].test_client()
        login(client)
        root = storage.snapshot_root()
        root.mkdir(parents=True, exist_ok=True)
        (root / "notes.txt").write_text("hello", encoding="utf-8")
        (root / "random.zip").write_text("not a zip", encoding="utf-8")
        payload = client.get("/api/snapshots").get_json()
        # 无关的 txt 不列出；无关 zip 会列出但标为不可用（readable=False）
        assert all(item["name"] != "notes.txt" for item in payload["items"])
        bad = next(item for item in payload["items"] if item["name"] == "random.zip")
        assert bad["valid"] is False

    def test_requires_admin(self, app_env, biz_user):
        client = app_env["app"].test_client()
        login(client, "biz1")
        assert client.get("/api/snapshots").status_code == 403


class TestSnapshotRestore:
    def _prepare(self, app_env, client, tmp_path, brand_at_snapshot="HPE"):
        """做一份快照（含当时的 app.db），随后把正式表改成别的值。"""
        data_dir = app_env["data_dir"]
        seed_servers("SRV-1", brand_at_snapshot)
        root = tmp_path / "ext-backups"
        set_snapshot_dir(client, root)
        assert client.post("/api/backups/run").status_code == 200
        snapshot_name = next(
            item["name"] for item in client.get("/api/snapshots").get_json()["items"]
            if item["kind"] == "dir"
        )
        # 快照之后的改动：换品牌 + 多一台服务器
        records = storage.read_records("servers")
        records[0]["品牌"] = "Changed"
        storage.write_records("servers", records)
        seed_servers("SRV-2", "Lenovo")
        return snapshot_name

    def test_restore_overwrites_tables_and_keeps_undo(self, app_env, admin, tmp_path):
        client = app_env["app"].test_client()
        login(client)
        name = self._prepare(app_env, client, tmp_path)
        assert {row["序列号"] for row in storage.read_records("servers")} == {"SRV-1", "SRV-2"}

        resp = client.post("/api/snapshots/restore",
                           json={"snapshot": name, "confirm": "RESTORE"})
        assert resp.status_code == 200, resp.get_json()
        payload = resp.get_json()
        servers = storage.read_records("servers")
        assert [row["序列号"] for row in servers] == ["SRV-1"]
        assert servers[0]["品牌"] == "HPE"
        # 恢复前自动备份，名字能对上，且目录真实存在
        undo = payload["backup_before_restore"]
        assert undo.startswith("B")
        assert (storage.manual_backup_dir() / undo).is_dir()
        # 撤销备份里是恢复前的状态（两台机器）
        undo_file = storage.manual_backup_dir() / undo / "服务器信息表.xlsx"
        assert undo_file.is_file()

    def test_restore_rejects_without_confirm(self, app_env, admin, tmp_path):
        client = app_env["app"].test_client()
        login(client)
        name = self._prepare(app_env, client, tmp_path)
        resp = client.post("/api/snapshots/restore", json={"snapshot": name})
        assert resp.status_code == 400
        assert "确认" in resp.get_json()["error"]
        assert len(storage.read_records("servers")) == 2

    def test_restore_unknown_snapshot(self, app_env, admin):
        client = app_env["app"].test_client()
        login(client)
        resp = client.post("/api/snapshots/restore",
                           json={"snapshot": "../../etc/passwd", "confirm": "RESTORE"})
        assert resp.status_code == 400
        assert "不存在" in resp.get_json()["error"]

    def test_restore_aborts_on_missing_columns(self, app_env, admin, tmp_path):
        """快照缺列时中止，正式表原样保留。"""
        from openpyxl import load_workbook

        client = app_env["app"].test_client()
        login(client)
        seed_servers("SRV-1", "Dell")
        root = tmp_path / "ext-backups"
        set_snapshot_dir(client, root)
        assert client.post("/api/backups/run").status_code == 200
        name = next(item["name"] for item in client.get("/api/snapshots").get_json()["items"]
                    if item["kind"] == "dir")

        # 偷偷把快照里服务器表的某一列删掉
        snap_file = root / "manual" / Path(name).name / "服务器信息表.xlsx"
        wb = load_workbook(snap_file)
        ws = wb[wb.sheetnames[0]]
        headers = [cell.value for cell in ws[1]]
        target_col = headers.index("品牌") + 1
        ws.delete_cols(target_col)
        wb.save(snap_file)
        wb.close()

        records = storage.read_records("servers")
        records[0]["品牌"] = "Changed"
        storage.write_records("servers", records)

        resp = client.post("/api/snapshots/restore", json={"snapshot": name, "confirm": "RESTORE"})
        assert resp.status_code == 400
        assert "缺列" in resp.get_json()["error"]
        assert storage.read_records("servers")[0]["品牌"] == "Changed"

    def test_restore_does_not_touch_users_json(self, app_env, admin, tmp_path):
        client = app_env["app"].test_client()
        login(client)
        name = self._prepare(app_env, client, tmp_path)
        before = (app_env["data_dir"] / "users.json").read_text(encoding="utf-8")
        assert client.post("/api/snapshots/restore",
                           json={"snapshot": name, "confirm": "RESTORE"}).status_code == 200
        assert (app_env["data_dir"] / "users.json").read_text(encoding="utf-8") == before

    def test_zip_snapshot_restores_tables(self, app_env, admin, tmp_path):
        """cron 那种 data/official/ 结构的 zip 也能恢复。"""
        client = app_env["app"].test_client()
        login(client)
        data_dir = app_env["data_dir"]
        root = tmp_path / "zip-backups"
        set_snapshot_dir(client, root)
        seed_servers("SRV-ZIP", "HPE")
        make_cron_zip(root / "data_20261101.zip", data_dir)

        records = storage.read_records("servers")
        records[0]["品牌"] = "Changed"
        storage.write_records("servers", records)

        resp = client.post("/api/snapshots/restore",
                           json={"snapshot": "data_20261101.zip", "confirm": "RESTORE"})
        assert resp.status_code == 200, resp.get_json()
        assert storage.read_records("servers")[0]["品牌"] == "HPE"

    def test_requires_admin(self, app_env, admin, biz_user, tmp_path):
        client = app_env["app"].test_client()
        login(client)
        name = self._prepare(app_env, client, tmp_path)
        other = app_env["app"].test_client()
        login(other, "biz1")
        resp = other.post("/api/snapshots/restore", json={"snapshot": name, "confirm": "RESTORE"})
        assert resp.status_code == 403


class TestRestoreDatabase:
    def _seed_pending(self, app_env):
        from app import services

        with storage.db() as conn:
            conn.execute(
                "INSERT INTO changes (id,entity,operation,record_key,new_data,status,submitter,submitted_at)"
                " VALUES ('c1','servers','新增','SRV-P','{}','pending','biz1','2026-11-01 10:00:00')"
            )
            conn.execute(
                "INSERT INTO rack_requests (id,serial_number,request_type,request_data,status,"
                "submitter_id,submitted_at,stage) VALUES ('r1','SRV-Q','上架','{}','pending','biz1',"
                "'2026-11-01 10:00:00','allocation')"
            )
        return services

    def _pending_counts(self):
        with storage.db() as conn:
            return (
                conn.execute("SELECT COUNT(*) FROM changes WHERE status='pending'").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM rack_requests WHERE status='pending'").fetchone()[0],
            )

    def test_drop_pending_after_restore(self, app_env, admin, tmp_path):
        client = app_env["app"].test_client()
        login(client)
        data_dir = app_env["data_dir"]
        root = tmp_path / "ext-backups"
        set_snapshot_dir(client, root)
        make_cron_zip(root / "data_20261102.zip", data_dir)
        self._seed_pending(app_env)
        assert self._pending_counts() == (1, 1)

        resp = client.post("/api/snapshots/restore", json={
            "snapshot": "data_20261102.zip", "confirm": "RESTORE", "include_db": False,
        })
        assert resp.status_code == 200, resp.get_json()
        assert self._pending_counts() == (0, 0)
        assert resp.get_json()["dropped_pending"] == {"changes": 1, "rack_requests": 1}

    def test_keep_pending_when_disabled(self, app_env, admin, tmp_path):
        client = app_env["app"].test_client()
        login(client)
        data_dir = app_env["data_dir"]
        root = tmp_path / "ext-backups"
        set_snapshot_dir(client, root)
        make_cron_zip(root / "data_20261102.zip", data_dir)
        self._seed_pending(app_env)

        resp = client.post("/api/snapshots/restore", json={
            "snapshot": "data_20261102.zip", "confirm": "RESTORE",
            "include_db": False, "drop_pending": False,
        })
        assert resp.status_code == 200, resp.get_json()
        assert self._pending_counts() == (1, 1)

    def test_include_db_restores_tables(self, app_env, admin, tmp_path):
        """快照里的 app.db 若有回收站记录，恢复后应出现在当前库。"""
        client = app_env["app"].test_client()
        login(client)
        data_dir = app_env["data_dir"]
        root = tmp_path / "ext-backups"

        with storage.db() as conn:
            conn.execute(
                "INSERT INTO trash (id,serial_number,data,request_id,deleted_at,deleted_by,reason,origin)"
                " VALUES ('t-old','SRV-OLD','{}','','2026-11-01 10:00:00','admin','测试','')"
            )
        make_cron_zip(root / "data_20261102.zip", data_dir)
        with storage.db() as conn:
            conn.execute("DELETE FROM trash")

        set_snapshot_dir(client, root)
        resp = client.post("/api/snapshots/restore", json={
            "snapshot": "data_20261102.zip", "confirm": "RESTORE", "include_db": True,
        })
        assert resp.status_code == 200, resp.get_json()
        assert "trash" in resp.get_json()["db_restored"]
        with storage.db() as conn:
            rows = [row["id"] for row in conn.execute("SELECT id FROM trash")]
        assert rows == ["t-old"]

    def test_excluding_db_keeps_current_rows(self, app_env, admin, tmp_path):
        client = app_env["app"].test_client()
        login(client)
        data_dir = app_env["data_dir"]
        root = tmp_path / "ext-backups"
        make_cron_zip(root / "data_20261102.zip", data_dir)
        with storage.db() as conn:
            conn.execute(
                "INSERT INTO trash (id,serial_number,data,request_id,deleted_at,deleted_by,reason,origin)"
                " VALUES ('t-now','SRV-NOW','{}','','2026-11-02 10:00:00','admin','测试','')"
            )
        set_snapshot_dir(client, root)
        resp = client.post("/api/snapshots/restore", json={
            "snapshot": "data_20261102.zip", "confirm": "RESTORE", "include_db": False,
        })
        assert resp.status_code == 200
        assert resp.get_json()["db_restored"] == []
        with storage.db() as conn:
            rows = [row["id"] for row in conn.execute("SELECT id FROM trash")]
        assert rows == ["t-now"]

    def test_settings_and_versions_not_overwritten(self, app_env, admin, tmp_path):
        """app.db 恢复不能把 snapshot_dir 自己回滚掉。"""
        client = app_env["app"].test_client()
        login(client)
        data_dir = app_env["data_dir"]
        root = tmp_path / "ext-backups"
        make_cron_zip(root / "data_20261102.zip", data_dir)
        set_snapshot_dir(client, root)
        resp = client.post("/api/snapshots/restore", json={
            "snapshot": "data_20261102.zip", "confirm": "RESTORE", "include_db": True,
        })
        assert resp.status_code == 200
        assert storage.read_settings()["snapshot_dir"] == str(root)
        assert "settings" not in resp.get_json()["db_restored"]


class TestSnapshotDelete:
    def _prepare(self, client, app_env, tmp_path):
        data_dir = app_env["data_dir"]
        root = tmp_path / "ext-backups"
        set_snapshot_dir(client, root)
        seed_servers("SRV-1")
        make_cron_zip(root / "data_20261103.zip", data_dir)
        assert client.post("/api/backups/run").status_code == 200
        return root

    def test_delete_zip(self, app_env, admin, tmp_path):
        client = app_env["app"].test_client()
        login(client)
        root = self._prepare(client, app_env, tmp_path)
        assert (root / "data_20261103.zip").is_file()

        resp = client.delete("/api/snapshots", json={"snapshot": "data_20261103.zip"})
        assert resp.status_code == 200, resp.get_json()
        assert "已删除" in resp.get_json()["message"]
        assert not (root / "data_20261103.zip").exists()
        names = {item["name"] for item in client.get("/api/snapshots").get_json()["items"]}
        assert "data_20261103.zip" not in names

    def test_delete_manual_dir(self, app_env, admin, tmp_path):
        client = app_env["app"].test_client()
        login(client)
        root = self._prepare(client, app_env, tmp_path)
        name = next(item["name"] for item in client.get("/api/snapshots").get_json()["items"]
                    if item["origin"] == "manual")

        resp = client.delete("/api/snapshots", json={"snapshot": name})
        assert resp.status_code == 200, resp.get_json()
        assert not (root / name).exists()
        assert resp.get_json()["remaining"] == 1

    def test_delete_rejects_path_traversal(self, app_env, admin, tmp_path):
        client = app_env["app"].test_client()
        login(client)
        self._prepare(client, app_env, tmp_path)
        outside = tmp_path / "important.txt"
        outside.write_text("keep me", encoding="utf-8")

        for bad in ("../important.txt", "../../etc/passwd", "data_20261103.zip/../../important.txt"):
            resp = client.delete("/api/snapshots", json={"snapshot": bad})
            assert resp.status_code == 400, bad
        assert outside.is_file()

    def test_delete_unknown_name(self, app_env, admin, tmp_path):
        client = app_env["app"].test_client()
        login(client)
        self._prepare(client, app_env, tmp_path)
        resp = client.delete("/api/snapshots", json={"snapshot": "nope.zip"})
        assert resp.status_code == 400
        assert "不存在" in resp.get_json()["error"]

    def test_delete_requires_admin(self, app_env, admin, biz_user, tmp_path):
        client = app_env["app"].test_client()
        login(client)
        self._prepare(client, app_env, tmp_path)
        other = app_env["app"].test_client()
        login(other, "biz1")
        assert other.delete("/api/snapshots", json={"snapshot": "data_20261103.zip"}).status_code == 403


class TestAutoBackupBeforeWrite:
    HEADER = {"X-SM-Auto-Backup": "1"}

    def _auto_dir(self):
        return storage.auto_backup_dir()

    def test_write_with_header_creates_backup(self, app_env, admin):
        client = app_env["app"].test_client()
        login(client)
        seed_servers("SRV-1")
        resp = client.post("/api/servers/add", json={
            "序列号": "SRV-2", "客户id": "1", "所在机房": "机房A", "所属机柜": "C1",
            "所属机位": "5-6", "管理ip": "10.1.1.20/24", "带外ip": "10.1.3.20/24",
        }, headers=self.HEADER)
        assert resp.status_code == 200, resp.get_json()
        name = resp.headers.get("X-SM-Auto-Backup")
        assert name and name.startswith("B")
        assert (self._auto_dir() / name).is_dir()
        # 备份里是写入前的状态（只有 SRV-1）
        from openpyxl import load_workbook
        wb = load_workbook(self._auto_dir() / name / "服务器信息表.xlsx", data_only=True)
        serials = [row[0] for row in wb[wb.sheetnames[0]].iter_rows(min_row=2, values_only=True)]
        wb.close()
        assert serials == ["SRV-1"]

    def test_read_requests_do_not_backup(self, app_env, admin):
        client = app_env["app"].test_client()
        login(client)
        seed_servers("SRV-1")
        resp = client.get("/api/servers", headers=self.HEADER)
        assert resp.status_code == 200
        assert not resp.headers.get("X-SM-Auto-Backup")
        assert not self._auto_dir().exists() or not list(self._auto_dir().glob("B*"))

    def test_identical_state_is_not_backed_up_twice(self, app_env, admin):
        client = app_env["app"].test_client()
        login(client)
        seed_servers("SRV-1")
        first = client.post("/api/servers/add", json={
            "序列号": "SRV-2", "客户id": "1", "所在机房": "机房A", "所属机柜": "C1",
            "所属机位": "5-6", "管理ip": "10.1.1.20/24", "带外ip": "10.1.3.20/24",
        }, headers=self.HEADER)
        assert first.headers.get("X-SM-Auto-Backup")

        # 同一个请求再发一次：状态没变（会因主键冲突 400），不应再生成备份
        second = client.post("/api/servers/add", json={
            "序列号": "SRV-2", "客户id": "1", "所在机房": "机房A", "所属机柜": "C1",
            "所属机位": "5-6", "管理ip": "10.1.1.20/24", "带外ip": "10.1.3.20/24",
        }, headers=self.HEADER)
        assert second.status_code == 400
        assert not second.headers.get("X-SM-Auto-Backup")
        assert len(list(self._auto_dir().glob("B*"))) == 1

    def test_new_change_triggers_new_backup(self, app_env, admin):
        client = app_env["app"].test_client()
        login(client)
        seed_servers("SRV-1")
        payload = {
            "序列号": "SRV-2", "客户id": "1", "所在机房": "机房A", "所属机柜": "C1",
            "所属机位": "5-6", "管理ip": "10.1.1.20/24", "带外ip": "10.1.3.20/24",
        }
        first = client.post("/api/servers/add", json=payload, headers=self.HEADER)
        assert first.headers.get("X-SM-Auto-Backup")
        third = client.post("/api/servers/add", json={
            "序列号": "SRV-3", "客户id": "1", "所在机房": "机房A", "所属机柜": "C1",
            "所属机位": "7-8", "管理ip": "10.1.1.30/24", "带外ip": "10.1.3.30/24",
        }, headers=self.HEADER)
        assert third.headers.get("X-SM-Auto-Backup")
        assert len(list(self._auto_dir().glob("B*"))) == 2

    def test_auto_backups_scanned_and_labeled(self, app_env, admin):
        client = app_env["app"].test_client()
        login(client)
        seed_servers("SRV-1")
        client.post("/api/servers/add", json={
            "序列号": "SRV-2", "客户id": "1", "所在机房": "机房A", "所属机柜": "C1",
            "所属机位": "5-6", "管理ip": "10.1.1.20/24", "带外ip": "10.1.3.20/24",
        }, headers=self.HEADER)
        items = client.get("/api/snapshots").get_json()["items"]
        assert any(item["origin"] == "auto" for item in items)
        auto_item = next(item for item in items if item["origin"] == "auto")
        assert auto_item["name"].startswith("auto/B")
        assert auto_item["valid"] is True

    def test_auto_backup_does_not_run_for_snapshot_endpoints(self, app_env, admin, tmp_path):
        client = app_env["app"].test_client()
        login(client)
        root = tmp_path / "ext-backups"
        set_snapshot_dir(client, root)
        resp = client.post("/api/snapshots/preview", json={"snapshot": "x"}, headers=self.HEADER)
        assert resp.status_code == 400  # 快照不存在，但不该因此去造自动备份
        assert not (storage.auto_backup_dir() / "x").exists()
        assert not list(storage.auto_backup_dir().glob("B*")) if storage.auto_backup_dir().exists() else True

    def test_failed_backup_blocks_write(self, app_env, admin, tmp_path):
        """快照目录不可写时，带自动备份要求的写入必须中止，而不是悄悄改数据。"""
        client = app_env["app"].test_client()
        login(client)
        seed_servers("SRV-1")
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("x", encoding="utf-8")
        # 直接写 settings 绕过 PUT 的目录预检查，模拟「运行期目录被弄坏」
        storage.write_settings({"snapshot_dir": str(blocker)})

        resp = client.post("/api/servers/add", json={
            "序列号": "SRV-2", "客户id": "1", "所在机房": "机房A", "所属机柜": "C1",
            "所属机位": "5-6", "管理ip": "10.1.1.20/24", "带外ip": "10.1.3.20/24",
        }, headers=self.HEADER)
        assert resp.status_code == 500
        assert "自动备份失败" in resp.get_json()["error"]
        assert {row["序列号"] for row in storage.read_records("servers")} == {"SRV-1"}

    def test_without_header_nothing_happens(self, app_env, admin):
        client = app_env["app"].test_client()
        login(client)
        seed_servers("SRV-1")
        resp = client.post("/api/servers/add", json={
            "序列号": "SRV-2", "客户id": "1", "所在机房": "机房A", "所属机柜": "C1",
            "所属机位": "5-6", "管理ip": "10.1.1.20/24", "带外ip": "10.1.3.20/24",
        })
        assert resp.status_code == 200
        assert not resp.headers.get("X-SM-Auto-Backup")


class TestSnapshotPreview:
    def test_preview_reports_counts_and_compatibility(self, app_env, admin, tmp_path):
        client = app_env["app"].test_client()
        login(client)
        data_dir = app_env["data_dir"]
        root = tmp_path / "ext-backups"
        set_snapshot_dir(client, root)
        seed_servers("SRV-1")
        make_cron_zip(root / "data_20261102.zip", data_dir)

        resp = client.post("/api/snapshots/preview", json={"snapshot": "data_20261102.zip"})
        assert resp.status_code == 200
        detail = resp.get_json()["snapshot"]
        assert detail["restorable"] is True
        counts = {table["entity"]: table["records"] for table in detail["tables"]}
        assert counts["servers"] == 1
        assert detail["has_db"] is True

    def test_preview_unknown_name(self, app_env, admin):
        client = app_env["app"].test_client()
        login(client)
        resp = client.post("/api/snapshots/preview", json={"snapshot": "nope.zip"})
        assert resp.status_code == 400
