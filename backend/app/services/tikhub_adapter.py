"""TikHub data-provider adapter for public Douyin posts.

This is the only module that talks to TikHub. The API token travels in the
Authorization header of HTTPS requests to an official TikHub host and nowhere
else: never in a URL, log line, error message, stored record or browser
response. The adapter never reads, requests or uploads Douyin cookies and does
not call any endpoint that needs one (private favourites are out of scope).

Provider payloads are mapped to plain dictionaries for the network library.
Signed media addresses are returned to the caller for immediate use; they are
not source metadata and must not be persisted.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from typing import Callable
from urllib.parse import parse_qsl, urlsplit

import httpx

from app.services.network_download import (MAX_BYTES, MAX_DURATION_MS, NetworkDownloadError,
                                           _safe_text, _validate_url, parse_sources)

DEFAULT_BASE_URL = "https://api.tikhub.io"
# TikHub documents this as the primary domain for all requests; the token is never sent elsewhere.
OFFICIAL_HOSTS = frozenset({"api.tikhub.io"})
PAGE_SIZE = 20
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_CANDIDATES = 6
MAX_SHORT_SIDE = 720
PRICES_CHECKED_ON = "2026-11-16"

_MESSAGES = {
    "tikhub_not_configured": "尚未配置 TikHub Token；请在本机 backend/.env 设置 TIKHUB_API_KEY 后重启后端。",
    "tikhub_invalid_endpoint": "TikHub 服务地址配置无效；只允许官方 HTTPS 地址。",
    "tikhub_unauthorized": "TikHub 拒绝了当前 Token（无效、过期或已停用）；请在 TikHub 后台检查后更新本机配置。",
    "tikhub_payment_required": "TikHub 账户余额或免费额度不足；本次没有取得数据。",
    "tikhub_forbidden": "当前 TikHub Token 没有此接口的权限，或账户尚未完成邮箱验证。",
    "tikhub_rate_limited": "TikHub 请求过于频繁，请稍后重试。",
    "tikhub_bad_request": "TikHub 无法识别这个链接或作品编号，请检查后重试。",
    "tikhub_unavailable": "TikHub 服务暂时不可用或响应超时，请稍后重试。",
    "tikhub_network_error": "无法连接 TikHub，请检查本机网络或代理后重试。",
    "tikhub_invalid_response": "TikHub 返回了无法识别的数据结构；没有保存任何内容。",
    "private_content": "作者已将该作品设为私密；需要登录或权限才能查看。本工具不会获取账号或 Cookie。",
    "restricted_content": "该作品仅部分用户可见；需要登录或权限才能查看。本工具不会获取账号或 Cookie。",
    "content_unavailable": "作品不存在、已删除，或因地区与版权限制无法取得。",
    "image_post": "这是图文作品，没有可下载的视频文件；来源已保留。",
    "no_playable_media": "TikHub 没有返回可在本机播放的视频地址；来源已保留。",
    "not_douyin_video": "请粘贴一条抖音视频分享文字或链接。",
    "not_douyin_creator": "请粘贴一位抖音博主的主页链接。",
    "single_link_only": "一次只解析一条链接；请逐条粘贴，或使用“保存链接”批量收藏。",
}
_HTTP_CODES = {400: "tikhub_bad_request", 404: "tikhub_bad_request", 422: "tikhub_bad_request",
               401: "tikhub_unauthorized", 402: "tikhub_payment_required", 403: "tikhub_forbidden",
               429: "tikhub_rate_limited"}
# $.data.filter_list[0].reason, as documented for the single-post endpoints.
_FILTER_REASONS = {5: "private_content", 10: "restricted_content", 8: "content_unavailable"}
_IMAGE_POST_TYPES = {2, 68}
_ITEM_ID = re.compile(r"\d{6,25}")
_SEC_USER_ID = re.compile(r"[A-Za-z0-9_.-]{20,200}")
_DOUYIN_HOSTS = {"douyin.com", "www.douyin.com", "m.douyin.com", "iesdouyin.com", "www.iesdouyin.com"}


class TikHubError(Exception):
    """A provider failure with a fixed, user-safe message.

    Messages never interpolate provider text, URLs or credentials.
    """
    def __init__(self, code: str, *, http_status: int | None = None):
        self.code = code if code in _MESSAGES else "tikhub_invalid_response"
        self.http_status = http_status
        super().__init__(_MESSAGES[self.code])


@dataclass(frozen=True)
class Endpoint:
    path: str
    cost_usd: float  # Public list price on PRICES_CHECKED_ON; an estimate, not an invoice.
    auth: bool = True


ENDPOINTS = {
    "demo_video": Endpoint("/api/v1/demo/douyin/app/fetch_one_video", 0.0, auth=False),
    "account": Endpoint("/api/v1/tikhub/user/get_user_info", 0.0),
    "video_by_id": Endpoint("/api/v1/douyin/app/v3/fetch_one_video", 0.001),
    "video_by_share_url": Endpoint("/api/v1/douyin/app/v3/fetch_one_video_by_share_url", 0.001),
    "creator_videos": Endpoint("/api/v1/douyin/app/v3/fetch_user_post_videos", 0.001),
    "creator_id": Endpoint("/api/v1/douyin/web/get_sec_user_id", 0.001),
}


def validate_base_url(value: str) -> str:
    try:
        parts = urlsplit(value.strip())
        host = (parts.hostname or "").lower()
        port = parts.port
    except (AttributeError, ValueError):
        raise TikHubError("tikhub_invalid_endpoint") from None
    if (parts.scheme != "https" or host not in OFFICIAL_HOSTS or port not in (None, 443)
            or parts.username is not None or parts.password is not None
            or parts.path not in ("", "/") or parts.query or parts.fragment):
        raise TikHubError("tikhub_invalid_endpoint")
    return "https://" + host


def canonical_video_url(item_id: str) -> str:
    return "https://www.douyin.com/video/" + item_id


def canonical_creator_url(sec_user_id: str) -> str:
    return "https://www.douyin.com/user/" + sec_user_id


def _douyin_link(url: str) -> dict | None:
    """Classify one already validated URL without any network access."""
    parts = urlsplit(url)
    host = (parts.hostname or "").rstrip(".").lower()
    if host == "v.douyin.com":
        match = re.fullmatch(r"/([A-Za-z0-9_-]{4,32})/?", parts.path)
        # Tracking parameters are dropped before the short link leaves this machine.
        return {"type": "short", "share_url": f"https://v.douyin.com/{match[1]}/"} if match else None
    if host not in _DOUYIN_HOSTS:
        return None
    match = re.fullmatch(r"/(?:share/)?(?:video|note)/(\d{6,25})/?", parts.path)
    if match:
        return {"type": "video", "item_id": match[1]}
    modal = dict(parse_qsl(parts.query)).get("modal_id", "")
    if _ITEM_ID.fullmatch(modal):
        return {"type": "video", "item_id": modal}
    match = re.fullmatch(r"/(?:share/)?user/([A-Za-z0-9_.-]{20,200})/?", parts.path)
    if match and match[1] != "self":
        return {"type": "creator", "sec_user_id": match[1]}
    return None


def offline_item_id(url: str) -> str | None:
    """Douyin post id when the link itself contains it; short links return None."""
    try:
        link = _douyin_link(url)
    except ValueError:
        return None
    return link["item_id"] if link and link["type"] == "video" else None


def _single_link(text: str, wanted: set[str], missing: str) -> tuple[dict, dict]:
    # parse_sources applies the library's URL safety rules; it never resolves DNS.
    sources = parse_sources(text)
    found = [(source, link) for source in sources if (link := _douyin_link(source["url"]))]
    usable = [(source, link) for source, link in found if link["type"] in wanted]
    if not usable:
        raise TikHubError(missing)
    if len(usable) > 1:
        raise TikHubError("single_link_only")
    return usable[0]


def parse_video_link(text: str) -> dict:
    """One Douyin post link from share text: item id when offline-known, else a short link."""
    source, link = _single_link(text, {"video", "short"}, "not_douyin_video")
    return {"item_id": link.get("item_id"), "share_url": link.get("share_url"),
            "alias": source["canonical_url"], "url": source["url"]}


def parse_creator_link(text: str) -> dict:
    """One Douyin profile link: sec_user_id when offline-known, else a short link."""
    source, link = _single_link(text, {"creator", "short"}, "not_douyin_creator")
    return {"sec_user_id": link.get("sec_user_id"), "share_url": link.get("share_url"), "url": source["url"]}


def _integer(value) -> int:
    return value if type(value) is int and value >= 0 else 0


def _codec(address: dict, flag=None) -> str:
    key = str(address.get("url_key") or "")
    for marker, codec in (("_h264_", "h264"), ("_bytevc1_", "h265"), ("_bytevc2_", "h266")):
        if marker in key:
            return codec
    return {0: "h264", 1: "h265"}.get(flag, "other") if type(flag) is int else "other"


def _safe_media_url(url) -> bool:
    if not isinstance(url, str) or len(url) > 2048:
        return False
    try:
        _validate_url(url)
    except NetworkDownloadError:
        return False
    return True


def media_candidates(video: dict) -> tuple[list[dict], int | None]:
    """Progressive MP4 addresses, most broadly playable first.

    H.264 at or below 720p is preferred because the built-in browser player and
    the local integrity check both decode it everywhere. H.265 is a fallback;
    newer codecs that browsers cannot play are never selected.
    """
    if not isinstance(video, dict):
        return [], None
    options, seen = [], set()

    def add(address, codec: str, bitrate: int) -> None:
        if not isinstance(address, dict) or codec not in {"h264", "h265"}:
            return
        urls = [url for url in address.get("url_list") or [] if _safe_media_url(url)]
        identity = str(address.get("url_key") or address.get("file_hash") or (urls[0] if urls else ""))
        size = _integer(address.get("data_size"))
        if not urls or identity in seen or size > MAX_BYTES:
            return
        seen.add(identity)
        width, height = _integer(address.get("width")), _integer(address.get("height"))
        options.append({"urls": urls, "codec": codec, "size": size, "bitrate": bitrate,
                        "short_side": min(width, height) if width and height else 0})

    for rate in video.get("bit_rate") or []:
        if isinstance(rate, dict) and isinstance(rate.get("play_addr"), dict) and str(rate.get("format") or "mp4").lower() == "mp4":
            add(rate["play_addr"], _codec(rate["play_addr"], rate.get("is_bytevc1", rate.get("is_h265"))),
                _integer(rate.get("bit_rate")))
    add(video.get("play_addr_h264"), "h264", 0)
    primary = video.get("play_addr")
    if isinstance(primary, dict):
        add(primary, _codec(primary, video.get("is_bytevc1", video.get("is_h265"))), 0)
    add(video.get("play_addr_265"), "h265", 0)

    def rank(option: dict):
        side = option["short_side"]
        tier = 1 if not side else 0 if side <= MAX_SHORT_SIDE else 2
        return (0 if option["codec"] == "h264" else 1, tier, side if tier == 2 else -side, -option["bitrate"])

    candidates = []
    for position, option in enumerate(sorted(options, key=rank)):
        for url in option["urls"][:3 if position == 0 else 2]:
            candidates.append({"url": url, "codec": option["codec"], "short_side": option["short_side"], "size": option["size"]})
            if len(candidates) == MAX_CANDIDATES:
                break
        if len(candidates) == MAX_CANDIDATES:
            break
    expires = video.get("cdn_url_expired")
    return candidates, expires if type(expires) is int and expires > 0 else None


def _cover(video: dict) -> str | None:
    for key in ("cover", "origin_cover"):
        address = video.get(key)
        for url in (address.get("url_list") or []) if isinstance(address, dict) else []:
            if _safe_media_url(url) and url.startswith("https://") and ".heic" not in url.split("?", 1)[0]:
                return url
    return None


def describe_aweme(aweme: dict) -> dict:
    """Map one provider post to the library's vocabulary plus in-memory media."""
    if not isinstance(aweme, dict):
        raise TikHubError("tikhub_invalid_response")
    item_id = str(aweme.get("aweme_id") or "")
    if not _ITEM_ID.fullmatch(item_id):
        raise TikHubError("tikhub_invalid_response")
    author = aweme.get("author") if isinstance(aweme.get("author"), dict) else {}
    video = aweme.get("video") if isinstance(aweme.get("video"), dict) else {}
    status = aweme.get("status") if isinstance(aweme.get("status"), dict) else {}
    title = ""
    for key in ("item_title", "desc", "preview_title"):
        value = aweme.get(key)
        # Line breaks become spaces before control characters are removed.
        title = _safe_text(" ".join(value.split()) if isinstance(value, str) else None, "")[:256]
        if title:
            break
    duration = _integer(aweme.get("duration")) or _integer(video.get("duration"))
    kind = "images" if aweme.get("images") or aweme.get("image_post_info") or aweme.get("aweme_type") in _IMAGE_POST_TYPES else "video"
    candidates, expires = media_candidates(video) if kind == "video" else ([], None)
    if status.get("is_delete") or status.get("is_prohibited"):
        reason = "content_unavailable"
    elif status.get("is_private") or status.get("private_status") == 1:
        reason = "private_content"
    elif status.get("private_status") == 2 or status.get("part_see") == 1:
        reason = "restricted_content"
    elif kind == "images":
        reason = "image_post"
    elif not candidates:
        reason = "no_playable_media"
    elif duration > MAX_DURATION_MS:
        reason = "duration_exceeded"
    else:
        reason = None
    created = aweme.get("create_time")
    try:
        published = datetime.fromtimestamp(created, timezone.utc).isoformat() if type(created) is int and created > 0 else None
    except (OverflowError, OSError, ValueError):
        published = None
    sec_uid = author.get("sec_uid")
    return {"item_id": item_id, "canonical_url": canonical_video_url(item_id), "platform": "douyin",
            "title": title or "抖音视频", "author": _safe_text(author.get("nickname"), "")[:256],
            "author_id": sec_uid if isinstance(sec_uid, str) and _SEC_USER_ID.fullmatch(sec_uid) else None,
            "duration_ms": duration or None, "published_at": published, "kind": kind,
            "downloadable": reason is None, "reason": reason, "cover_url": _cover(video),
            "candidates": candidates if reason is None else [], "media_expires_at": expires}


def unavailable_message(code: str) -> str:
    return _MESSAGES.get(code) or NetworkDownloadError(code).message


class TikHubClient:
    """Minimal synchronous client; one HTTP request per call and no retries.

    Each request is reported to ``on_request`` so callers can count requests and
    estimate cost. Nothing here logs; errors carry fixed messages only.
    """
    def __init__(self, token: str = "", *, base_url: str = DEFAULT_BASE_URL, timeout: float = 45.0,
                 transport: httpx.BaseTransport | None = None, on_request: Callable[[dict], None] | None = None):
        self._token = token.strip() if isinstance(token, str) else ""
        self._base_url = validate_base_url(base_url)
        self._timeout = timeout
        self._transport = transport
        self._on_request = on_request

    def __repr__(self) -> str:
        return f"<TikHubClient host={urlsplit(self._base_url).hostname} configured={self.configured}>"

    @property
    def configured(self) -> bool:
        return bool(self._token)

    def _call(self, name: str, params: dict | None = None) -> dict:
        endpoint = ENDPOINTS[name]
        if endpoint.auth and not self._token:
            raise TikHubError("tikhub_not_configured")
        headers = {"Accept": "application/json", "User-Agent": "DouyinMind/TikHubAdapter"}
        if endpoint.auth:
            headers["Authorization"] = "Bearer " + self._token
        record = {"endpoint": name, "paid": endpoint.cost_usd > 0, "ok": False, "cost_usd": 0.0,
                  "http_status": None, "code": None}
        try:
            try:
                # Redirects are never followed: the token must not reach another host.
                with httpx.Client(timeout=self._timeout, follow_redirects=False, transport=self._transport) as client:
                    with client.stream("GET", self._base_url + endpoint.path, params=params, headers=headers) as response:
                        record["http_status"] = status = response.status_code
                        body = bytearray()
                        for chunk in response.iter_bytes():
                            body += chunk
                            if len(body) > MAX_RESPONSE_BYTES:
                                raise TikHubError("tikhub_invalid_response", http_status=status)
            except httpx.TimeoutException:
                raise TikHubError("tikhub_unavailable") from None
            except httpx.HTTPError:
                raise TikHubError("tikhub_network_error") from None
            if status != 200:
                code = _HTTP_CODES.get(status) or ("tikhub_unavailable" if status >= 500 else "tikhub_invalid_response")
                raise TikHubError(code, http_status=status)
            try:
                payload = json.loads(body)
            except (ValueError, UnicodeError):
                raise TikHubError("tikhub_invalid_response", http_status=status) from None
            if not isinstance(payload, dict):
                raise TikHubError("tikhub_invalid_response", http_status=status)
            inner = payload.get("code")
            if inner != 200:
                code = _HTTP_CODES.get(inner) or ("tikhub_unavailable" if type(inner) is int and inner >= 500 else "tikhub_invalid_response")
                raise TikHubError(code, http_status=status)
            # TikHub bills successful requests; other outcomes are recorded as unbilled.
            record.update(ok=True, cost_usd=endpoint.cost_usd)
            return payload
        except TikHubError as error:
            record["code"] = error.code
            raise
        finally:
            if self._on_request is not None:
                self._on_request(record)

    @staticmethod
    def _post(payload: dict) -> dict:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise TikHubError("tikhub_invalid_response")
        detail = data.get("aweme_detail")
        if isinstance(detail, dict) and detail.get("aweme_id"):
            return detail
        filters = data.get("filter_list")
        entry = filters[0] if isinstance(filters, list) and filters and isinstance(filters[0], dict) else {}
        try:
            reason = int(entry.get("reason"))
        except (TypeError, ValueError):
            reason = None
        raise TikHubError(_FILTER_REASONS.get(reason, "content_unavailable"))

    def demo_video(self) -> dict:
        """TikHub's free fixed sample post; no token is sent."""
        return self._post(self._call("demo_video"))

    def account(self) -> dict:
        payload = self._call("account")
        key = payload.get("api_key_data") if isinstance(payload.get("api_key_data"), dict) else {}
        user = payload.get("user_data") if isinstance(payload.get("user_data"), dict) else {}
        number = lambda value: float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
        scopes = [scope for scope in key.get("api_key_scopes") or [] if isinstance(scope, str)]
        missing = [name for name, endpoint in ENDPOINTS.items() if endpoint.auth and scopes
                   and not any(endpoint.path.startswith(scope) for scope in scopes)]
        expires = key.get("expires_at")
        # The account e-mail address and key name are deliberately not returned.
        return {"balance_usd": number(user.get("balance")), "free_credit_usd": number(user.get("free_credit")),
                "email_verified": bool(user.get("email_verified")),
                "account_active": bool(user.get("is_active")) and not user.get("account_disabled"),
                "key_active": key.get("api_key_status") == 1,
                "key_expires_at": _safe_text(expires, "")[:40] or None,
                "missing_scopes": missing}

    def video(self, item_id: str) -> dict:
        if not isinstance(item_id, str) or not _ITEM_ID.fullmatch(item_id):
            raise TikHubError("tikhub_bad_request")
        return self._post(self._call("video_by_id", {"aweme_id": item_id}))

    def video_by_share_url(self, share_url: str) -> dict:
        link = _douyin_link(share_url) if isinstance(share_url, str) else None
        if not link or link["type"] != "short":
            raise TikHubError("tikhub_bad_request")
        return self._post(self._call("video_by_share_url", {"share_url": link["share_url"]}))

    def creator_id(self, share_url: str) -> str:
        link = _douyin_link(share_url) if isinstance(share_url, str) else None
        if not link or link["type"] != "short":
            raise TikHubError("tikhub_bad_request")
        data = self._call("creator_id", {"url": link["share_url"]}).get("data")
        value = data.get("sec_user_id") if isinstance(data, dict) else data
        if not isinstance(value, str) or not _SEC_USER_ID.fullmatch(value):
            raise TikHubError("not_douyin_creator")
        return value

    def creator_page(self, sec_user_id: str, cursor: int = 0) -> dict:
        """One page of a creator's public posts, at most PAGE_SIZE entries."""
        if not isinstance(sec_user_id, str) or not _SEC_USER_ID.fullmatch(sec_user_id) or type(cursor) is not int or cursor < 0:
            raise TikHubError("tikhub_bad_request")
        data = self._call("creator_videos", {"sec_user_id": sec_user_id, "max_cursor": cursor, "count": PAGE_SIZE,
                                             "sort_type": 0, "channel": "normal"}).get("data")
        if not isinstance(data, dict):
            raise TikHubError("tikhub_invalid_response")
        posts = [post for post in data.get("aweme_list") or [] if isinstance(post, dict)][:PAGE_SIZE]
        following = data.get("max_cursor")
        more = bool(data.get("has_more")) and type(following) is int and following > cursor
        return {"items": posts, "has_more": more, "next_cursor": following if more else None}
