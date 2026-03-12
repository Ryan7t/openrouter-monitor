from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from .messages import build_help_message
from .models import UserIdentity
from .service import MonitorService, UserCommandError
from .utils import parse_time_value


LOGGER = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class IncomingMention:
    key: str | None
    name: str | None


@dataclass(slots=True, frozen=True)
class IncomingMessage:
    message_id: str
    chat_id: str | None
    chat_type: str
    message_type: str
    content: str
    mentions: tuple[IncomingMention, ...]
    open_id: str | None
    user_id: str | None
    union_id: str | None

    def as_identity(self) -> UserIdentity | None:
        if not self.open_id:
            return None
        return UserIdentity(open_id=self.open_id, user_id=self.user_id, union_id=self.union_id)


class FeishuCommandProcessor:
    def __init__(self, service: MonitorService) -> None:
        self.service = service
        self.messenger = service.notifier

    def handle_message(self, message: IncomingMessage) -> None:
        if message.chat_type != "p2p" and not message.mentions:
            return

        if message.message_type != "text":
            self._reply(message.message_id, "当前只支持文本指令。发送 /帮助 查看可用命令。")
            return

        command_text = extract_command_text(message.content, message.mentions)
        if not command_text:
            self._reply(message.message_id, build_help_message())
            return

        try:
            response = self._dispatch(command_text, message)
        except UserCommandError as exc:
            self._reply(message.message_id, str(exc))
            return
        except Exception:
            LOGGER.exception("Failed to process Feishu message.")
            self._reply(message.message_id, "命令处理失败，请稍后重试。")
            return

        if response:
            self._reply(message.message_id, response)

    def _dispatch(self, command_text: str, message: IncomingMessage) -> str:
        parts = command_text.strip().split(maxsplit=1)
        command_token = parts[0]
        remainder = parts[1].strip() if len(parts) > 1 else ""
        normalized_command = normalize_command(command_token)

        if normalized_command == "help":
            return build_help_message()
        if normalized_command == "detail":
            open_id = self._require_open_id(message)
            return self.service.inspect_user(open_id)
        if normalized_command == "bind":
            identity = self._require_identity(message)
            api_key, alias = parse_bind_arguments(remainder)
            return self.service.bind_key(identity, api_key, alias)
        if normalized_command == "delete":
            open_id = self._require_open_id(message)
            return self.service.delete_key(open_id, remainder)
        if normalized_command == "config":
            identity = self._require_identity(message)
            return self._handle_config(identity, remainder)
        return build_help_message()

    def _handle_config(self, identity: UserIdentity, remainder: str) -> str:
        if not remainder:
            return self.service.get_user_config_message(identity.open_id)

        tokens = remainder.split(maxsplit=1)
        action = normalize_config_action(tokens[0])
        tail = tokens[1].strip() if len(tokens) > 1 else ""

        if action == "view":
            return self.service.get_user_config_message(identity.open_id)
        if action == "time":
            if not tail:
                raise UserCommandError("请指定推送时间，例如: /配置 推送时间 09:00")
            try:
                push_time = parse_time_value(tail)
            except ValueError as exc:
                raise UserCommandError("时间格式不对，请使用类似 09:00、22:30 的格式。") from exc
            return self.service.update_push_time(identity, push_time)
        if action in {"warning", "danger", "critical"}:
            if not tail:
                raise UserCommandError(f"请指定金额，例如: /配置 {tokens[0]} 10")
            try:
                amount = float(tail)
            except ValueError as exc:
                raise UserCommandError("金额必须是数字，例如: 10 或 5.5") from exc
            return self.service.update_threshold(identity, action, amount)
        raise UserCommandError("不支持的设置项，发送 /帮助 查看可用指令。")

    def _require_identity(self, message: IncomingMessage) -> UserIdentity:
        identity = message.as_identity()
        if identity is None:
            raise UserCommandError("当前事件没有返回 open_id，无法识别你的身份。")
        return identity

    def _require_open_id(self, message: IncomingMessage) -> str:
        identity = self._require_identity(message)
        return identity.open_id

    def _reply(self, message_id: str, text: str) -> None:
        self.messenger.send_text(text, reply_to_message_id=message_id)


class FeishuLongConnectionApp:
    def __init__(
        self,
        service: MonitorService,
        command_processor: FeishuCommandProcessor | None = None,
        ws_client_factory: Any | None = None,
    ) -> None:
        self.service = service
        self.command_processor = command_processor or FeishuCommandProcessor(service)
        self.ws_client_factory = ws_client_factory or self._create_ws_client

    def run_forever(self) -> None:
        dispatcher = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._handle_sdk_message)
            .build()
        )
        self.service.start_scheduler()
        self.service.safe_threshold_scan()
        ws_client = self.ws_client_factory(
            self.service.config.feishu.app_id,
            self.service.config.feishu.app_secret,
            dispatcher,
        )
        try:
            LOGGER.info("Starting Feishu long connection client.")
            ws_client.start()
        finally:
            self.service.stop_scheduler()

    def _create_ws_client(self, app_id: str, app_secret: str, dispatcher: Any) -> Any:
        return lark.ws.Client(
            app_id=app_id,
            app_secret=app_secret,
            event_handler=dispatcher,
            log_level=lark.LogLevel.INFO,
            auto_reconnect=True,
        )

    def _handle_sdk_message(self, data: P2ImMessageReceiveV1) -> None:
        message = convert_sdk_event(data)
        if message is None:
            return
        self.command_processor.handle_message(message)


COMMAND_ALIASES = {
    "inspect": "detail",
    "detail": "detail",
    "report": "detail",
    "详情": "detail",
    "详细": "detail",
    "bind": "bind",
    "绑定": "bind",
    "delete": "delete",
    "remove": "delete",
    "删除": "delete",
    "config": "config",
    "配置": "config",
    "help": "help",
    "帮助": "help",
}


CONFIG_ACTION_ALIASES = {
    "查看": "view",
    "view": "view",
    "show": "view",
    "推送时间": "time",
    "time": "time",
    "warning": "warning",
    "警告": "warning",
    "danger": "danger",
    "危险": "danger",
    "critical": "critical",
    "严重": "critical",
}


def normalize_command(token: str) -> str:
    normalized = token.lstrip("/").strip().lower()
    return COMMAND_ALIASES.get(normalized, normalized)


def normalize_config_action(token: str) -> str:
    normalized = token.strip().lower()
    return CONFIG_ACTION_ALIASES.get(normalized, normalized)


def parse_bind_arguments(remainder: str) -> tuple[str, str | None]:
    if not remainder:
        raise UserCommandError("请提供要绑定的 Key，例如: /绑定 sk-or-v1-xxx 我的Key")

    api_key, _, tail = remainder.partition(" ")
    if not api_key:
        raise UserCommandError("请提供要绑定的 Key，例如: /绑定 sk-or-v1-xxx 我的Key")
    if not tail.strip():
        return api_key.strip(), None

    alias_text = tail.strip()
    # 兼容旧的 别名=xxx / alias=xxx 写法
    match = re.fullmatch(r"(?:(?:别名|备注名?|alias)=(.+))", alias_text, flags=re.IGNORECASE)
    if match:
        alias_text = match.group(1).strip()
    if not alias_text:
        raise UserCommandError("备注名不能为空。")
    return api_key.strip(), alias_text


def extract_command_text(content: str, mentions: tuple[IncomingMention, ...]) -> str:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return ""
    text = payload.get("text")
    if not isinstance(text, str):
        return ""

    normalized = text
    for mention in mentions:
        if mention.key:
            normalized = normalized.replace(mention.key, " ")
        if mention.name:
            normalized = normalized.replace(f"@{mention.name}", " ")

    normalized = normalized.replace("\u200b", " ")
    normalized = " ".join(normalized.split())
    return normalized.lstrip(",，:：").strip()


def convert_sdk_event(data: P2ImMessageReceiveV1) -> IncomingMessage | None:
    if data.event is None or data.event.message is None:
        return None
    sdk_message = data.event.message
    message_id = sdk_message.message_id
    if not isinstance(message_id, str) or not message_id.strip():
        return None

    mentions: list[IncomingMention] = []
    if sdk_message.mentions:
        for item in sdk_message.mentions:
            mentions.append(
                IncomingMention(
                    key=getattr(item, "key", None),
                    name=getattr(item, "name", None),
                )
            )

    sender_id = getattr(getattr(data.event, "sender", None), "sender_id", None)
    return IncomingMessage(
        message_id=message_id.strip(),
        chat_id=sdk_message.chat_id,
        chat_type=sdk_message.chat_type or "",
        message_type=sdk_message.message_type or "",
        content=sdk_message.content or "",
        mentions=tuple(mentions),
        open_id=getattr(sender_id, "open_id", None),
        user_id=getattr(sender_id, "user_id", None),
        union_id=getattr(sender_id, "union_id", None),
    )
