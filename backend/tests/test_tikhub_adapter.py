"""TikHub adapter: offline link handling, payload mapping and token confinement.

Every HTTP exchange uses an in-memory transport. No test contacts TikHub, and
payloads are synthetic stand-ins for the documented response structure.
"""
from __future__ import annotations

import json
import socket

import httpx
import pytest

from app.services import tikhub_adapter as adapter
from app.services.network_download import MAX_BYTES, MAX_DURATION_MS, NetworkDownloadError
from tests import tikhub_samples as samples
from tests.tikhub_samples import ITEM_ID, SEC_USER_ID, TOKEN


class Upstream:
    """Records requests and answers by path."""
    def __init__(self, routes: dict | None = None):
        self.routes = routes or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.routes.get(request.url.path, (404, {"detail": {"code": 404}}))
        if callable(answer):
            return answer(request)
        status, body = answer
        return httpx.Response(status, content=body if isinstance(body, bytes) else json.dumps(body).encode())


def client(routes, *, token=TOKEN, **kwargs):
    upstream = Upstream(routes)
    return adapter.TikHubClient(token, transport=httpx.MockTransport(upstream), **kwargs), upstream


def path(name: str) -> str:
    return adapter.ENDPOINTS[name].path


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: pytest.fail("adapter tests must stay offline"))


@pytest.mark.parametrize(("text", "item_id", "share_url"), [
    ("https://www.douyin.com/video/7000000000000000001", ITEM_ID, None),
    ("https://www.douyin.com/video/7000000000000000001/?previous_page=app_code_link", ITEM_ID, None),
    ("https://www.iesdouyin.com/share/video/7000000000000000001/?region=US&mid=1&u_code=-1", ITEM_ID, None),
    ("https://www.douyin.com/user/" + SEC_USER_ID + "?modal_id=7000000000000000001", ITEM_ID, None),
    ("https://www.douyin.com/note/7000000000000000001", ITEM_ID, None),
    ("3.05 复制打开抖音，看看【合成作者的作品】 https://v.douyin.com/TestDouyinA/ a@B.cd 01/02", None,
     "https://v.douyin.com/TestDouyinA/"),
    ("https://v.douyin.com/TestDouyinA/?utm_source=copy&share_tag=x", None, "https://v.douyin.com/TestDouyinA/"),
])
def test_video_links_are_classified_offline_and_tracking_is_dropped(text, item_id, share_url):
    link = adapter.parse_video_link(text)
    assert link["item_id"] == item_id and link["share_url"] == share_url
    assert link["alias"].startswith("https://")


def test_long_and_share_forms_of_one_post_name_the_same_platform_id():
    forms = ["https://www.douyin.com/video/7000000000000000001", "https://www.douyin.com/video/7000000000000000001/",
             "https://www.iesdouyin.com/share/video/7000000000000000001/?from_ssr=1",
             "https://www.douyin.com/discover?modal_id=7000000000000000001"]
    assert {adapter.offline_item_id(url) for url in forms} == {ITEM_ID}
    assert adapter.offline_item_id("https://v.douyin.com/TestDouyinA/") is None
    assert adapter.offline_item_id("https://www.youtube.com/watch?v=TestVideo01") is None
    assert adapter.canonical_video_url(ITEM_ID) == "https://www.douyin.com/video/7000000000000000001"


@pytest.mark.parametrize(("text", "code"), [
    ("https://www.youtube.com/watch?v=TestVideo01", "not_douyin_video"),
    ("https://www.douyin.com/user/" + SEC_USER_ID, "not_douyin_video"),
    ("https://v.douyin.com/TestDouyinA/ https://v.douyin.com/TestDouyinB/", "single_link_only"),
])
def test_video_parsing_refuses_other_sites_profiles_and_batches(text, code):
    with pytest.raises(adapter.TikHubError) as error:
        adapter.parse_video_link(text)
    assert error.value.code == code


@pytest.mark.parametrize("text", ["https://www.douyin.com/video/7000000000000000001?token=secret",
                                  "http://127.0.0.1/video/7000000000000000001", "no link here"])
def test_unsafe_or_missing_links_never_reach_the_provider(text):
    with pytest.raises(NetworkDownloadError) as error:
        adapter.parse_video_link(text)
    assert error.value.code in {"unsafe_url", "invalid_source"}
    assert "secret" not in str(error.value)


def test_creator_links_accept_profiles_and_short_links_but_not_the_logged_in_page():
    assert adapter.parse_creator_link("https://www.douyin.com/user/" + SEC_USER_ID + "?from_tab_name=main")["sec_user_id"] == SEC_USER_ID
    assert adapter.parse_creator_link("https://www.iesdouyin.com/share/user/" + SEC_USER_ID)["sec_user_id"] == SEC_USER_ID
    short = adapter.parse_creator_link("看看TA的主页 https://v.douyin.com/TestUserA/")
    assert short["sec_user_id"] is None and short["share_url"] == "https://v.douyin.com/TestUserA/"
    for text in ("https://www.douyin.com/user/self?from_login=1", "https://www.douyin.com/video/7000000000000000001"):
        with pytest.raises(adapter.TikHubError) as error:
            adapter.parse_creator_link(text)
        assert error.value.code == "not_douyin_creator"


def test_media_selection_prefers_browser_playable_h264_within_720p():
    candidates, expires = adapter.media_candidates(samples.aweme()["video"])
    assert expires == 4102444800
    assert len(candidates) == adapter.MAX_CANDIDATES
    # Portrait 720x1280 counts as 720p; the higher bitrate of the two 720p H.264 gears wins.
    assert [candidate["codec"] for candidate in candidates] == ["h264"] * 6
    assert candidates[0]["short_side"] == 720 and "v0000_h264_720p_2900" in candidates[0]["url"]
    assert all("bytevc2" not in candidate["url"] for candidate in candidates)
    assert all(candidate["url"].startswith("https://") for candidate in candidates)


def test_media_selection_falls_back_to_h265_and_never_selects_unplayable_codecs():
    video = {"bit_rate": [samples.rate("v0000_bytevc2_720p_800", 800, codec=2),
                          samples.rate("v0000_bytevc1_1080p_900", 900, codec=1, width=1080, height=1920)]}
    candidates, expires = adapter.media_candidates(video)
    assert expires is None
    assert {candidate["codec"] for candidate in candidates} == {"h265"}
    assert adapter.media_candidates({"bit_rate": [samples.rate("v0000_bytevc2_720p_800", 800, codec=2)]})[0] == []
    assert adapter.media_candidates(None) == ([], None)


def test_media_selection_skips_oversized_files_and_unsafe_addresses():
    video = {"bit_rate": [samples.rate("v0000_h264_720p_2900", 2900, size=MAX_BYTES + 1),
                          samples.rate("v0000_h264_540p_1000", 1000, width=576, height=1024, urls=[
                              "http://127.0.0.1/private.mp4", "https://user:password@media.example.com/a.mp4",
                              "file:///etc/passwd", "https://v1.media.example.com/ok/?a=1"])]}
    candidates, _ = adapter.media_candidates(video)
    assert [candidate["url"] for candidate in candidates] == ["https://v1.media.example.com/ok/?a=1"]


def test_post_mapping_matches_the_library_vocabulary_without_persistable_secrets():
    info = adapter.describe_aweme(samples.aweme())
    assert info["item_id"] == ITEM_ID and info["canonical_url"] == "https://www.douyin.com/video/" + ITEM_ID
    assert info["platform"] == "douyin" and info["kind"] == "video" and info["downloadable"] is True
    assert info["title"] == "合成演示作品 #测试 第二行" and info["author"] == "合成作者"
    assert info["author_id"] == SEC_USER_ID and info["duration_ms"] == 29400
    assert info["published_at"].startswith("2025-08-04T")
    assert info["cover_url"].endswith(".jpeg?x-expires=1&x-signature=s")
    assert info["reason"] is None and info["candidates"]


@pytest.mark.parametrize(("overrides", "reason", "kind"), [
    ({"images": [{"uri": "x"}]}, "image_post", "images"),
    ({"aweme_type": 68}, "image_post", "images"),
    ({"status": {"is_private": True}}, "private_content", "video"),
    ({"status": {"private_status": 2}}, "restricted_content", "video"),
    ({"status": {"is_delete": True}}, "content_unavailable", "video"),
    ({"duration": MAX_DURATION_MS + 1}, "duration_exceeded", "video"),
    ({"video": {"bit_rate": []}}, "no_playable_media", "video"),
])
def test_posts_that_cannot_be_downloaded_are_labelled_and_expose_no_addresses(overrides, reason, kind):
    info = adapter.describe_aweme(samples.aweme(**overrides))
    assert (info["reason"], info["kind"], info["downloadable"], info["candidates"]) == (reason, kind, False, [])
    assert adapter.unavailable_message(reason)


def test_post_text_is_sanitised_and_invalid_ids_are_rejected():
    info = adapter.describe_aweme(samples.aweme(desc="标题\x00\x1b[31m" + "长" * 400, author={"nickname": "作者\x07"}))
    assert "\x00" not in info["title"] and "\x1b" not in info["title"] and len(info["title"]) <= 256
    assert info["author"] == "作者" and info["author_id"] is None
    for bad in ({"aweme_id": "../../etc"}, {"aweme_id": ""}, "not-a-dict"):
        with pytest.raises(adapter.TikHubError):
            adapter.describe_aweme(samples.aweme(**bad) if isinstance(bad, dict) else bad)


def test_token_travels_only_in_the_authorization_header_of_the_official_host():
    records = []
    tikhub, upstream = client({path("video_by_id"): (200, samples.detail())}, on_request=records.append)
    assert tikhub.video(ITEM_ID)["aweme_id"] == ITEM_ID
    request = upstream.requests[0]
    assert request.url.host == "api.tikhub.io" and request.url.scheme == "https"
    assert request.headers["authorization"] == "Bearer " + TOKEN
    assert TOKEN not in str(request.url) and "cookie" not in request.headers
    assert dict(request.url.params) == {"aweme_id": ITEM_ID}
    assert TOKEN not in repr(tikhub) and TOKEN not in json.dumps(records)
    assert records == [{"endpoint": "video_by_id", "paid": True, "ok": True, "cost_usd": 0.001,
                        "http_status": 200, "code": None}]


def test_free_demo_sends_no_token_and_account_check_hides_identity():
    records = []
    tikhub, upstream = client({path("demo_video"): (200, samples.detail()), path("account"): (200, samples.account())},
                              on_request=records.append)
    assert tikhub.demo_video()["aweme_id"] == ITEM_ID
    assert "authorization" not in upstream.requests[0].headers
    account = tikhub.account()
    assert account == {"balance_usd": 1.5, "free_credit_usd": 0.25, "email_verified": True, "account_active": True,
                       "key_active": True, "key_expires_at": None, "missing_scopes": []}
    assert "private-owner" not in json.dumps(account) and "PRIVATE-KEY-NAME" not in json.dumps(account)
    assert [record["cost_usd"] for record in records] == [0.0, 0.0]
    assert [record["paid"] for record in records] == [False, False]


def test_missing_token_fails_before_any_request_and_other_hosts_are_refused():
    tikhub, upstream = client({}, token="")
    with pytest.raises(adapter.TikHubError) as error:
        tikhub.video(ITEM_ID)
    assert error.value.code == "tikhub_not_configured" and upstream.requests == []
    for url in ("http://api.tikhub.io", "https://api.tikhub.io.attacker.example", "https://attacker.example",
                "https://user:pw@api.tikhub.io", "https://api.tikhub.io:8443", "https://api.tikhub.io/v2?x=1",
                "https://api.tikhub.dev", ""):
        with pytest.raises(adapter.TikHubError) as error:
            adapter.TikHubClient(TOKEN, base_url=url)
        assert error.value.code == "tikhub_invalid_endpoint"
    assert adapter.validate_base_url("https://API.tikhub.io/") == "https://api.tikhub.io"


def test_redirects_are_not_followed_so_the_token_cannot_reach_another_host():
    tikhub, upstream = client({path("video_by_id"): lambda request: httpx.Response(
        302, headers={"Location": "https://attacker.example/steal"})})
    with pytest.raises(adapter.TikHubError) as error:
        tikhub.video(ITEM_ID)
    assert error.value.code == "tikhub_invalid_response"
    assert [request.url.host for request in upstream.requests] == ["api.tikhub.io"]


@pytest.mark.parametrize(("status", "code"), [(400, "tikhub_bad_request"), (401, "tikhub_unauthorized"),
                                              (402, "tikhub_payment_required"), (403, "tikhub_forbidden"),
                                              (404, "tikhub_bad_request"), (422, "tikhub_bad_request"),
                                              (429, "tikhub_rate_limited"), (500, "tikhub_unavailable"),
                                              (503, "tikhub_unavailable"), (418, "tikhub_invalid_response")])
def test_provider_failures_map_to_fixed_messages_and_are_recorded_unbilled(status, code):
    records = []
    tikhub, _ = client({path("video_by_id"): (status, samples.unauthorized())}, on_request=records.append)
    with pytest.raises(adapter.TikHubError) as error:
        tikhub.video(ITEM_ID)
    assert error.value.code == code and error.value.http_status == status
    assert "PROVIDER-PRIVATE-TEXT" not in str(error.value) and TOKEN not in str(error.value)
    assert records[0]["ok"] is False and records[0]["cost_usd"] == 0.0 and records[0]["code"] == code


@pytest.mark.parametrize("body", [b"<html>gateway</html>", b"[1, 2]", json.dumps({"code": 500, "data": None}).encode(),
                                  json.dumps({"code": 200, "data": "text"}).encode()])
def test_unrecognised_bodies_are_rejected_without_echoing_them(body):
    tikhub, _ = client({path("video_by_id"): (200, body)})
    with pytest.raises(adapter.TikHubError) as error:
        tikhub.video(ITEM_ID)
    assert error.value.code in {"tikhub_invalid_response", "tikhub_unavailable"}
    assert "gateway" not in str(error.value)


def test_oversized_responses_stop_at_the_limit(monkeypatch):
    monkeypatch.setattr(adapter, "MAX_RESPONSE_BYTES", 1024)
    tikhub, _ = client({path("video_by_id"): (200, b"x" * 4096)})
    with pytest.raises(adapter.TikHubError) as error:
        tikhub.video(ITEM_ID)
    assert error.value.code == "tikhub_invalid_response"


@pytest.mark.parametrize(("failure", "code"), [(httpx.ConnectTimeout("t"), "tikhub_unavailable"),
                                               (httpx.ConnectError("PRIVATE proxy detail"), "tikhub_network_error")])
def test_transport_failures_are_classified_without_details(failure, code):
    def broken(request):
        raise failure
    records = []
    tikhub, _ = client({path("video_by_id"): broken}, on_request=records.append)
    with pytest.raises(adapter.TikHubError) as error:
        tikhub.video(ITEM_ID)
    assert error.value.code == code and "PRIVATE" not in str(error.value)
    assert records[0]["http_status"] is None and records[0]["ok"] is False


@pytest.mark.parametrize(("reason", "code"), [(5, "private_content"), ("10", "restricted_content"),
                                              (8, "content_unavailable"), (None, "content_unavailable")])
def test_filtered_posts_report_permission_or_availability(reason, code):
    records = []
    tikhub, _ = client({path("video_by_id"): (200, samples.filtered(reason))}, on_request=records.append)
    with pytest.raises(adapter.TikHubError) as error:
        tikhub.video(ITEM_ID)
    assert error.value.code == code and "PROVIDER-PRIVATE-TEXT" not in str(error.value)
    # The provider answered successfully, so the request is counted as billable.
    assert records[0]["ok"] is True and records[0]["cost_usd"] == 0.001


def test_share_link_lookup_sends_only_the_clean_short_link():
    tikhub, upstream = client({path("video_by_share_url"): (200, samples.detail())})
    assert tikhub.video_by_share_url("https://v.douyin.com/TestDouyinA/?utm_source=x")["aweme_id"] == ITEM_ID
    assert dict(upstream.requests[0].url.params) == {"share_url": "https://v.douyin.com/TestDouyinA/"}
    for bad in ("https://attacker.example/x", "https://www.douyin.com/video/" + ITEM_ID, None):
        with pytest.raises(adapter.TikHubError):
            tikhub.video_by_share_url(bad)
    assert len(upstream.requests) == 1


def test_creator_page_requests_one_page_of_twenty_and_reports_the_next_cursor():
    tikhub, upstream = client({path("creator_videos"): (200, samples.creator_page(25, has_more=1))})
    page = tikhub.creator_page(SEC_USER_ID)
    assert len(page["items"]) == adapter.PAGE_SIZE == 20
    assert page["has_more"] is True and page["next_cursor"] == 1754000000000
    assert dict(upstream.requests[0].url.params) == {"sec_user_id": SEC_USER_ID, "max_cursor": "0", "count": "20",
                                                      "sort_type": "0", "channel": "normal"}
    last, _ = client({path("creator_videos"): (200, samples.creator_page(2, has_more=0))})
    assert last.creator_page(SEC_USER_ID, 1754000000000) == {"items": last.creator_page(SEC_USER_ID)["items"],
                                                              "has_more": False, "next_cursor": None}
    for bad in ("self", "../x", ""):
        with pytest.raises(adapter.TikHubError):
            tikhub.creator_page(bad)
    with pytest.raises(adapter.TikHubError):
        tikhub.creator_page(SEC_USER_ID, -1)


def test_creator_short_link_resolution_validates_the_returned_identifier():
    tikhub, upstream = client({path("creator_id"): (200, samples.envelope(SEC_USER_ID))})
    assert tikhub.creator_id("https://v.douyin.com/TestUserA/") == SEC_USER_ID
    assert dict(upstream.requests[0].url.params) == {"url": "https://v.douyin.com/TestUserA/"}
    broken, _ = client({path("creator_id"): (200, samples.envelope("<script>"))})
    with pytest.raises(adapter.TikHubError) as error:
        broken.creator_id("https://v.douyin.com/TestUserA/")
    assert error.value.code == "not_douyin_creator"


def test_no_endpoint_in_use_accepts_or_requires_a_douyin_cookie():
    assert all("cookie" not in endpoint.path and "collect" not in endpoint.path for endpoint in adapter.ENDPOINTS.values())
    assert {name for name, endpoint in adapter.ENDPOINTS.items() if endpoint.cost_usd == 0} == {"demo_video", "account"}
