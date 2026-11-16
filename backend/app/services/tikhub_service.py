"""TikHub as a consent-gated source for the existing network video library.

The service owns three pieces of local state and no credentials on disk:

* a request ledger (counts and list-price estimates; never tokens or URLs);
* short-lived previews of provider results awaiting the user's selection;
* signed media addresses, kept in memory only until they expire.

A paid request is sent only after the caller confirms it for that action and
while the local daily cap allows it. Cookies are never read or sent.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
from threading import RLock
import time
from urllib.parse import urlsplit
from uuid import uuid4

from app.services.local_media_workspace import WorkspaceError, now, write_json
from app.services.network_download import NetworkDownloadError
from app.services.tikhub_adapter import (DEFAULT_BASE_URL, ENDPOINTS, PAGE_SIZE, PRICES_CHECKED_ON, TikHubClient,
                                         TikHubError, canonical_creator_url, describe_aweme, offline_item_id,
                                         parse_creator_link, parse_video_link, unavailable_message, validate_base_url)

PREVIEW_SECONDS = 30 * 60
MAX_PREVIEWS = 8
MAX_CACHED_MEDIA = 200
MEDIA_SECONDS = 6 * 60 * 60
LEDGER_DAYS = 90
_BUCKET = {"requests": 0, "succeeded": 0, "failed": 0, "paid_attempts": 0, "billable": 0, "estimated_cost_usd": 0.0}
_UPSTREAM = {"tikhub_unauthorized", "tikhub_payment_required", "tikhub_forbidden", "tikhub_rate_limited",
             "tikhub_unavailable", "tikhub_network_error", "tikhub_invalid_response"}


def _failure(error: Exception) -> WorkspaceError:
    """Translate adapter/input errors into the library's safe API errors."""
    if isinstance(error, WorkspaceError):
        return error
    code = getattr(error, "code", "tikhub_invalid_response")
    status = 409 if code in {"tikhub_not_configured", "tikhub_invalid_endpoint"} else 502 if code in _UPSTREAM else 400
    return WorkspaceError(code, str(error), status)


class TikHubService:
    def __init__(self, library, *, configuration=None, transport=None, clock=time.time):
        self.library = library
        self._configuration = configuration
        self._transport = transport
        self._clock = clock
        self._lock = RLock()
        self._reserved = 0
        self._previews: OrderedDict[str, dict] = OrderedDict()
        self._media: OrderedDict[str, dict] = OrderedDict()
        library.path("tikhub").mkdir(exist_ok=True)

    # ----- configuration; the token never leaves this object -----
    def configuration(self):
        if self._configuration is None:
            from app.core.config import settings
            return settings
        return self._configuration

    def _token(self) -> str:
        value = getattr(self.configuration(), "tikhub_api_key", "")
        value = value.get_secret_value() if hasattr(value, "get_secret_value") else value
        return value.strip() if isinstance(value, str) else ""

    @property
    def configured(self) -> bool:
        return bool(self._token())

    def _base_url(self) -> str:
        return getattr(self.configuration(), "tikhub_base_url", DEFAULT_BASE_URL) or DEFAULT_BASE_URL

    def _daily_limit(self) -> int:
        value = getattr(self.configuration(), "tikhub_daily_request_limit", 20)
        return value if type(value) is int and value >= 0 else 0

    def _client(self, *, token: bool = True) -> TikHubClient:
        timeout = getattr(self.configuration(), "tikhub_timeout_seconds", 45.0)
        return TikHubClient(self._token() if token else "", base_url=self._base_url(), timeout=timeout,
                            transport=self._transport, on_request=self._record)

    # ----- request ledger -----
    def _ledger_path(self):
        return self.library.path("tikhub", "usage.json")

    def _ledger(self) -> dict:
        try:
            value = json.loads(self._ledger_path().read_text(encoding="utf-8"))
            if isinstance(value, dict) and isinstance(value.get("days"), dict) and isinstance(value.get("total"), dict):
                return value
        except (OSError, ValueError):
            pass
        return {"version": 1, "total": dict(_BUCKET), "days": {}, "endpoints": {}, "last_request": None, "last_check": None}

    def _today(self) -> str:
        return datetime.fromtimestamp(self._clock(), timezone.utc).date().isoformat()

    def _record(self, entry: dict) -> None:
        with self._lock:
            ledger = self._ledger()
            day = ledger["days"].setdefault(self._today(), dict(_BUCKET))
            per_endpoint = ledger.setdefault("endpoints", {}).setdefault(entry["endpoint"], dict(_BUCKET))
            for bucket in (ledger["total"], day, per_endpoint):
                for key, default in _BUCKET.items():
                    bucket.setdefault(key, default)
                bucket["requests"] += 1
                bucket["succeeded" if entry["ok"] else "failed"] += 1
                if entry["paid"]:
                    bucket["paid_attempts"] += 1
                if entry["ok"] and entry["cost_usd"] > 0:
                    bucket["billable"] += 1
                    bucket["estimated_cost_usd"] = round(bucket["estimated_cost_usd"] + entry["cost_usd"], 6)
            for stale in sorted(ledger["days"])[:-LEDGER_DAYS]:
                del ledger["days"][stale]
            # Only fixed endpoint names and status codes are recorded: no URLs, ids or tokens.
            ledger["last_request"] = {"at": now(), "endpoint": entry["endpoint"], "ok": entry["ok"],
                                      "http_status": entry["http_status"], "code": entry["code"]}
            write_json(self._ledger_path(), ledger)

    def usage(self) -> dict:
        with self._lock:
            ledger = self._ledger()
            today = {**_BUCKET, **ledger["days"].get(self._today(), {})}
            limit = self._daily_limit()
            return {"today": today, "total": {**_BUCKET, **ledger["total"]},
                    "endpoints": deepcopy(ledger.get("endpoints", {})), "last_request": ledger.get("last_request"),
                    "daily_limit": limit, "remaining_today": max(0, limit - today["paid_attempts"] - self._reserved)}

    @staticmethod
    def estimate(paid_requests: int) -> float:
        return round(paid_requests * ENDPOINTS["video_by_id"].cost_usd, 6)

    def authorize(self, paid_requests: int, confirm_paid: bool) -> None:
        """Refuse a paid action unless it is configured, confirmed and within today's cap."""
        if paid_requests <= 0:
            return  # Addresses already in memory: nothing is sent to TikHub.
        if not self.configured:
            raise WorkspaceError("tikhub_not_configured", unavailable_message("tikhub_not_configured"), 409)
        if not confirm_paid:
            raise WorkspaceError("tikhub_confirmation_required",
                                 f"此操作将向 TikHub 发送 {paid_requests} 次付费请求（按公开单价估算约 "
                                 f"${self.estimate(paid_requests):.3f}）。确认后才会发送。", 409)
        with self._lock:
            if self.usage()["remaining_today"] < paid_requests:
                raise WorkspaceError("tikhub_budget_exhausted",
                                     f"已达到本机设置的 TikHub 每日请求上限（{self._daily_limit()} 次）；没有发送请求。", 409)

    @contextmanager
    def _paid(self, paid_requests: int, confirm_paid: bool):
        with self._lock:
            self.authorize(paid_requests, confirm_paid)
            self._reserved += paid_requests
        try:
            yield self._client()
        finally:
            with self._lock:
                self._reserved -= paid_requests

    # ----- connection status -----
    def status(self) -> dict:
        try:
            endpoint, valid = urlsplit(validate_base_url(self._base_url())).hostname, True
        except TikHubError:
            endpoint, valid = None, False
        with self._lock:
            last_check = self._ledger().get("last_check")
        return {"available": True, "configured": self.configured, "endpoint": endpoint, "endpoint_valid": valid,
                "cookie_access": False, "page_size": PAGE_SIZE,
                "prices": {"per_paid_request_usd": ENDPOINTS["video_by_id"].cost_usd, "checked_on": PRICES_CHECKED_ON,
                           "free": ["demo_video", "account"]},
                "usage": self.usage(), "last_check": last_check, "comparison": self.library.download_statistics()}

    def check(self, mode: str) -> dict:
        """User-triggered live check. ``demo`` is free and sends no token."""
        result = {"at": now(), "mode": mode, "ok": False, "code": None, "message": None}
        try:
            if mode == "demo":
                info = describe_aweme(self._client(token=False).demo_video())
                result.update(ok=True, sample={"item_id": info["item_id"], "title": info["title"], "author": info["author"],
                                               "duration_ms": info["duration_ms"], "downloadable": info["downloadable"],
                                               "candidates": len(info["candidates"]),
                                               "codec": info["candidates"][0]["codec"] if info["candidates"] else None})
            else:
                if not self.configured:
                    raise TikHubError("tikhub_not_configured")
                result.update(ok=True, account=self._client().account())
        except (TikHubError, NetworkDownloadError) as error:
            result.update(code=error.code, message=str(error))
        with self._lock:
            ledger = self._ledger()
            ledger["last_check"] = result
            write_json(self._ledger_path(), ledger)
        return {**result, "status": self.status()}

    # ----- previews -----
    def _remember(self, preview: dict) -> dict:
        with self._lock:
            deadline = self._clock() - PREVIEW_SECONDS
            for key in [key for key, value in self._previews.items() if value["created"] < deadline]:
                del self._previews[key]
            self._previews[preview["id"]] = preview
            while len(self._previews) > MAX_PREVIEWS:
                self._previews.popitem(last=False)
        return self._public_preview(preview)

    def _public_item(self, info: dict) -> dict:
        existing = self.library.find_item("douyin", item_id=info["item_id"])
        item = {key: info[key] for key in ("item_id", "title", "author", "duration_ms", "published_at", "kind",
                                           "downloadable", "reason", "cover_url")}
        item.update(reason_message=unavailable_message(info["reason"]) if info["reason"] else None,
                    in_library=existing is not None, source_id=existing["id"] if existing else None,
                    has_media=bool(existing and existing.get("has_media")))
        return item

    def _public_preview(self, preview: dict) -> dict:
        return {"preview_id": preview["id"], "kind": preview["kind"], "creator": preview.get("creator"),
                "items": [self._public_item(preview["items"][item_id]) for item_id in preview["order"]],
                "has_more": preview.get("has_more", False), "next_cursor": preview.get("next_cursor"),
                "requests_used": preview["requests_used"], "estimated_cost_usd": self.estimate(preview["requests_used"]),
                "usage": self.usage()}

    def _cache_media(self, info: dict) -> None:
        if not info["candidates"]:
            return
        current = self._clock()
        expires = info["media_expires_at"]
        deadline = min(expires - 120, current + MEDIA_SECONDS) if expires else current + 20 * 60
        with self._lock:
            self._media[info["item_id"]] = {"candidates": deepcopy(info["candidates"]), "expires": deadline,
                                            "title": info["title"], "author": info["author"]}
            self._media.move_to_end(info["item_id"])
            while len(self._media) > MAX_CACHED_MEDIA:
                self._media.popitem(last=False)

    def _fresh_media(self, item_id: str | None) -> dict | None:
        with self._lock:
            entry = self._media.get(item_id) if item_id else None
            if entry and entry["expires"] > self._clock() + 60:
                return entry
            if entry:
                del self._media[item_id]
            return None

    def resolve(self, text: str, confirm_paid: bool = False) -> dict:
        """One share link. Known posts are answered from the library without a request."""
        try:
            link = parse_video_link(text)
            known = self.library.find_item("douyin", item_id=link["item_id"], alias=link["alias"])
            if known and known.get("platform_item_id") and known.get("provider") == "tikhub":
                video = bool(known.get("supported"))
                info = {"item_id": known["platform_item_id"], "title": known["title"], "author": known.get("author") or "",
                        "duration_ms": known.get("duration_ms"), "published_at": None, "kind": "video" if video else "images",
                        "downloadable": video, "reason": None if video else "image_post", "cover_url": None,
                        "candidates": [], "media_expires_at": None, "canonical_url": known["canonical_url"]}
                requests_used = 0
            else:
                with self._paid(1, confirm_paid) as client:
                    aweme = client.video(link["item_id"]) if link["item_id"] else client.video_by_share_url(link["share_url"])
                info, requests_used = describe_aweme(aweme), 1
                self._cache_media(info)
        except (TikHubError, NetworkDownloadError, WorkspaceError) as error:
            raise _failure(error) from None
        return self._remember({"id": uuid4().hex, "kind": "video", "created": self._clock(),
                               "items": {info["item_id"]: info}, "order": [info["item_id"]],
                               "aliases": {info["item_id"]: link["alias"]}, "urls": {info["item_id"]: link["url"]},
                               "requests_used": requests_used})

    def creator(self, text: str, cursor: int = 0, confirm_paid: bool = False) -> dict:
        """One page (at most 20) of a creator's public posts."""
        try:
            link = parse_creator_link(text)
            paid = 1 if link["sec_user_id"] else 2
            with self._paid(paid, confirm_paid) as client:
                sec_user_id = link["sec_user_id"] or client.creator_id(link["share_url"])
                page = client.creator_page(sec_user_id, cursor)
        except (TikHubError, NetworkDownloadError, WorkspaceError) as error:
            raise _failure(error) from None
        items: dict[str, dict] = {}
        for post in page["items"]:
            try:
                info = describe_aweme(post)
            except TikHubError:
                continue  # A malformed entry never blocks the rest of the page.
            if info["item_id"] not in items:
                items[info["item_id"]] = info
                self._cache_media(info)
        nickname = next((info["author"] for info in items.values() if info["author"]), "")
        return self._remember({"id": uuid4().hex, "kind": "creator", "created": self._clock(), "items": items,
                               "order": list(items), "aliases": {}, "urls": {},
                               "creator": {"sec_user_id": sec_user_id, "nickname": nickname,
                                           "url": canonical_creator_url(sec_user_id)},
                               "has_more": page["has_more"], "next_cursor": page["next_cursor"], "requests_used": paid})

    def import_items(self, preview_id: str, item_ids: list[str]) -> dict:
        """Add the user's selection to the library. No provider request is sent."""
        with self._lock:
            preview = self._previews.get(preview_id)
            if preview is None or preview["created"] < self._clock() - PREVIEW_SECONDS:
                raise WorkspaceError("tikhub_preview_expired", "预览已过期，请重新解析后再选择。", 404)
        selected = list(dict.fromkeys(item_ids))
        if not 1 <= len(selected) <= PAGE_SIZE or any(item_id not in preview["items"] for item_id in selected):
            raise WorkspaceError("invalid_batch", "请选择预览列表中的 1–20 个作品。")
        entries = []
        for item_id in selected:
            info = preview["items"][item_id]
            entries.append({"platform": "douyin", "item_id": item_id, "canonical_url": info["canonical_url"],
                            "url": preview["urls"].get(item_id) or info["canonical_url"],
                            "alias": preview["aliases"].get(item_id), "title": info["title"], "author": info["author"],
                            "duration_ms": info["duration_ms"], "supported": info["kind"] == "video", "provider": "tikhub"})
        return self.library.import_provider_items(entries)

    # ----- downloads -----
    def download_cost(self, source: dict) -> int:
        """Paid requests a download of this source would need right now."""
        item_id = source.get("platform_item_id") or offline_item_id(source["canonical_url"])
        return 0 if self._fresh_media(item_id) else 1

    def media_for(self, source: dict, *, paid_allowed: bool) -> tuple[dict, int]:
        """Signed addresses for the isolated downloader, plus requests used."""
        item_id = source.get("platform_item_id") or offline_item_id(source["canonical_url"])
        cached = self._fresh_media(item_id)
        requests_used = 0
        if cached is None:
            try:
                with self._paid(1, paid_allowed) as client:
                    requests_used = 1
                    aweme = client.video(item_id) if item_id else client.video_by_share_url(source["canonical_url"])
                info = describe_aweme(aweme)
            except (TikHubError, NetworkDownloadError, WorkspaceError) as error:
                failure = _failure(error)
                failure.tikhub_requests = requests_used
                raise failure from None
            try:
                self.library.attach_provider_item(source["id"], info)
                if not info["downloadable"]:
                    raise WorkspaceError(info["reason"], unavailable_message(info["reason"]))
            except WorkspaceError as error:
                error.tikhub_requests = requests_used
                raise
            self._cache_media(info)
            cached = self._fresh_media(info["item_id"])
            if cached is None:
                failure = WorkspaceError("no_playable_media", unavailable_message("no_playable_media"))
                failure.tikhub_requests = requests_used
                raise failure
        media = {"candidates": [{"url": candidate["url"]} for candidate in cached["candidates"]],
                 "title": cached["title"], "author": cached["author"]}
        return media, requests_used
