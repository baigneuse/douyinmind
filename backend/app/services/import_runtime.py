"""Per-job cloud admission and cancellation; never persists credentials or text."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import re
from threading import RLock
from typing import Callable


@dataclass(frozen=True)
class ProviderConfig:
    asr_key: str = field(default="", repr=False)
    summary_key: str = field(default="", repr=False)


class RuntimeControlError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


_USAGE_KEYS = {"prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens",
               "audio_tokens", "text_tokens", "image_tokens", "video_tokens", "cached_tokens",
               "reasoning_tokens", "prompt_tokens_details", "completion_tokens_details",
               "input_tokens_details", "output_tokens_details"}


def _safe_usage(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if not isinstance(value, dict):
        return None
    cleaned = {}
    for key, item in value.items():
        if key not in _USAGE_KEYS:
            continue
        if isinstance(item, dict):
            nested = _safe_usage(item)
            if nested:
                cleaned[key] = nested
        elif type(item) is int and item >= 0:
            cleaned[key] = item
    return cleaned or None


class ImportControl:
    """A budget counts SDK submissions, with SDK retries disabled by its caller.

    Cancellation closes admission to subsequent calls. An admitted request may
    already be in flight and can still finish and incur a provider charge.
    """
    def __init__(self, max_requests: int = 20, on_update: Callable[[dict], None] | None = None):
        if type(max_requests) is not int or not 1 <= max_requests <= 100:
            raise ValueError("请求次数上限必须为 1–100。")
        self.max_requests = max_requests
        self._on_update = on_update
        self._lock = RLock()
        self._cancelled = False
        self._revision = 0
        self._requests: list[dict] = []

    def snapshot(self) -> dict:
        with self._lock:
            return {"revision": self._revision, "max_requests": self.max_requests, "request_count": len(self._requests),
                    "cancel_requested": self._cancelled, "requests": deepcopy(self._requests)}

    def _publish(self):
        if self._on_update is not None:
            self._on_update(self.snapshot())

    def cancel(self):
        with self._lock:
            self._cancelled = True
            self._revision += 1
        self._publish()

    def check(self):
        with self._lock:
            if self._cancelled:
                raise RuntimeControlError("cancelled", "已停止后续处理；已发送的云请求可能仍完成并计费。")

    def before_request(self, provider: str, model: str, endpoint: str) -> int:
        with self._lock:
            self.check()
            if len(self._requests) >= self.max_requests:
                raise RuntimeControlError("request_budget_exhausted", "已达到本次请求次数上限；已完成片段保留，可调整上限后继续。")
            index = len(self._requests)
            self._requests.append({"index": index, "provider": provider, "model": model,
                                   "endpoint": endpoint, "status": "in_flight",
                                   "request_id": None, "usage": None, "error_code": None})
            self._revision += 1
        self._publish()
        return index

    def finish_request(self, request_index: int, status: str, request_id=None, usage=None, error_code=None):
        with self._lock:
            record = self._requests[request_index]
            safe_id = request_id if isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9._:/-]{1,200}", request_id) else None
            record.update(status=status if status in {"completed", "failed"} else "failed",
                          request_id=safe_id, usage=_safe_usage(usage),
                          error_code=error_code if isinstance(error_code, str) and re.fullmatch(r"[a-z_]{1,60}", error_code) else None)
            self._revision += 1
        self._publish()
