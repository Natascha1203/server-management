"""mcp_server/install_config.py 的行为测试。

重点是「不破坏用户既有配置」：这两个文件（尤其 ~/.claude.json）里有别的 mcpServers
和 onboarding 状态，写坏了很难恢复。所以覆盖：保留其他键、幂等、先备份、坏文件不覆盖。
"""
import json
import sys

import pytest

from mcp_server import install_config as installer


@pytest.fixture
def home(tmp_path, monkeypatch):
    """把 Path.home() 指向临时目录，避免碰到真实的 ~/.claude.json。"""
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(installer.Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def run(host, token="tok-1", base="http://127.0.0.1:18888", dry_run=False):
    argv = ["--host", host, "--base-url", base, "--token", token,
            "--python", sys.executable]
    if dry_run:
        argv.append("--dry-run")
    return installer.main(argv)


class TestClaude:
    def target(self, home):
        return home / ".claude.json"

    def test_creates_file_when_missing(self, home):
        assert run("claude") == 0
        data = json.loads(self.target(home).read_text(encoding="utf-8"))
        entry = data["mcpServers"]["server-management"]
        assert entry["type"] == "stdio"
        assert entry["command"] == sys.executable
        assert entry["args"][0].endswith("run_server.py")
        assert entry["env"]["SM_BASE_URL"] == "http://127.0.0.1:18888"
        assert entry["env"]["SM_TOKEN"] == "tok-1"

    def test_dry_run_does_not_touch_disk(self, home):
        assert run("claude", dry_run=True) == 0
        assert not self.target(home).exists()

    def test_preserves_unrelated_keys_and_servers(self, home):
        self.target(home).write_text(json.dumps({
            "numStartups": 42,
            "mcpServers": {"other": {"command": "other-bin", "args": []}},
        }), encoding="utf-8")
        run("claude")
        data = json.loads(self.target(home).read_text(encoding="utf-8"))
        assert data["numStartups"] == 42
        assert data["mcpServers"]["other"]["command"] == "other-bin"
        assert "server-management" in data["mcpServers"]

    def test_idempotent_and_updates_token(self, home):
        run("claude", token="tok-1")
        run("claude", token="tok-2")
        data = json.loads(self.target(home).read_text(encoding="utf-8"))
        assert list(data["mcpServers"]) == ["server-management"]
        assert data["mcpServers"]["server-management"]["env"]["SM_TOKEN"] == "tok-2"

    def test_backup_created_on_overwrite(self, home):
        self.target(home).write_text('{"mcpServers": {}}', encoding="utf-8")
        run("claude")
        backups = list(home.glob(".claude.json.bak-*"))
        assert len(backups) == 1
        assert json.loads(backups[0].read_text(encoding="utf-8")) == {"mcpServers": {}}

    def test_corrupt_file_is_not_overwritten(self, home):
        self.target(home).write_text("{ this is not json", encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            run("claude")
        assert "无法读取" in str(exc.value)
        assert self.target(home).read_text(encoding="utf-8") == "{ this is not json"

    def test_trailing_slash_in_base_url(self, home):
        run("claude", base="http://host:18888/")
        data = json.loads(self.target(home).read_text(encoding="utf-8"))
        assert data["mcpServers"]["server-management"]["env"]["SM_BASE_URL"] == "http://host:18888"


class TestWorkBuddy:
    def target(self, home):
        return home / ".workbuddy" / "mcp.json"

    def test_writes_to_workbuddy_path(self, home):
        assert run("workbuddy") == 0
        data = json.loads(self.target(home).read_text(encoding="utf-8"))
        assert "server-management" in data["mcpServers"]

    def test_does_not_touch_connector_market_config(self, home):
        """内置市场配置在 connectors/default/mcp.json，不能被这个脚本改写。"""
        market = home / ".workbuddy" / "connectors" / "default" / "mcp.json"
        market.parent.mkdir(parents=True)
        market.write_text('{"mcpServers": {"connector:x": {"url": "https://x"}}}', encoding="utf-8")
        run("workbuddy")
        assert "connector:x" in json.loads(market.read_text(encoding="utf-8"))["mcpServers"]
        assert "server-management" not in market.read_text(encoding="utf-8")


class TestBoth:
    def test_both_hosts_get_same_entry(self, home):
        assert run("both") == 0
        claude = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
        wb = json.loads((home / ".workbuddy" / "mcp.json").read_text(encoding="utf-8"))
        assert claude["mcpServers"]["server-management"] == wb["mcpServers"]["server-management"]


class TestCodeBuddy:
    def target(self, home):
        return home / ".codebuddy" / ".mcp.json"

    def test_writes_to_codebuddy_path(self, home):
        assert run("codebuddy") == 0
        data = json.loads(self.target(home).read_text(encoding="utf-8"))
        entry = data["mcpServers"]["server-management"]
        assert entry["type"] == "stdio"
        assert entry["args"][0].endswith("run_server.py")
        assert entry["env"]["SM_BASE_URL"] == "http://127.0.0.1:18888"

    def test_does_not_touch_deprecated_paths(self, home):
        """CodeBuddy 还有 ~/.codebuddy/mcp.json（弃用）与 ~/.codebuddy.json（legacy），只写推荐路径。"""
        run("codebuddy")
        assert not (home / ".codebuddy" / "mcp.json").exists()
        assert not (home / ".codebuddy.json").exists()


class TestAll:
    def test_all_three_hosts_get_same_entry(self, home):
        assert run("all") == 0
        entries = []
        for rel in (".claude.json", ".workbuddy/mcp.json", ".codebuddy/.mcp.json"):
            data = json.loads((home / rel).read_text(encoding="utf-8"))
            entries.append(data["mcpServers"]["server-management"])
        assert entries[0] == entries[1] == entries[2]


class TestValidation:
    def test_bad_python_path(self, home):
        with pytest.raises(SystemExit) as exc:
            installer.main(["--host", "claude", "--base-url", "http://x", "--token", "t",
                            "--python", str(home / "nope" / "python.exe")])
        assert "python 路径不存在" in str(exc.value)
