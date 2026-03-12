from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)


LOGGER = logging.getLogger(__name__)


class FeishuAppClient:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        sdk_client: object | None = None,
        sleeper: Callable[[float], None] | None = None,
        time_source: Callable[[], float] | None = None,
        max_requests_per_minute: int = 10,
        max_retries: int = 3,
        retry_backoff_seconds: float = 1.0,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.sdk_client = sdk_client or (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .log_level(lark.LogLevel.INFO)
            .build()
        )
        self.sleeper = sleeper or time.sleep
        self.time_source = time_source or time.monotonic
        self.max_requests_per_minute = max_requests_per_minute
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.max_text_length = 1800
        self._send_timestamps: deque[float] = deque()
        self._rate_lock = threading.Lock()

    def send_text(
        self,
        text: str,
        mention_all: bool = False,
        receive_id: str | None = None,
        receive_id_type: str = "open_id",
        reply_to_message_id: str | None = None,
        reply_in_thread: bool = False,
    ) -> bool:
        chunks = self._split_text(self._prepend_mention_all(text, mention_all))
        for chunk in chunks:
            if not self._send_chunk(
                chunk,
                receive_id=receive_id,
                receive_id_type=receive_id_type,
                reply_to_message_id=reply_to_message_id,
                reply_in_thread=reply_in_thread,
            ):
                return False
        return True

    def _send_chunk(
        self,
        text: str,
        receive_id: str | None,
        receive_id_type: str,
        reply_to_message_id: str | None,
        reply_in_thread: bool,
    ) -> bool:
        content = json.dumps({"text": text}, ensure_ascii=False)
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                if reply_to_message_id:
                    response = self._reply_message(reply_to_message_id, content, reply_in_thread)
                else:
                    if not receive_id:
                        LOGGER.error("Missing receive_id for proactive Feishu message.")
                        return False
                    response = self._create_message(receive_id, receive_id_type, content)
            except Exception:
                if attempt >= self.max_retries:
                    LOGGER.exception("Feishu send_text failed after retries.")
                    return False
                self._sleep_before_retry(attempt)
                continue

            if response.success():
                return True

            if self._is_retryable_response(response) and attempt < self.max_retries:
                LOGGER.warning(
                    "Feishu message send hit retryable failure (code=%s, msg=%s), retrying.",
                    getattr(response, "code", None),
                    getattr(response, "msg", ""),
                )
                self._sleep_before_retry(attempt)
                continue

            LOGGER.error(
                "Feishu message rejected: code=%s msg=%s log_id=%s",
                getattr(response, "code", None),
                getattr(response, "msg", ""),
                getattr(response, "get_log_id", lambda: "-")(),
            )
            return False
        return False

    def _create_message(self, receive_id: str, receive_id_type: str, content: str) -> object:
        request = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type("text")
                .content(content)
                .uuid(str(uuid.uuid4()))
                .build()
            )
            .build()
        )
        return self.sdk_client.im.v1.message.create(request)

    def _reply_message(self, message_id: str, content: str, reply_in_thread: bool) -> object:
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type("text")
                .content(content)
                .reply_in_thread(reply_in_thread)
                .uuid(str(uuid.uuid4()))
                .build()
            )
            .build()
        )
        return self.sdk_client.im.v1.message.reply(request)

    def _throttle(self) -> None:
        with self._rate_lock:
            now = self.time_source()
            cutoff = now - 60.0
            while self._send_timestamps and self._send_timestamps[0] <= cutoff:
                self._send_timestamps.popleft()
            if len(self._send_timestamps) >= self.max_requests_per_minute:
                sleep_seconds = max(0.0, 60.0 - (now - self._send_timestamps[0]))
                if sleep_seconds > 0:
                    self.sleeper(sleep_seconds)
                now = self.time_source()
                cutoff = now - 60.0
                while self._send_timestamps and self._send_timestamps[0] <= cutoff:
                    self._send_timestamps.popleft()
            self._send_timestamps.append(self.time_source())

    def _sleep_before_retry(self, attempt: int) -> None:
        self.sleeper(self.retry_backoff_seconds * (2 ** (attempt - 1)))

    def _prepend_mention_all(self, text: str, mention_all: bool) -> str:
        if not mention_all:
            return text
        return '<at user_id="all">所有人</at>\n' + text

    def _split_text(self, text: str) -> list[str]:
        if len(text) <= self.max_text_length:
            return [text]

        chunks: list[str] = []
        current_lines: list[str] = []
        current_length = 0
        for original_line in text.splitlines():
            line_parts = (
                [original_line[index : index + self.max_text_length] for index in range(0, len(original_line), self.max_text_length)]
                if len(original_line) > self.max_text_length
                else [original_line]
            )
            for line in line_parts:
                separator = 1 if current_lines else 0
                prospective = current_length + separator + len(line)
                if current_lines and prospective > self.max_text_length:
                    chunks.append("\n".join(current_lines))
                    current_lines = [line]
                    current_length = len(line)
                    continue
                current_lines.append(line)
                current_length = prospective

        if current_lines:
            chunks.append("\n".join(current_lines))
        return chunks

    def _is_retryable_response(self, response: object) -> bool:
        code = getattr(response, "code", None)
        msg = str(getattr(response, "msg", "")).lower()
        if code in {429, 90013, 99991668, 11232}:
            return True
        return any(token in msg for token in ("429", "rate", "频", "too many", "frequency"))
