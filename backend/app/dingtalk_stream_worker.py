from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

import dingtalk_stream


DEFAULT_CONFIG = "/data/dahua_pricing_runtime/agent/dingtalk_stream.local.json"


def read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def safe_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def post_json(url: str, payload: Dict[str, Any], timeout: int = 20) -> Dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            return json.loads(text) if text else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"backend HTTP {e.code}: {body}") from e


def incoming_text(message: dingtalk_stream.ChatbotMessage) -> str:
    if message.text and message.text.content:
        return safe_text(message.text.content)
    texts = message.get_text_list() if hasattr(message, "get_text_list") else []
    return "\n".join(safe_text(x) for x in texts if safe_text(x)).strip()


def build_backend_payload(
    callback_message: dingtalk_stream.CallbackMessage,
    incoming_message: dingtalk_stream.ChatbotMessage,
) -> Dict[str, Any]:
    payload = incoming_message.to_dict()
    payload.update(
        {
            "source": "dingtalk-stream",
            "streamHeaders": callback_message.headers.to_dict(),
            "text": {"content": incoming_text(incoming_message)},
            "isInAtList": bool(incoming_message.is_in_at_list),
            "senderStaffId": incoming_message.sender_staff_id,
            "senderId": incoming_message.sender_id,
            "senderNick": incoming_message.sender_nick,
            "conversationId": incoming_message.conversation_id,
            "conversationTitle": incoming_message.conversation_title,
            "robotCode": incoming_message.robot_code,
            "msgId": incoming_message.message_id,
            "raw": callback_message.data,
        }
    )
    return payload


def build_reply(result: Dict[str, Any]) -> str:
    if not result.get("matched"):
        if result.get("unsupported"):
            return "哥们，搞我是吧，现在还不会嗷"
        return safe_text(result.get("reason")) or "收到，但没有识别到可执行的审批查询指令。"

    command = result.get("command") or {}
    if safe_text(command.get("intent")) == "scan_gsp_under_approval":
        return (
            "哥们，我识别到你要查当前 GSP 所有 Under Approval / 待审批摘要。"
            "这条能力的 Windows 扫描器已经跑通，但 Linux 这边还没接扫描结果回传接口；"
            "接上后就能直接按负责人、节点和数量回群里。"
        )

    queue = result.get("queue") or {}
    sheet = safe_text(command.get("sheet")) or "全部月份"
    count = int(queue.get("count") or 0)
    if count == 0:
        return f"哥们，{sheet} 暂时没查到需要跟进的 PLA 审批。"

    return ""


def can_reply_to_group(result: Dict[str, Any]) -> bool:
    """Treat the backend reply policy as the final outbound safety gate."""
    event = result.get("event") if isinstance(result, dict) else None
    policy = event.get("reply_policy") if isinstance(event, dict) else None
    return bool(isinstance(policy, dict) and policy.get("can_reply_to_group"))


class DahuaPricingStreamHandler(dingtalk_stream.ChatbotHandler):
    def __init__(self, cfg: Dict[str, Any]):
        super().__init__()
        self.cfg = cfg
        self.backend_event_url = safe_text(cfg.get("backend_event_url")) or "http://127.0.0.1:8000/api/agent/dingtalk/event"
        self.reply_enabled = bool(cfg.get("reply_enabled", True))
        self.reply_on_backend_error = bool(cfg.get("reply_on_backend_error", False))
        self.mention_only = bool(cfg.get("mention_only", True))

    async def process(self, callback_message: dingtalk_stream.CallbackMessage):
        incoming_message = dingtalk_stream.ChatbotMessage.from_dict(callback_message.data)
        if self.mention_only and not bool(incoming_message.is_in_at_list):
            self.logger.info("ignore non-mentioned message msgId=%s", incoming_message.message_id)
            return dingtalk_stream.AckMessage.STATUS_OK, "ignored"

        payload = build_backend_payload(callback_message, incoming_message)
        self.logger.info(
            "stream message msgId=%s sender=%s conversation=%s text=%s",
            incoming_message.message_id,
            incoming_message.sender_staff_id or incoming_message.sender_id,
            incoming_message.conversation_id,
            incoming_text(incoming_message)[:120],
        )
        try:
            result = post_json(self.backend_event_url, payload)
        except Exception as e:
            self.logger.exception("backend event failed")
            if self.reply_enabled and self.reply_on_backend_error:
                self.reply_text(f"后端处理失败：{type(e).__name__}", incoming_message)
            return dingtalk_stream.AckMessage.STATUS_SYSTEM_EXCEPTION, str(e)

        if self.reply_enabled and can_reply_to_group(result):
            reply = build_reply(result)
            if reply:
                self.reply_text(reply, incoming_message)
        elif self.reply_enabled:
            self.logger.info(
                "suppress group reply msgId=%s backend_policy=false",
                incoming_message.message_id,
            )
        return dingtalk_stream.AckMessage.STATUS_OK, "OK"


def setup_logger(log_path: Optional[str]) -> logging.Logger:
    logger = logging.getLogger("dahua.dingtalk_stream")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)
    if log_path:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
    return logger


def load_config(path: Path) -> Dict[str, Any]:
    cfg = read_json(path)
    client_id = safe_text(os.getenv("DINGTALK_CLIENT_ID") or cfg.get("client_id"))
    client_secret = safe_text(os.getenv("DINGTALK_CLIENT_SECRET") or cfg.get("client_secret"))
    if not client_id or not client_secret:
        raise SystemExit(f"missing client_id/client_secret in {path}")
    cfg["client_id"] = client_id
    cfg["client_secret"] = client_secret
    return cfg


def main() -> None:
    parser = argparse.ArgumentParser(description="Dahua pricing DingTalk Stream worker")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    logger = setup_logger(safe_text(cfg.get("log_path")) or "/data/dahua_pricing_runtime/agent/dingtalk_stream.log")
    credential = dingtalk_stream.Credential(cfg["client_id"], cfg["client_secret"])
    client = dingtalk_stream.DingTalkStreamClient(credential, logger=logger)
    client.register_callback_handler(
        dingtalk_stream.ChatbotMessage.TOPIC,
        DahuaPricingStreamHandler(cfg),
    )
    logger.info("starting DingTalk Stream worker")
    client.start_forever()


if __name__ == "__main__":
    main()
