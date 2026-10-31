"""后端 HTTP 客户端：只用标准库 urllib，保证目标机不需要 pip install。

错误一律转成可直接展示给 agent 的中文句子——不要把 traceback 抛到协议层，
否则 agent 只会看到 "internal error" 然后盲目重试。
"""
from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "http://127.0.0.1:18888"


class SMError(Exception):
    """可读的业务错误，直接作为工具结果返回给 agent。"""


class SMClient:
    def __init__(self, base_url=None, token=None, timeout=None):
        self.base_url = (base_url or os.environ.get("SM_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        if token is None:
            token = os.environ.get("SM_TOKEN", "")
        self.token = (token or "").strip()
        try:
            self.timeout = float(timeout or os.environ.get("SM_TIMEOUT", "20"))
        except ValueError:
            self.timeout = 20.0
        self.verify = os.environ.get("SM_INSECURE", "") not in ("1", "true", "True")

    # ---------------------------------------------------------------- 基础请求

    def _ssl_context(self):
        if self.verify:
            return None
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    def _open(self, path, params=None, method="GET", payload=None):
        url = self.base_url + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None and v != ""}
            if clean:
                url += ("&" if "?" in url else "?") + urllib.parse.urlencode(clean)
        data = None
        headers = {"Accept": "application/json, text/markdown"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=self._ssl_context()) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            raise SMError(self._describe_http_error(exc.code, exc.read(), path)) from None
        except urllib.error.URLError as exc:
            raise SMError(
                "后端不可达：%s（%s）。检查 SM_BASE_URL 是否指向在本机/内网可访问的后端，"
                "以及服务是否在运行。" % (self.base_url, exc.reason)
            ) from None
        except (OSError, ssl.SSLError) as exc:
            # 超时/连接被重置等不一定被 urllib 包成 URLError，但语义一样是「连不上」
            raise SMError("后端不可达：%s（%s）" % (self.base_url, exc)) from None

    def _describe_http_error(self, code, raw, path):
        detail = ""
        try:
            body = json.loads(raw.decode("utf-8"))
            detail = str(body.get("error") or body.get("message") or "")
        except Exception:
            detail = raw.decode("utf-8", "replace").strip()[:300]
        if code == 401:
            return (
                "token 无效或已失效（%s）。让使用者在页面左下角「获取 Token」重新生成，"
                "更新 SM_TOKEN 后重启 MCP。" % path
            )
        if code == 403:
            return (
                "当前身份无此能力（%s）：%s。先调 sm_whoami 看身份与 capabilities，"
                "不要换参数重试——权限由后端判定，重试无用。" % (path, detail or "权限不足")
            )
        if code == 404:
            return "目标不存在（%s）：%s" % (path, detail or "记录或接口未找到")
        if code == 409:
            return "状态冲突（%s）：%s。先重新查询确认最新状态，不要重复提交。" % (path, detail or "记录已变化")
        if code == 413:
            return "请求体过大（%s）。附件请用本地文件路径上传，不要内联到参数里。" % path
        return "HTTP %s（%s）：%s" % (code, path, detail or "无错误详情")

    # ---------------------------------------------------------------- 便捷方法

    def json(self, path, params=None, method="GET", payload=None):
        raw = self._open(path, params=params, method=method, payload=payload)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            raise SMError("后端返回了非 JSON 响应（%s）：%r" % (path, raw[:200])) from None

    def text(self, path, params=None):
        return self._open(path, params=params).decode("utf-8", "replace")
