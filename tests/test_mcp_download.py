"""「下载 MCP」按钮的后端测试。

覆盖：
1. 打包内容 = 磁盘上的 mcp_server/（不预生成、不发旧包）
2. 排除 __pycache__ / *.pyc
3. 顶层目录固定为 server_management_mcp/，解压后 args 路径才可预测
4. 版本号（X-MCP-Version）随文件内容变化
5. 未登录 401、目录不存在 404
"""
import io
import json
import zipfile
from pathlib import Path

import pytest

from app import mcp_package

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MCP_SRC = PROJECT_ROOT / "mcp_server"


def _login(client, username="admin", password="pw"):
    resp = client.post("/login", data={"username": username, "password": password})
    assert resp.status_code == 302


def _download(client):
    resp = client.get("/api/mcp/download")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp, zipfile.ZipFile(io.BytesIO(resp.data))


class TestDownloadEndpoint:
    def test_requires_login(self, app_env):
        client = app_env["app"].test_client()
        assert client.get("/api/mcp/download").status_code == 401

    def test_returns_zip_attachment(self, app_env):
        client = app_env["app"].test_client()
        _login(client)
        resp, archive = _download(client)
        assert resp.mimetype == "application/zip"
        assert "server_management_mcp.zip" in resp.headers["Content-Disposition"]
        assert archive.testzip() is None

    def test_all_files_under_single_root_dir(self, app_env):
        client = app_env["app"].test_client()
        _login(client)
        _, archive = _download(client)
        names = archive.namelist()
        assert names
        assert all(name.startswith("server_management_mcp/") for name in names), names

    def test_contains_entry_points_and_readme(self, app_env):
        client = app_env["app"].test_client()
        _login(client)
        _, archive = _download(client)
        names = set(archive.namelist())
        for expected in (
            "server_management_mcp/run_server.py",
            "server_management_mcp/install_config.py",
            "server_management_mcp/README.md",
            "server_management_mcp/sm_mcp/__main__.py",
            "server_management_mcp/sm_mcp/server.py",
            "server_management_mcp/sm_mcp/tools.py",
        ):
            assert expected in names, expected

    def test_excludes_pycache_and_pyc(self, app_env):
        # 造一个脏文件，确认真实目录里的缓存也不会被带出去
        cache_dir = MCP_SRC / "sm_mcp" / "__pycache__"
        cache_dir.mkdir(exist_ok=True)
        junk = cache_dir / "_download_test.pyc"
        junk.write_bytes(b"junk")
        try:
            client = app_env["app"].test_client()
            _login(client)
            _, archive = _download(client)
            names = archive.namelist()
            assert not any("__pycache__" in name for name in names), names
            assert not any(name.endswith(".pyc") for name in names), names
        finally:
            junk.unlink(missing_ok=True)

    def test_content_matches_disk(self, app_env):
        """包里的 tools.py 必须与磁盘完全一致——这就是「不发旧包」的保证。"""
        client = app_env["app"].test_client()
        _login(client)
        _, archive = _download(client)
        packed = archive.read("server_management_mcp/sm_mcp/tools.py")
        assert packed == (MCP_SRC / "sm_mcp" / "tools.py").read_bytes()

    def test_version_header_is_stable_and_matches_builder(self, app_env):
        client = app_env["app"].test_client()
        _login(client)
        first = client.get("/api/mcp/download")
        second = client.get("/api/mcp/download")
        version = first.headers["X-MCP-Version"]
        assert len(version) == 12 and all(c in "0123456789abcdef" for c in version)
        assert second.headers["X-MCP-Version"] == version
        assert version == mcp_package.package_version(MCP_SRC)

    def test_missing_dir_returns_404(self, tmp_path):
        from app import create_app

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "users.json").write_text(
            json.dumps([{"用户id": "admin", "密码": "pw", "身份": "管理人员"}], ensure_ascii=False),
            encoding="utf-8",
        )
        config_path = tmp_path / "app.ini"
        config_path.write_text(
            f"[app]\ndata_dir = {data_dir}\nsecret_key = test\nmcp_dir = {tmp_path / 'nope'}\n",
            encoding="utf-8",
        )
        client = create_app(config_path).test_client()
        _login(client)
        resp = client.get("/api/mcp/download")
        assert resp.status_code == 404
        assert "mcp_dir" in resp.get_json()["error"]


class TestBuilder:
    def test_version_changes_when_file_changes(self, tmp_path):
        root = tmp_path / "mcp_server"
        (root / "sm_mcp").mkdir(parents=True)
        (root / "run_server.py").write_text("print(1)\n", encoding="utf-8")
        (root / "sm_mcp" / "__init__.py").write_text("v1\n", encoding="utf-8")
        before = mcp_package.package_version(root)
        (root / "sm_mcp" / "__init__.py").write_text("v2\n", encoding="utf-8")
        after = mcp_package.package_version(root)
        assert before != after

    def test_version_ignores_mtime(self, tmp_path):
        """同样内容重新写一遍（mtime 变）版本号不能变，否则无法比较两台机器。"""
        root = tmp_path / "mcp_server"
        root.mkdir()
        target = root / "run_server.py"
        target.write_text("print(1)\n", encoding="utf-8")
        before = mcp_package.package_version(root)
        target.write_text("print(1)\n", encoding="utf-8")
        assert mcp_package.package_version(root) == before

    def test_empty_dir_raises(self, tmp_path):
        root = tmp_path / "empty"
        root.mkdir()
        with pytest.raises(FileNotFoundError):
            mcp_package.build_package(root)

    def test_mcp_dir_defaults_to_project_root(self):
        assert mcp_package.mcp_dir({}) == PROJECT_ROOT / "mcp_server"
        assert mcp_package.mcp_dir({"base_dir": "/tmp/x"}).as_posix() == "/tmp/x/mcp_server"

    def test_mcp_dir_config_overrides(self):
        assert mcp_package.mcp_dir({"base_dir": "/tmp/x", "mcp_dir": "/opt/mcp"}).as_posix() == "/opt/mcp"
