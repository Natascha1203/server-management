"""skill 文档分发：为 /api/skill/{manifest,docs,schema} 三个只读端点组装数据。

设计要点
--------
- **schema 完全由 core 的运行时数据组装，零缓存**。给 ``ENTITIES`` 加一列，重启服务后
  ``/api/skill/schema`` 立刻返回新列——MCP 和 agent 都不用改，skill 也不用重新下载。
- **version 把「文档内容 + schema」一起哈希**，所以字段或文档任一变化都会换版本，
  不会出现「文档没变、结构已漂移」的静默不一致。
- 文档正文直接从仓库文件读取，**不维护第二份副本**。P0 阶段按
  ``docs/`` → ``server_management_skill/`` → ``server_management_skill/references/``
  顺序探测；P3 把参考资料迁到 ``docs/`` 之后就只走第一处。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .core import (
    ENTITIES,
    FIELD_ALIASES,
    MULTI_VALUE_FIELDS,
    RACK_REQUEST_EXPORT_HEADERS,
    RACK_REQUEST_FIELD_ALIASES,
    RACK_REQUEST_HEADERS,
    ROLE_ADMIN,
    ROLE_BUSINESS,
    ROLE_ROOM,
    can_mutate_entity,
    can_view_entity,
    capabilities_for,
)

SKILL_NAME = "server-management"
ROLES = (ROLE_ADMIN, ROLE_ROOM, ROLE_BUSINESS)

# schema 结构版本：仅当返回结构本身变化（增删顶层字段、改变字段语义）时递增。
# 字段增删不需要动它——那由 ENTITIES 驱动，version 哈希会自动变。
SCHEMA_VERSION = 3

# 文档名 → 候选文件名。只允许白名单，天然挡住 ../ 之类路径穿越。
DOC_FILES = {
    "skill": ["SKILL.md"],
    "skill.md": ["SKILL.md"],
    "api-reference": ["api-reference.md"],
    "api-reference.md": ["api-reference.md"],
    "readme": ["README.md"],
    "readme.md": ["README.md"],
}
DEFAULT_DOC = "skill"
# 计入 version 哈希的文档（其余文档变化不影响版本）
VERSIONED_DOCS = ("skill", "api-reference")

# 静态文案：改审批流程时同步这里。它们描述的是「规则」，无法从代码推导。
# 注意 rejected 也是 stage 的一个取值——驳回时后端会把 stage 一起置为 rejected。
RACK_STAGES = ["allocation", "implementation", "rejected"]
RACK_STATUSES = ["pending", "approved", "rejected"]
SCHEMA_RULES = [
    "servers 必填 `客户id`，且必须是 customers 表已登记的客户；保存时会回填 `客户名称`，`负责人` 为空时也从客户表带出。",
    "`所属机位`（servers）/ `机位`（cabinets）格式必须是区间 `N-N`（如 `9-10`），单值会被拒绝。",
    "`所属机位` 必须落在该机柜 `机位` 登记的范围内，且区间内每个机位都不能被其他服务器占用。",
    "`管理ip` / `交易ip` / `带外ip` 为 IPv4 或 CIDR；`管理ip网段` 等网段字段支持 `a.b.c.*`、`起-止`、CIDR。",
    "资产变更的生效方式由 token 对应身份决定：管理人员直接生效（`applied`），其他身份进入待审批（`pending` + `change_id`）。",
    "上下架请求按 `请求类型` 分流：`上架` 走 allocation（机位IP分配）→ implementation（具体实施）两段；`下架` / `变更` 直接进 implementation；驳回时 stage 置为 rejected。",
    "`上架` 要求序列号不在服务器表；`下架` / `变更` 要求序列号已存在。下架实施完成后服务器整行进回收站；变更实施完成后以现有记录为底合并后直接写正式表（序列号不可改）。",
    "同一机位/IP 不允许被多个「待实施」请求同时占用（in-transit 槽位保护）。",
    "资料（附件）按序列号归属，不进入任何 Excel/CSV 导出；回收站永久删除会连带清理该序列号的资料。",
    "servers 的 `用途分类` / `接入行情` / `接入柜台` 是多值标签字段：存 `A, B` 文本，写入时也接受数组，服务端会去重并按 `, ` 拼接；各字段候选池见 GET /api/field-options。",
    "回收站接口仅管理人员可用：GET /api/trash、POST /api/trash/<id>/restore、DELETE /api/trash/<id>。",
]


def _probe_user(role):
    """给能力函数用的最小 user dict，用于在 schema 里计算「哪些身份能读/能写」。"""
    return {"用户id": "_schema_probe", "身份": role, "业务单位": "", "所属机房": ""}


def base_dir():
    from . import storage

    config = storage.STATE.get("config") or {}
    return Path(config.get("base_dir") or Path(__file__).resolve().parent.parent)


def doc_search_dirs():
    root = base_dir()
    return [
        root / "docs",
        root / "server_management_skill",
        root / "server_management_skill" / "references",
    ]


def available_docs():
    """可用文档名列表（去重、按 DOC_FILES 声明顺序）。"""
    names = []
    for name in DOC_FILES:
        if name.endswith(".md"):
            continue
        if resolve_doc(name):
            names.append(name)
    return names


def resolve_doc(name):
    """探测文档实际路径；名字不在白名单或文件不存在都返回 None。"""
    key = str(name or "").strip().lower()
    filenames = DOC_FILES.get(key)
    if not filenames:
        return None
    for directory in doc_search_dirs():
        for filename in filenames:
            candidate = directory / filename
            if candidate.is_file():
                return candidate
    return None


def read_doc(name):
    path = resolve_doc(name)
    if path is None:
        return None, None
    return path.read_bytes(), path


def doc_sha256(name):
    content, _ = read_doc(name)
    if content is None:
        return ""
    return hashlib.sha256(content).hexdigest()


def build_schema():
    """运行时组装字段/权限/流程结构。任何字段增删都直接反映到这里。"""
    entities = {}
    for key, meta in ENTITIES.items():
        entities[key] = {
            "label": meta["label"],
            "key": list(meta["key"]),
            "headers": list(meta["headers"]),
            "aliases": dict(FIELD_ALIASES.get(key, {})),
            # 多值字段：值为字符串（"A, B"），写入时也接受数组，服务端统一归一
            "multi_value_fields": list(MULTI_VALUE_FIELDS.get(key, ())),
            "can_view": [role for role in ROLES if can_view_entity(key, _probe_user(role))],
            "can_mutate": [role for role in ROLES if can_mutate_entity(key, _probe_user(role))],
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "entities": entities,
        "rack_headers": list(RACK_REQUEST_HEADERS),
        "rack_export_headers": list(RACK_REQUEST_EXPORT_HEADERS),
        "rack_aliases": dict(RACK_REQUEST_FIELD_ALIASES),
        "rack_stages": list(RACK_STAGES),
        "rack_statuses": list(RACK_STATUSES),
        "capabilities": {role: capabilities_for(_probe_user(role)) for role in ROLES},
        "rules": list(SCHEMA_RULES),
    }


def _schema_digest(schema=None):
    payload = schema if schema is not None else build_schema()
    # sort_keys + 紧凑分隔符：保证同一份结构永远得到同一个字节序列
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_version(schema=None):
    """文档内容 + schema 的确定性哈希，取前 12 位。"""
    digest = hashlib.sha256()
    for name in VERSIONED_DOCS:
        digest.update(name.encode("utf-8"))
        digest.update(b"\x00")
        content, _ = read_doc(name)
        digest.update(content or b"")
        digest.update(b"\x00")
    digest.update(_schema_digest(schema).encode("ascii"))
    return digest.hexdigest()[:12]


def _updated_at(paths):
    stamps = []
    for path in paths:
        try:
            stamps.append(path.stat().st_mtime)
        except OSError:
            continue
    if not stamps:
        return ""
    from datetime import datetime

    return datetime.fromtimestamp(max(stamps)).strftime("%Y-%m-%d %H:%M:%S")


def build_manifest():
    files = []
    paths = []
    for name in VERSIONED_DOCS:
        content, path = read_doc(name)
        if content is None:
            continue
        paths.append(path)
        files.append(
            {
                "name": name,
                "path": path.name,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
        )
    schema = build_schema()
    return {
        "name": SKILL_NAME,
        "version": compute_version(schema),
        "schema_version": SCHEMA_VERSION,
        "updated_at": _updated_at(paths),
        "files": files,
        "endpoints": {
            "manifest": "/api/skill/manifest",
            "docs": "/api/skill/docs",
            "schema": "/api/skill/schema",
        },
        "counts": {
            "entities": len(ENTITIES),
            "fields": {key: len(meta["headers"]) for key, meta in ENTITIES.items()},
            "rack_headers": len(RACK_REQUEST_HEADERS),
        },
    }


def render_instructions(manifest=None):
    """生成 MCP ``InitializeResult.instructions`` 用的短文本。

    只是「增强」——不支持 instructions 的 host 会忽略它，因此 MCP 侧不依赖它，
    真正的入口是 ``sm_bootstrap`` 工具。
    """
    manifest = manifest or build_manifest()
    lines = [
        f"server-management skill v{manifest['version']}"
        f"（updated_at {manifest['updated_at'] or 'unknown'}）",
        "字段清单与权限矩阵不在本文内：调用 sm_schema（或 sm_bootstrap）获取，"
        "它由后端代码实时生成，永远与实现一致。",
        "写操作的返回值会显式区分 applied:true（已落库）与 pending:true + change_id（待审批），不要靠 message 文案猜测。",
    ]
    content, _ = read_doc(DEFAULT_DOC)
    if content:
        lines.append("")
        lines.append(content.decode("utf-8", errors="replace"))
    return "\n".join(lines)


def _is_safe_doc_name(name):
    """白名单之外的任何形式（含 ../、绝对路径、URL 编码）都不接受。"""
    text = str(name or "").strip()
    if not text or len(text) > 64:
        return False
    if ".." in text or text.startswith(".") or "/" in text or "\\" in text:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9 ._-]+", text))
