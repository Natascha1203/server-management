"""配置加载：app.ini + 环境变量（SM_SECRET_KEY / SM_NOTIFY_WEBHOOK_URL 可覆盖敏感项）。"""
from __future__ import annotations

import os
from configparser import ConfigParser
from pathlib import Path

from .core import ROLE_ADMIN, ROLE_BUSINESS, ROLE_ROOM

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "app.ini"


def legacy_role_name(role):
    return ROLE_ADMIN if role == "admin" else ROLE_BUSINESS


def role_code(role_name):
    return "admin" if role_name == ROLE_ADMIN else "client"


def clean_config_value(value):
    return "" if value is None else str(value).strip()


def normalize_user(raw):
    user_id = clean_config_value(raw.get("用户id") or raw.get("username") or raw.get("user_id"))
    role_name = clean_config_value(raw.get("身份") or raw.get("role_name") or raw.get("role"))
    if role_name in {"admin", "client"}:
        role_name = legacy_role_name(role_name)
    return {
        "用户id": user_id,
        "密码": clean_config_value(raw.get("密码") or raw.get("password")),
        "身份": role_name,
        "业务单位": clean_config_value(raw.get("业务单位") or raw.get("business_unit")),
        "所属机房": clean_config_value(raw.get("所属机房") or raw.get("room")),
        "api_token": clean_config_value(raw.get("api_token") or raw.get("token")),
    }


def load_users_json(data_dir):
    path = data_dir / "users.json"
    if not path.exists():
        return None
    import json

    users_data = json.loads(path.read_text(encoding="utf-8-sig"))
    users = {}
    for item in users_data:
        user = normalize_user(item)
        if not user["用户id"] or not user["密码"]:
            raise RuntimeError("users.json 中用户id和密码不能为空")
        if user["身份"] not in {ROLE_ADMIN, ROLE_ROOM, ROLE_BUSINESS}:
            raise RuntimeError(f"用户 {user['用户id']} 的身份无效：{user['身份']}")
        users[user["用户id"]] = user
    if not users:
        raise RuntimeError("users.json 至少需要一个用户")
    return users


def load_config(config_path=None):
    config_path = Path(config_path) if config_path else CONFIG_PATH
    if not config_path.exists():
        raise RuntimeError(f"配置文件不存在：{config_path}")
    parser = ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(config_path, encoding="utf-8")
    if not parser.has_section("app"):
        raise RuntimeError("配置文件缺少 [app] 节")

    base_dir = config_path.resolve().parent

    data_dir_text = parser.get("app", "data_dir", fallback="").strip()
    if not data_dir_text:
        raise RuntimeError("配置项 app.data_dir 不能为空")

    secret_key = parser.get("app", "secret_key", fallback="").strip() or os.environ.get("SM_SECRET_KEY", "")
    if not secret_key:
        raise RuntimeError("配置项 app.secret_key 不能为空")

    data_dir = Path(data_dir_text)
    if not data_dir.is_absolute():
        data_dir = base_dir / data_dir

    data_dir.mkdir(parents=True, exist_ok=True)
    users = load_users_json(data_dir)
    if users is None:
        if not parser.has_section("users"):
            raise RuntimeError("配置文件缺少 [users] 节或 data/users.json")
        users = {}
        for username, value in parser.items("users"):
            parts = [part.strip() for part in value.split(",", 1)]
            if len(parts) != 2 or not parts[0] or not parts[1]:
                raise RuntimeError(f"用户 {username} 配置格式应为：密码, 角色")
            password, role = parts[0], parts[1].lower()
            if role not in {"admin", "client"}:
                raise RuntimeError(f"用户 {username} 的角色无效：{role}")
            users[username.strip()] = normalize_user({
                "用户id": username.strip(),
                "密码": password,
                "身份": legacy_role_name(role),
            })
    if not users:
        raise RuntimeError("配置文件 [users] 至少需要一个用户")

    max_versions = parser.getint("app", "max_versions", fallback=0)
    backup_keep_text = parser.get("app", "backup_keep_count", fallback=None)
    if backup_keep_text is None or not str(backup_keep_text).strip():
        backup_keep_count = max_versions if max_versions > 0 else 20
    else:
        backup_keep_count = int(str(backup_keep_text).strip())

    notify_webhook_url = parser.get("app", "notify_webhook_url", fallback="").strip() or os.environ.get("SM_NOTIFY_WEBHOOK_URL", "")

    skill_zip_text = parser.get("app", "skill_zip_path", fallback="").strip() or os.environ.get("SM_SKILL_ZIP_PATH", "")
    skill_zip_path = None
    if skill_zip_text:
        skill_zip_path = Path(skill_zip_text)
        if not skill_zip_path.is_absolute():
            skill_zip_path = base_dir / skill_zip_path

    return {
        "base_dir": base_dir,
        "data_dir": data_dir,
        "secret_key": secret_key,
        "max_versions": max_versions,
        "reset": 1 if parser.getboolean("app", "reset", fallback=False) else 0,
        "backup_keep_count": backup_keep_count,
        "backup_command": parser.get("app", "backup_command", fallback="").strip(),
        "audit_keep_count": parser.getint("app", "audit_keep_count", fallback=200),
        "notify_enabled": 1 if parser.getboolean("app", "notify_enabled", fallback=False) else 0,
        "notify_webhook_url": notify_webhook_url,
        "notify_message_type": parser.get("app", "notify_message_type", fallback="markdown").strip() or "markdown",
        "skill_zip_path": skill_zip_path,
        "users": users,
    }
