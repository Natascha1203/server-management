import json
import shutil
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def app_env(tmp_path):
    """构造一个使用临时 data 目录的完整应用环境。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "users.json").write_text(
        json.dumps([
            {"用户id": "admin", "密码": "pw", "身份": "管理人员"},
            {"用户id": "room1", "密码": "pw", "身份": "机房人员", "所属机房": "机房A"},
            {"用户id": "biz1", "密码": "pw", "身份": "业务人员", "业务单位": "单位A"},
        ], ensure_ascii=False),
        encoding="utf-8",
    )
    # /api/skill/* 从 base_dir 读文档，把仓库里的真实 skill 复制进来，
    # 这样测试覆盖的是真文档与真探测顺序，而不是桩内容。
    skill_src = PROJECT_ROOT / "server_management_skill"
    if skill_src.is_dir():
        shutil.copytree(skill_src, tmp_path / "server_management_skill",
                        ignore=shutil.ignore_patterns("__pycache__"))
    config_path = tmp_path / "app.ini"
    # mcp_dir 指向仓库里真实的 mcp_server/：/api/mcp/download 的测试打包的是真代码，
    # 断言「包内文件与磁盘一致」才有意义（和上面复制真实 skill 同一个思路）。
    mcp_src = (PROJECT_ROOT / "mcp_server").as_posix()
    config_path.write_text(
        f"[app]\ndata_dir = {data_dir}\nsecret_key = test\nmax_versions = 2\nmcp_dir = {mcp_src}\n",
        encoding="utf-8",
    )
    from app import create_app

    app = create_app(config_path)
    yield {"app": app, "data_dir": data_dir}


@pytest.fixture
def admin(app_env):
    return {"用户id": "admin", "密码": "pw", "身份": "管理人员", "业务单位": "", "所属机房": ""}


@pytest.fixture
def room_user(app_env):
    return {"用户id": "room1", "密码": "pw", "身份": "机房人员", "业务单位": "", "所属机房": "机房A"}


@pytest.fixture
def biz_user(app_env):
    return {"用户id": "biz1", "密码": "pw", "身份": "业务人员", "业务单位": "单位A", "所属机房": ""}
