"""MCP 自更新引导器（mcp_server/run_server.py）的回归测试。

引导器是「旧版用户能自己更新」的唯一机制，所以这里逐条盯住它的边界：
1. 首次启动：版本库为空 → 从后端下载安装，并写 current.txt
2. 已是最新：不重复下载
3. 后端发了新版：拉新包、切换 current.txt、旧版本目录保留（可回滚）
4. 版本检查失败（后端不可达）：继续用现有版本，不阻塞启动
5. SM_MCP_AUTO_UPDATE=0：只提示不下载
6. TTL 内不重复问后端
7. 只保留 N 份历史版本，且永不删当前在用的那份
8. /api/mcp/version 端点本身
"""
import importlib.util
import json
import os
import threading
import zipfile
from pathlib import Path

import pytest

from app import mcp_package, storage

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER_PATH = PROJECT_ROOT / "mcp_server" / "run_server.py"


def load_launcher():
    """按文件路径加载引导器模块（它不是包，用 importlib 直接载入）。"""
    spec = importlib.util.spec_from_file_location("sm_mcp_launcher_under_test", LAUNCHER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def launcher():
    return load_launcher()


@pytest.fixture
def live_backend(app_env):
    from werkzeug.serving import make_server

    server = make_server("127.0.0.1", 0, app_env["app"], threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_port
    finally:
        server.shutdown()
        thread.join(timeout=5)


def admin_token(app_env, user_id="admin"):
    return storage.get_users()[user_id]["api_token"] or storage.generate_user_api_token(user_id)


@pytest.fixture
def env(app_env, live_backend, tmp_path, monkeypatch):
    """把引导器需要的环境变量指到临时版本库 + 真实后端。"""
    home = tmp_path / "mcp-home"
    monkeypatch.setenv("SM_MCP_HOME", str(home))
    monkeypatch.setenv("SM_BASE_URL", live_backend)
    monkeypatch.setenv("SM_TOKEN", admin_token(app_env))
    monkeypatch.delenv("SM_MCP_AUTO_UPDATE", raising=False)
    monkeypatch.setenv("SM_MCP_CHECK_TTL", "0")
    monkeypatch.delenv("SM_MCP_RUNNING_VERSION", raising=False)
    return {"home": home, "base_url": live_backend, "app_env": app_env}


# ---------------------------------------------------------------- 版本端点

class TestVersionEndpoint:
    def test_returns_same_hash_as_download_header(self, app_env):
        client = app_env["app"].test_client()
        client.post("/login", data={"username": "admin", "password": "pw"})
        payload = client.get("/api/mcp/version").get_json()
        assert len(payload["version"]) == 12
        assert payload["files"] > 0
        download = client.get("/api/mcp/download")
        assert download.headers["X-MCP-Version"] == payload["version"]

    def test_requires_login(self, app_env):
        client = app_env["app"].test_client()
        assert client.get("/api/mcp/version").status_code == 401


# ---------------------------------------------------------------- 首次安装

class TestFirstInstall:
    def test_installs_from_backend(self, launcher, env):
        code_dir = launcher.resolve_code_dir()
        home = env["home"]
        assert code_dir.parent == home / "versions"
        assert (code_dir / "sm_mcp" / "__init__.py").is_file()
        version = launcher.read_current(home)
        assert version and code_dir.name == version
        # 环境变量要能被业务代码读到（sm_bootstrap 报版本靠它）
        assert os.environ["SM_MCP_RUNNING_VERSION"] == version

    def test_installed_version_matches_backend(self, launcher, env, app_env):
        launcher.resolve_code_dir()
        client = app_env["app"].test_client()
        client.post("/login", data={"username": "admin", "password": "pw"})
        remote = client.get("/api/mcp/version").get_json()["version"]
        assert launcher.read_current(env["home"]) == remote

    def test_falls_back_to_extracted_dir_when_unreachable(self, launcher, tmp_path, monkeypatch):
        """版本库为空 + 后端不可达：不该抛异常，退回解压目录继续跑。"""
        monkeypatch.setenv("SM_MCP_HOME", str(tmp_path / "mcp-home-empty"))
        monkeypatch.setenv("SM_BASE_URL", "http://127.0.0.1:9")
        monkeypatch.setenv("SM_TOKEN", "x")
        monkeypatch.setenv("SM_MCP_CHECK_TTL", "0")
        code_dir = launcher.resolve_code_dir()
        assert code_dir == launcher.HERE
        assert os.environ["SM_MCP_RUNNING_VERSION"] == ""


# ---------------------------------------------------------------- 版本切换

class TestUpdate:
    def _publish_new_version(self, tmp_path, monkeypatch):
        """模拟后端代码变了：让 mcp_dir 指向一个内容不同的副本。"""
        import shutil

        source = PROJECT_ROOT / "mcp_server"
        target = tmp_path / "mcp_v2"
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (target / "sm_mcp" / "_marker_v2.py").write_text("# v2\n", encoding="utf-8")
        monkeypatch.setitem(storage.STATE["config"], "mcp_dir", str(target))
        return mcp_package.package_version(target)

    def test_picks_up_new_version_and_keeps_old(self, launcher, env, monkeypatch, tmp_path):
        first_dir = launcher.resolve_code_dir()
        first_version = first_dir.name

        want = self._publish_new_version(tmp_path, monkeypatch)
        second_dir = launcher.resolve_code_dir()
        assert second_dir.name == want != first_version
        assert (second_dir / "sm_mcp" / "_marker_v2.py").is_file()
        # 旧版本目录还在 → 把 current.txt 改回去就能回滚
        assert (env["home"] / "versions" / first_version).is_dir()
        assert launcher.read_current(env["home"]) == want

    def test_no_download_when_already_latest(self, launcher, env, monkeypatch):
        launcher.resolve_code_dir()
        calls = []
        original = launcher.download_package

        def spy(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        monkeypatch.setattr(launcher, "download_package", spy)
        launcher.resolve_code_dir()
        assert calls == [], "版本没变不该重新下载"

    def test_broken_package_keeps_old_version(self, launcher, env, monkeypatch):
        """后端给出新版本号但包是坏的：继续用旧版本，不把版本库搞挂。"""
        first = launcher.resolve_code_dir()
        monkeypatch.setattr(launcher, "fetch_remote_version", lambda *a, **k: "deadbeefcafe")
        monkeypatch.setattr(launcher, "download_package", lambda *a, **k: (b"not a zip", "deadbeefcafe"))
        again = launcher.resolve_code_dir()
        assert again == first
        assert launcher.read_current(env["home"]) == first.name

    def test_uninstall_keeps_running_from_extracted(self, launcher, env):
        """current.txt 指向不存在的目录：按未安装处理，重新装好而不是崩溃。"""
        launcher.resolve_code_dir()
        (env["home"] / "current.txt").write_text("doesnotexist\n", encoding="utf-8")
        code_dir = launcher.resolve_code_dir()
        assert (code_dir / "sm_mcp" / "__init__.py").is_file()
        assert launcher.read_current(env["home"]) != "doesnotexist"


class TestUpdateDisabled:
    def test_does_not_download_but_reports(self, launcher, env, monkeypatch, tmp_path):
        import shutil

        monkeypatch.setenv("SM_MCP_AUTO_UPDATE", "0")
        source = PROJECT_ROOT / "mcp_server"
        target = tmp_path / "mcp_v2"
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (target / "sm_mcp" / "_m.py").write_text("# v2\n", encoding="utf-8")
        monkeypatch.setitem(storage.STATE["config"], "mcp_dir", str(target))

        code_dir = launcher.resolve_code_dir()
        # 版本库为空 + 关掉自动更新 → 跑解压目录那份，不写 current.txt
        assert code_dir == launcher.HERE
        assert launcher.read_current(env["home"]) in ("", None)

    def test_existing_version_still_used(self, launcher, env, monkeypatch):
        installed = launcher.resolve_code_dir()
        monkeypatch.setenv("SM_MCP_AUTO_UPDATE", "0")
        monkeypatch.setattr(launcher, "download_package",
                            lambda *a, **k: pytest.fail("关闭自动更新后不该下载"))
        assert launcher.resolve_code_dir() == installed


class TestTtl:
    def test_skips_check_within_ttl(self, launcher, env, monkeypatch):
        launcher.resolve_code_dir()
        monkeypatch.setenv("SM_MCP_CHECK_TTL", "600")
        monkeypatch.setattr(launcher, "fetch_remote_version",
                            lambda *a, **k: pytest.fail("TTL 内不该再问后端"))
        launcher.resolve_code_dir()

    def test_checks_when_ttl_zero(self, launcher, env, monkeypatch):
        launcher.resolve_code_dir()
        monkeypatch.setenv("SM_MCP_CHECK_TTL", "0")
        calls = []
        original = launcher.fetch_remote_version

        def spy(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        monkeypatch.setattr(launcher, "fetch_remote_version", spy)
        launcher.resolve_code_dir()
        assert len(calls) == 1


class TestPrune:
    def test_keeps_newest_and_current(self, launcher, env):
        home = env["home"]
        versions = home / "versions"
        versions.mkdir(parents=True, exist_ok=True)
        for index in range(6):
            path = versions / ("v%02d" % index)
            (path / "sm_mcp").mkdir(parents=True)
            (path / "sm_mcp" / "__init__.py").write_text("", encoding="utf-8")
        launcher.prune_versions(home, current="v00")
        names = sorted(p.name for p in versions.iterdir() if p.is_dir())
        # 保留最新 3 个（v03/v04/v05）+ 正在用的 v00
        assert names == ["v00", "v03", "v04", "v05"]

    def test_keep_env_override(self, launcher, env, monkeypatch):
        home = env["home"]
        versions = home / "versions"
        versions.mkdir(parents=True, exist_ok=True)
        for index in range(4):
            path = versions / ("w%d" % index)
            (path / "sm_mcp").mkdir(parents=True)
            (path / "sm_mcp" / "__init__.py").write_text("", encoding="utf-8")
        monkeypatch.setenv("SM_MCP_KEEP_VERSIONS", "1")
        launcher.prune_versions(home, current="")
        assert len([p for p in versions.iterdir() if p.is_dir()]) == 1


class TestInstallSafety:
    def test_rejects_zip_slip(self, launcher, env, tmp_path):
        import io

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("../../evil.txt", "x")
            archive.writestr("server_management_mcp/sm_mcp/__init__.py", "")
        with pytest.raises(ValueError):
            launcher.install_version(env["home"], "badc0ffee000", buffer.getvalue())
        assert not (tmp_path / "evil.txt").exists()

    def test_rejects_package_without_sm_mcp(self, launcher, env):
        import io

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("server_management_mcp/readme.txt", "x")
        with pytest.raises(ValueError):
            launcher.install_version(env["home"], "aaaabbbbcccc", buffer.getvalue())

    def test_concurrent_install_reuses_existing(self, launcher, env):
        """另一个进程已装好同一版本时，后来的那个直接用现成目录，不报错。"""
        blob, version = launcher.download_package()
        first = launcher.install_version(env["home"], version, blob)
        second = launcher.install_version(env["home"], version, blob)
        assert first == second
        assert (second / "sm_mcp" / "__init__.py").is_file()


class TestBootstrapVersionLine:
    def test_reports_match(self, app_env, live_backend, monkeypatch):
        from mcp_server.sm_mcp import tools as mcp_tools
        from mcp_server.sm_mcp.client import SMClient

        client = SMClient(base_url=live_backend, token=admin_token(app_env))
        remote = client.json("/api/mcp/version")["version"]
        monkeypatch.setenv("SM_MCP_RUNNING_VERSION", remote)
        text = mcp_tools.call_tool("sm_bootstrap", {}, client)
        assert "## 版本" in text
        assert "一致" in text

    def test_reports_stale_client(self, app_env, live_backend, monkeypatch):
        from mcp_server.sm_mcp import tools as mcp_tools
        from mcp_server.sm_mcp.client import SMClient

        client = SMClient(base_url=live_backend, token=admin_token(app_env))
        monkeypatch.setenv("SM_MCP_RUNNING_VERSION", "000000000000")
        text = mcp_tools.call_tool("sm_bootstrap", {}, client)
        assert "客户端偏旧" in text

    def test_reports_unknown_when_run_from_source(self, app_env, live_backend, monkeypatch):
        from mcp_server.sm_mcp import tools as mcp_tools
        from mcp_server.sm_mcp.client import SMClient

        monkeypatch.delenv("SM_MCP_RUNNING_VERSION", raising=False)
        client = SMClient(base_url=live_backend, token=admin_token(app_env))
        text = mcp_tools.call_tool("sm_bootstrap", {}, client)
        assert "未知" in text


class TestSubprocessHandshake:
    """真起进程跑一次 stdio 握手：引导器装完版本后必须能用新代码应答。"""

    def test_stdio_roundtrip_after_install(self, env):
        import subprocess
        import sys

        messages = "\n".join([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                   "clientInfo": {"name": "test", "version": "0"}}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {"name": "sm_bootstrap", "arguments": {}}}),
        ]) + "\n"
        process = subprocess.run(
            [sys.executable, str(LAUNCHER_PATH)],
            input=messages, capture_output=True, text=True, timeout=90,
            env={**os.environ, "SM_MCP_HOME": str(env["home"]), "SM_BASE_URL": env["base_url"],
                 "SM_TOKEN": os.environ["SM_TOKEN"], "SM_MCP_CHECK_TTL": "0", "PYTHONIOENCODING": "utf-8"},
        )
        lines = [line for line in process.stdout.splitlines() if line.strip().startswith("{")]
        assert lines, "没有协议响应；stderr=%s" % process.stderr[-800:]
        replies = [json.loads(line) for line in lines]
        init = next(r for r in replies if r.get("id") == 1)
        assert init["result"]["serverInfo"]["name"] == "server-management"
        tools = next(r for r in replies if r.get("id") == 2)["result"]["tools"]
        names = {tool["name"] for tool in tools}
        assert {"sm_bootstrap", "sm_query", "sm_upsert"} <= names
        # 引导器装完版本后必须把版本号传给业务代码，sm_bootstrap 才能自报版本
        installed = launcher_read_current(env["home"])
        bootstrap = next(r for r in replies if r.get("id") == 3)["result"]["content"][0]["text"]
        assert "## 版本" in bootstrap
        assert installed in bootstrap, bootstrap[-400:]
        # 版本库已经就绪，第二次启动不会再下载
        assert (env["home"] / "versions" / installed).is_dir()


def launcher_read_current(home):
    return (Path(home) / "current.txt").read_text(encoding="utf-8").strip()

