"""Synthetic TikHub payloads shaped like the free demo response (2026-11-16).

Identifiers, names and hosts are invented; no creator data or signed address
from a real response is kept in the repository.
"""
from __future__ import annotations

TOKEN = "tikhub-test-token-DO-NOT-LEAK-0123456789"
SEC_USER_ID = "MS4wLjABAAAA" + "s" * 43
ITEM_ID = "7000000000000000001"


def address(key: str, *, width: int = 720, height: int = 1280, size: int = 1_000_000, urls: list[str] | None = None) -> dict:
    return {"url_key": key, "uri": "v0000synthetic", "width": width, "height": height, "data_size": size,
            "file_hash": "0" * 32,
            "url_list": urls if urls is not None else [
                f"https://v1.media.example.com/{key}/?a=1128&mime_type=video_mp4",
                f"https://v2.media.example.com/{key}/?a=1128&mime_type=video_mp4",
                f"https://play.media.example.com/aweme/v1/play/?video_id=v0000synthetic&file_id={key}&sign=synthetic"]}


def rate(key: str, bit_rate: int, *, codec: int = 0, **kwargs) -> dict:
    return {"gear_name": key, "bit_rate": bit_rate, "is_h265": codec, "is_bytevc1": codec, "format": "mp4",
            "play_addr": address(key, **kwargs)}


def aweme(item_id: str = ITEM_ID, **overrides) -> dict:
    video = {"duration": 29400, "width": 720, "height": 1280, "format": "mp4", "is_h265": 0,
             "cdn_url_expired": 4102444800,
             "play_addr": address("v0000_h264_540p_1000", width=576, height=1024),
             "play_addr_h264": address("v0000_h264_540p_1000", width=576, height=1024),
             "play_addr_265": address("v0000_bytevc1_1080p_900", width=1080, height=1920),
             "bit_rate": [rate("v0000_bytevc1_1080p_900", 900, codec=1, width=1080, height=1920),
                          rate("v0000_bytevc2_720p_800", 800, codec=2),
                          rate("v0000_h264_1080p_4000", 4000, width=1080, height=1920),
                          rate("v0000_h264_720p_2900", 2900),
                          rate("v0000_h264_720p_2000", 2000),
                          rate("v0000_h264_540p_1000", 1000, width=576, height=1024)],
             "cover": {"url_list": ["https://p1.image.example.com/cover~tplv.heic?x-expires=1&x-signature=s",
                                    "https://p1.image.example.com/cover~tplv.jpeg?x-expires=1&x-signature=s"]}}
    value = {"aweme_id": item_id, "desc": "合成演示作品 #测试\n第二行", "item_title": "", "create_time": 1754295391,
             "aweme_type": 0, "duration": 29400, "images": None,
             "author": {"uid": "100", "sec_uid": SEC_USER_ID, "nickname": "合成作者", "unique_id": "synthetic"},
             "status": {"is_delete": False, "is_private": False, "private_status": 0, "is_prohibited": False, "part_see": 0},
             "video": video}
    value.update(overrides)
    return value


def envelope(data, **extra) -> dict:
    return {"code": 200, "request_id": "00000000-0000-0000-0000-000000000000",
            "message": "Request successful. This request will incur a charge.", "router": "/synthetic",
            "params": {}, "data": data, **extra}


def detail(item_id: str = ITEM_ID, **overrides) -> dict:
    return envelope({"status_code": 0, "aweme_detail": aweme(item_id, **overrides)})


def filtered(reason) -> dict:
    return envelope({"status_code": 0, "aweme_detail": None,
                     "filter_list": [{"aweme_id": ITEM_ID, "reason": reason, "detail_msg": "PROVIDER-PRIVATE-TEXT"}]})


def creator_page(count: int = 3, *, start: int = 1, has_more: int = 0, max_cursor: int = 1754000000000) -> dict:
    return envelope({"status_code": 0, "has_more": has_more, "max_cursor": max_cursor,
                     "aweme_list": [aweme(str(7000000000000000000 + start + index), desc=f"合成作品 {start + index}")
                                    for index in range(count)]})


def account() -> dict:
    return {"code": 200, "router": "/api/v1/tikhub/user/get_user_info",
            "api_key_data": {"api_key_name": "PRIVATE-KEY-NAME", "api_key_scopes": ["/api/v1/"],
                             "created_at": "2026-01-01T00:00:00", "expires_at": None, "api_key_status": 1},
            "user_data": {"email": "private-owner@example.com", "balance": 1.5, "free_credit": 0.25,
                          "email_verified": True, "account_disabled": False, "is_active": True}}


def unauthorized() -> dict:
    return {"detail": {"code": 401, "message": "API token is invalid PROVIDER-PRIVATE-TEXT", "router": "/synthetic"}}
