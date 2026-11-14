"""API Token 自助生成：全链路 HTTP 用例。

设计约束：token 只能由**本人**在登录态下生成 / 重置 / 查看。
管理员（manage_settings）不代为用户生成，用户表接口也不下发 api_token。
"""
import json
import re

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


def login(app, username, password="pw"):
    client = app.test_client()
    response = client.post("/login", data={"username": username, "password": password})
    assert response.status_code in (200, 302), response.status_code
    return client


def me(app, token):
    return app.test_client().get("/api/me", headers={"Authorization": f"Bearer {token}"})


def read_users_file(data_dir):
    return json.loads((data_dir / "users.json").read_text(encoding="utf-8"))


def self_token(client):
    response = client.post("/api/my-token/regenerate")
    assert response.status_code == 200, response.get_json()
    return response.get_json()["api_token"]


class TestSelfService:
    def test_initial_state_has_no_token(self, app_env):
        client = login(app_env["app"], "biz1")
        response = client.get("/api/my-token")
        assert response.status_code == 200
        assert response.get_json()["api_token"] == ""

    def test_user_generates_own_token(self, app_env):
        app, data_dir = app_env["app"], app_env["data_dir"]
        client = login(app, "biz1")

        token = client.post("/api/my-token/regenerate").get_json()["api_token"]
        assert TOKEN_RE.match(token), token

        assert client.get("/api/my-token").get_json()["api_token"] == token
        assert me(app, token).get_json()["用户id"] == "biz1"
        stored = {user["用户id"]: user for user in read_users_file(data_dir)}
        assert stored["biz1"]["api_token"] == token

    def test_token_authenticates_with_the_owner_identity(self, app_env):
        app = app_env["app"]
        token = self_token(login(app, "room1"))
        payload = me(app, token).get_json()
        assert payload["用户id"] == "room1"
        assert payload["身份"] == "机房人员"
        assert payload["所属机房"] == "机房A"

    def test_regenerating_own_token_kills_old_one(self, app_env):
        app = app_env["app"]
        client = login(app, "biz1")
        first = self_token(client)
        second = self_token(client)

        assert first != second
        assert me(app, first).status_code == 401
        assert me(app, second).status_code == 200

    def test_tokens_are_unique_across_users(self, app_env):
        app = app_env["app"]
        tokens = {self_token(login(app, user)) for user in ("admin", "room1", "biz1")}
        assert len(tokens) == 3

    def test_admin_also_generates_for_self_only(self, app_env):
        """管理员的 token 同样是自助生成，不是"管理员特权"。"""
        app = app_env["app"]
        client = login(app, "admin")
        token = self_token(client)
        assert me(app, token).get_json()["身份"] == "管理人员"
        # 生成自己的 token 不影响他人
        assert login(app, "biz1").get("/api/my-token").get_json()["api_token"] == ""

    def test_requires_login(self, app_env):
        client = app_env["app"].test_client()
        assert client.post("/api/my-token/regenerate").status_code == 401
        assert client.get("/api/my-token").status_code == 401

    def test_token_used_as_bearer_cannot_regenerate(self, app_env):
        """用 token 调用（无 session）不满足"本人登录后自助生成"，应被拒。"""
        app = app_env["app"]
        token = self_token(login(app, "biz1"))
        response = app.test_client().post(
            "/api/my-token/regenerate", headers={"Authorization": f"Bearer {token}"}
        )
        # 当前实现允许 Bearer 身份通过 require_login；此处固定语义：不允许凭 token 自行换发新 token
        assert response.status_code == 403

    def test_token_survives_app_restart(self, app_env):
        from app import create_app

        app, data_dir = app_env["app"], app_env["data_dir"]
        token = self_token(login(app, "room1"))

        restarted = create_app(data_dir.parent / "app.ini")
        assert restarted.test_client().get(
            "/api/me", headers={"Authorization": f"Bearer {token}"}
        ).status_code == 200


class TestNoAdminProvisioning:
    def test_admin_cannot_generate_for_others(self, app_env):
        """管理员代生成端点已移除。"""
        client = login(app_env["app"], "admin")
        assert client.post("/api/system-users/room1/token").status_code == 404

    def test_user_list_never_exposes_tokens(self, app_env):
        app = app_env["app"]
        token = self_token(login(app, "biz1"))

        listed = login(app, "admin").get("/api/system-users").get_json()["users"]
        by_id = {user["用户id"]: user for user in listed}
        assert "api_token" not in by_id["biz1"]
        assert token not in json.dumps(listed, ensure_ascii=False)

    def test_put_cannot_write_token(self, app_env):
        """设置页整表保存既不能给他人建 token，也不能覆盖已有 token。"""
        app = app_env["app"]
        token = self_token(login(app, "room1"))

        admin_client = login(app, "admin")
        users = admin_client.get("/api/system-users").get_json()["users"]
        for user in users:
            user["api_token"] = "injected-token"
        response = admin_client.put("/api/system-users", json={"users": users})
        assert response.status_code == 200
        assert all("api_token" not in user for user in response.get_json()["users"])

        assert me(app, token).status_code == 200, "本人 token 不应被设置页覆盖"
        assert me(app, "injected-token").status_code == 401
        stored = {user["用户id"]: user for user in read_users_file(app_env["data_dir"])}
        assert stored["room1"]["api_token"] == token
        assert stored["admin"]["api_token"] == ""

    def test_saving_user_list_keeps_existing_tokens(self, app_env):
        app, data_dir = app_env["app"], app_env["data_dir"]
        token = self_token(login(app, "room1"))

        admin_client = login(app, "admin")
        users = admin_client.get("/api/system-users").get_json()["users"]
        response = admin_client.put("/api/system-users", json={"users": users})
        assert response.status_code == 200

        stored = {user["用户id"]: user for user in read_users_file(data_dir)}
        assert stored["room1"]["api_token"] == token

    def test_new_user_does_not_inherit_token(self, app_env):
        app, data_dir = app_env["app"], app_env["data_dir"]
        token = self_token(login(app, "room1"))

        admin_client = login(app, "admin")
        users = admin_client.get("/api/system-users").get_json()["users"]
        users.append({"用户id": "newbie", "密码": "pw", "身份": "业务人员", "业务单位": "单位B"})
        response = admin_client.put("/api/system-users", json={"users": users})
        assert response.status_code == 200

        stored = {user["用户id"]: user for user in read_users_file(data_dir)}
        assert stored["newbie"]["api_token"] == ""
        assert stored["room1"]["api_token"] == token

    def test_non_admin_cannot_read_user_list(self, app_env):
        app = app_env["app"]
        assert login(app, "biz1").get("/api/system-users").status_code == 403
        assert login(app, "room1").get("/api/system-users").status_code == 403
