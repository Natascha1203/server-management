from __future__ import annotations

import json
import logging
import queue
import threading
from urllib.parse import urlsplit, urlunsplit
from urllib import request


def _text(value):
    return "" if value is None else str(value)


def build_content(event, markdown=False):
    title = _text(event.get("title") or "审核通知")
    fields = [
        ("类型", event.get("entity_label") or event.get("audit_type")),
        ("操作", event.get("operation")),
        ("记录", event.get("record_key")),
        ("提交人", event.get("submitter")),
        ("审核人", event.get("reviewer")),
        ("时间", event.get("time") or event.get("created_at") or event.get("reviewed_at")),
        ("驳回原因", event.get("reject_reason")),
        ("摘要", event.get("summary")),
    ]
    if markdown:
        lines = [f"**{title}**"]
        for label, value in fields:
            value = _text(value)
            if value:
                lines.append(f">{label}：<font color=\"comment\">{value}</font>")
        return "\n".join(lines)
    lines = [title]
    for label, value in fields:
        value = _text(value)
        if value:
            lines.append(f"{label}：{value}")
    return "\n".join(lines)


def build_payload(message_type, event):
    message_type = _text(message_type or "markdown").lower()
    if message_type == "text":
        return {"msgtype": "text", "text": {"content": build_content(event, markdown=False)}}
    return {"msgtype": "markdown", "markdown": {"content": build_content(event, markdown=True)}}


def mask_webhook_url(url):
    parts = urlsplit(_text(url))
    query_items = []
    for item in parts.query.split("&"):
        key, sep, value = item.partition("=")
        query_items.append(f"{key}=***" if sep and key.lower() == "key" else item)
    query = "&".join(query_items)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def send_notification(config, event, opener=None, logger=None, timeout=5):
    logger = logger or logging.getLogger(__name__)
    payload = build_payload(config.get("notify_message_type"), event)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    enabled = int(config.get("notify_enabled") or 0)
    webhook_url = _text(config.get("notify_webhook_url")).strip()
    log_url = mask_webhook_url(webhook_url)
    if not enabled or not webhook_url:
        logger.info("notification (dry-run) POST %s\n%s", log_url or "(no url)", data.decode("utf-8"))
        return {"sent": False, "skipped": True}

    logger.info("notification POST %s\n%s", log_url, data.decode("utf-8"))
    req = request.Request(webhook_url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    opener = opener or request.urlopen
    try:
        with opener(req, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            status = getattr(response, "status", None)
        try:
            result = json.loads(body)
        except json.JSONDecodeError:
            result = {}
        errcode = int(result.get("errcode") or 0)
        errmsg = _text(result.get("errmsg"))
        if errcode:
            error = errmsg or body
            logger.warning("notification robot rejected status=%s errcode=%s errmsg=%s response=%s", status, errcode, errmsg, body)
            return {"sent": False, "response": body, "status": status, "errcode": errcode, "error": error}
        logger.info("notification response status=%s body=%s", status, body)
        return {"sent": True, "response": body, "status": status, "errcode": errcode}
    except Exception as exc:
        logger.warning("notification send failed: %s", exc)
        return {"sent": False, "error": str(exc)}


_NOTIFY_QUEUE = queue.Queue()


def _notify_worker():
    while True:
        config, event, logger, timeout = _NOTIFY_QUEUE.get()
        try:
            send_notification(config, event, logger=logger, timeout=timeout)
        except Exception:
            (logger or logging.getLogger(__name__)).exception("notification worker unexpected error")
        finally:
            _NOTIFY_QUEUE.task_done()


threading.Thread(target=_notify_worker, daemon=True, name="notify-worker").start()


def send_notification_async(config, event, logger=None, timeout=5):
    """将通知入队，由后台 worker 异步发送，不阻塞调用方。"""
    _NOTIFY_QUEUE.put((config, event, logger, timeout))


def wait_for_notifications():
    """等待队列中所有通知处理完成（供测试使用）。"""
    _NOTIFY_QUEUE.join()
