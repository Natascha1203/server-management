"""版本快照裁剪与孤儿目录清扫的回归测试。"""
from app import storage


def _version_codes():
    with storage.db() as conn:
        return {row["version_code"] for row in conn.execute("SELECT version_code FROM versions")}


def _snapshot_dirs():
    vdir = storage.STATE["versions_dir"]
    return {p.name for p in vdir.iterdir() if p.is_dir()}


def _make_versions(count):
    with storage.db() as conn:
        for i in range(count):
            storage.create_version_record(conn, f"c{i}", "servers", "新增", f"k{i}", "admin")


def test_prune_keeps_records_and_dirs_in_sync(app_env):
    """裁剪后：库记录数 == 磁盘快照目录数 == max_versions，且一一对应。"""
    _make_versions(5)

    with storage.db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0]

    assert count == 2
    assert len(_snapshot_dirs()) == 2
    assert _snapshot_dirs() == _version_codes()


def test_prune_caps_records_when_snapshot_path_stale(app_env):
    """存在指向 versions 目录外的历史记录时，记录仍被裁剪（只丢记录，不越界删目录）。"""
    with storage.db() as conn:
        conn.execute(
            """INSERT INTO versions
               (version_code,change_id,entity,operation,record_key,reviewer,created_at,snapshot_dir)
               VALUES (?,?,?,?,?,?,?,?)""",
            ("V-stale", "c0", "servers", "新增", "k0", "admin", "2026-01-01 00:00:00", "/tmp/elsewhere/V-stale"),
        )
    _make_versions(3)

    assert len(_version_codes()) == 2
    assert "V-stale" not in _version_codes()


def test_sweep_removes_orphan_dirs_only(app_env):
    """孤儿目录被清除，仍被记录引用的快照保留。"""
    _make_versions(2)
    vdir = storage.STATE["versions_dir"]
    orphan = vdir / "V19990101-000000-9999"
    orphan.mkdir()
    (orphan / "客户表.xlsx").write_text("x", encoding="utf-8")

    removed = storage.sweep_orphan_snapshots()

    assert removed >= 1
    assert not orphan.exists()
    for code in _version_codes():
        assert (vdir / code).is_dir()


def test_startup_sweep_cleans_legacy_orphans(app_env):
    """重新初始化（等价于重启）会自动清扫历史遗留的孤儿快照目录。"""
    vdir = storage.STATE["versions_dir"]
    orphan = vdir / "V19990101-000000-0001"
    orphan.mkdir()

    storage.init_storage()

    assert not orphan.exists()
