"""应用工厂：加载配置 -> 初始化存储 -> 注册路由。"""
from __future__ import annotations

import logging

from flask import Flask

from . import config as config_module
from . import storage


def create_app(config_path=None):
    cfg = config_module.load_config(config_path)
    storage.init_runtime(cfg)

    app = Flask(__name__)
    app.secret_key = cfg["secret_key"]
    app.config.update(MAX_CONTENT_LENGTH=16 * 1024 * 1024, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
    logging.basicConfig(level="INFO")
    app.logger.setLevel("INFO")

    storage.init_storage()

    from .routes import bp

    app.register_blueprint(bp)
    return app
