"""P0：/api/skill/{manifest,docs,schema} 的行为验证。

覆盖的关键语义：
- 默认免鉴权（只读文档，不含任何业务数据）
- schema 由 core 运行时组装，给 ENTITIES 加一列即刻生效
- version 把「文档 + schema」一起哈希，任一变化都会换版本
- ETag / 304、未知文档、非法文档名、可选 X-Skill-Token 收紧
"""
import json

import pytest


def client(app_env):
    return app_env["app"].test_client()


class TestManifest:
    def test_public_access_without_login(self, app_env):
        resp = client(app_env).get("/api/skill/manifest")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["name"] == "server-management"
        assert len(body["version"]) == 12 and all(c in "0123456789abcdef" for c in body["version"])
        assert resp.headers["X-Skill-Version"] == body["version"]

    def test_lists_versioned_files_with_hash(self, app_env):
        body = client(app_env).get("/api/skill/manifest").get_json()
        names = {item["name"] for item in body["files"]}
        assert names == {"skill", "api-reference"}
        for item in body["files"]:
            assert len(item["sha256"]) == 64
            assert item["size"] > 0

    def test_counts_match_entities(self, app_env):
        from app.core import ENTITIES, RACK_REQUEST_HEADERS

        body = client(app_env).get("/api/skill/manifest").get_json()
        assert body["counts"]["entities"] == len(ENTITIES)
        assert body["counts"]["rack_headers"] == len(RACK_REQUEST_HEADERS)
        for key, meta in ENTITIES.items():
            assert body["counts"]["fields"][key] == len(meta["headers"])

    def test_endpoints_point_at_real_routes(self, app_env):
        body = client(app_env).get("/api/skill/manifest").get_json()
        c = client(app_env)
        assert c.get(body["endpoints"]["manifest"]).status_code == 200
        assert c.get(body["endpoints"]["docs"]).status_code == 200
        assert c.get(body["endpoints"]["schema"]).status_code == 200


class TestSchema:
    def test_headers_equal_entities_exactly(self, app_env):
        from app.core import ENTITIES

        body = client(app_env).get("/api/skill/schema").get_json()
        assert list(body["entities"]) and set(body["entities"]) == set(ENTITIES)
        for key, meta in ENTITIES.items():
            assert body["entities"][key]["headers"] == list(meta["headers"])
            assert body["entities"][key]["key"] == list(meta["key"])
            assert body["entities"][key]["label"] == meta["label"]

    def test_rack_and_capabilities(self, app_env):
        from app.core import RACK_REQUEST_HEADERS, capabilities_for

        body = client(app_env).get("/api/skill/schema").get_json()
        assert body["rack_headers"] == list(RACK_REQUEST_HEADERS)
        assert body["rack_stages"] == ["allocation", "implementation", "rejected"], \
            "驳回时后端会把 stage 置为 rejected，schema 必须如实反映"
        assert body["rack_statuses"] == ["pending", "approved", "rejected"]
        for role, caps in body["capabilities"].items():
            assert caps == capabilities_for({
                "用户id": "_schema_probe", "身份": role, "业务单位": "", "所属机房": "",
            })

    def test_can_view_matrix_matches_role_rules(self, app_env):
        body = client(app_env).get("/api/skill/schema").get_json()
        assert set(body["entities"]["customers"]["can_view"]) == {"管理人员", "业务人员"}
        assert set(body["entities"]["cabinets"]["can_view"]) == {"管理人员", "机房人员"}
        assert set(body["entities"]["servers"]["can_view"]) == {"管理人员", "机房人员", "业务人员"}

    def test_new_field_appears_without_code_change(self, app_env):
        """核心验收点：给 ENTITIES 加一列 → schema 立刻多出该列，version 随之变化。"""
        from app import skill_docs
        from app.core import ENTITIES

        c = client(app_env)
        before = c.get("/api/skill/schema").get_json()
        before_version = c.get("/api/skill/manifest").get_json()["version"]
        before_count = len(before["entities"]["servers"]["headers"])

        ENTITIES["servers"]["headers"].append("临时压测字段")
        try:
            after = c.get("/api/skill/schema").get_json()
            after_version = c.get("/api/skill/manifest").get_json()["version"]
        finally:
            ENTITIES["servers"]["headers"].remove("临时压测字段")

        assert "临时压测字段" in after["entities"]["servers"]["headers"]
        assert len(after["entities"]["servers"]["headers"]) == before_count + 1
        assert after_version != before_version, "字段变了 version 必须变，否则 agent 无法感知结构漂移"

        restored = c.get("/api/skill/schema").get_json()
        assert "临时压测字段" not in restored["entities"]["servers"]["headers"]
        assert c.get("/api/skill/manifest").get_json()["version"] == before_version

    def test_version_is_stable_across_calls(self, app_env):
        c = client(app_env)
        versions = {c.get("/api/skill/manifest").get_json()["version"] for _ in range(3)}
        assert len(versions) == 1


class TestDocs:
    def test_default_doc_is_skill_md(self, app_env):
        resp = client(app_env).get("/api/skill/docs")
        assert resp.status_code == 200
        assert resp.headers["Content-Type"].startswith("text/markdown")
        assert resp.headers["X-Skill-Doc"] == "SKILL.md"
        text = resp.get_data(as_text=True)
        assert text.startswith("---")
        assert "server-management" in text

    def test_api_reference_resolved_from_references_dir(self, app_env):
        resp = client(app_env).get("/api/skill/docs?name=api-reference")
        assert resp.status_code == 200
        assert resp.headers["X-Skill-Doc"] == "api-reference.md"
        assert "references/api-reference.md" in resp.headers["X-Skill-Path"]
        assert "/api/" in resp.get_data(as_text=True)

    def test_etag_and_version_header(self, app_env):
        c = client(app_env)
        resp = c.get("/api/skill/docs?name=skill")
        etag = resp.headers["ETag"]
        assert etag.startswith('"') and etag.endswith('"')
        assert resp.headers["X-Skill-Version"] == c.get("/api/skill/manifest").get_json()["version"]

    def test_if_none_match_returns_304(self, app_env):
        c = client(app_env)
        etag = c.get("/api/skill/docs?name=skill").headers["ETag"]
        resp = c.get("/api/skill/docs?name=skill", headers={"If-None-Match": etag})
        assert resp.status_code == 304
        assert resp.get_data() == b""
        assert resp.headers["X-Skill-Version"]

    def test_stale_etag_returns_full_body(self, app_env):
        resp = client(app_env).get("/api/skill/docs?name=skill", headers={"If-None-Match": '"deadbeef"'})
        assert resp.status_code == 200
        assert len(resp.get_data()) > 0

    def test_unknown_doc_404_lists_available(self, app_env):
        resp = client(app_env).get("/api/skill/docs?name=nope")
        assert resp.status_code == 404
        assert resp.get_json()["available"] == ["skill", "api-reference"]

    @pytest.mark.parametrize("name", ["../app/config.py", "..", ".env", "/etc/passwd", "a/b"])
    def test_path_traversal_rejected(self, app_env, name):
        resp = client(app_env).get("/api/skill/docs", query_string={"name": name})
        assert resp.status_code in (400, 404)
        assert b"secret_key" not in resp.get_data()

    def test_doc_edit_changes_version(self, app_env):
        from app import skill_docs

        c = client(app_env)
        before = c.get("/api/skill/manifest").get_json()["version"]
        path = skill_docs.resolve_doc("api-reference")
        # 用二进制读写：text 模式在 Windows 上会把 LF 改成 CRLF，恢复后字节不一致
        original = path.read_bytes()
        try:
            path.write_bytes(original + b"\n<!-- p0-version-probe -->\n")
            after = c.get("/api/skill/manifest").get_json()["version"]
            assert after != before
            assert "p0-version-probe" in c.get("/api/skill/docs?name=api-reference").get_data(as_text=True)
        finally:
            path.write_bytes(original)
        assert c.get("/api/skill/manifest").get_json()["version"] == before


class TestAccessToken:
    def test_open_by_default(self, app_env):
        assert client(app_env).get("/api/skill/schema").status_code == 200

    def test_requires_header_when_configured(self, app_env):
        from app import storage

        storage.STATE["config"]["skill_docs_token"] = "s3cret"
        try:
            c = client(app_env)
            assert c.get("/api/skill/schema").status_code == 401
            assert c.get("/api/skill/manifest").status_code == 401
            assert c.get("/api/skill/docs").status_code == 401
            ok = c.get("/api/skill/schema", headers={"X-Skill-Token": "s3cret"})
            assert ok.status_code == 200
        finally:
            storage.STATE["config"]["skill_docs_token"] = ""

    def test_logged_in_user_passes_without_header(self, app_env):
        from app import storage

        storage.STATE["config"]["skill_docs_token"] = "s3cret"
        try:
            c = app_env["app"].test_client()
            c.post("/login", data={"username": "admin", "password": "pw"})
            assert c.get("/api/skill/schema").status_code == 200
        finally:
            storage.STATE["config"]["skill_docs_token"] = ""
