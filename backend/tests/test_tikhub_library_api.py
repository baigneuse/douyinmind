"""TikHub as a library source: consent, request accounting, post-id dedupe and downloads.

The provider is an in-memory transport returning synthetic payloads and the
downloader is a stub, so nothing here proves real TikHub or CDN behaviour.
"""
from __future__ import annotations

import hashlib
import json
import socket
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
from pydantic import SecretStr
import pytest

from app.api.routes import local_media, network_media
from app.services import network_download
from app.services.local_media_workspace import LocalMediaWorkspace
from app.services.network_library import NetworkLibrary
from app.services.tikhub_adapter import ENDPOINTS
from app.services.tikhub_service import TikHubService
from tests import tikhub_samples as samples
from tests.test_network_library_api import QueueWorker, STUB_MEDIA
from tests.tikhub_samples import ITEM_ID, SEC_USER_ID, TOKEN

LONG = "https://www.douyin.com/video/" + ITEM_ID
SHORT = "https://v.douyin.com/TestDouyinA/"
PROFILE = "https://www.douyin.com/user/" + SEC_USER_ID
P = {name: endpoint.path for name, endpoint in ENDPOINTS.items()}


class Upstream:
    def __init__(self):
        self.routes = {P["video_by_id"]: (200, samples.detail()), P["video_by_share_url"]: (200, samples.detail()),
                       P["creator_videos"]: (200, samples.creator_page(3)), P["creator_id"]: (200, samples.envelope(SEC_USER_ID)),
                       P["demo_video"]: (200, samples.detail()), P["account"]: (200, samples.account())}
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        status, body = self.routes[request.url.path]
        return httpx.Response(status, content=json.dumps(body).encode())

    def paths(self):
        return [request.url.path for request in self.requests]


@pytest.fixture
def context(tmp_path, monkeypatch):
    connect = socket.socket.connect
    def local_only(sock, address):
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return connect(sock, address)
        raise AssertionError("External network is forbidden in TikHub library tests")
    monkeypatch.setattr(socket.socket, "connect", local_only)
    workspace = LocalMediaWorkspace(tmp_path / "managed")
    worker = QueueWorker()
    library = NetworkLibrary(workspace, worker=worker)
    upstream = Upstream()
    settings = SimpleNamespace(tikhub_api_key=SecretStr(TOKEN), tikhub_base_url="https://api.tikhub.io",
                               tikhub_timeout_seconds=5.0, tikhub_daily_request_limit=20)
    clock = SimpleNamespace(now=1_800_000_000.0)
    library.tikhub = TikHubService(library, configuration=settings, transport=httpx.MockTransport(upstream),
                                   clock=lambda: clock.now)
    monkeypatch.setattr(local_media, "get_workspace", lambda: workspace)
    monkeypatch.setattr(network_media, "get_network_library", lambda: library)
    app = FastAPI()
    app.include_router(network_media.router, prefix="/api")
    client = TestClient(app, base_url="http://127.0.0.1")
    downloads = []
    def stub_download(url, target_dir, *, progress, cancelled, media=None):
        downloads.append({"url": url, "media": media})
        target = target_dir / "complete.mp4"
        target.write_bytes(STUB_MEDIA)
        return {"path": target, "title": (media or {}).get("title") or "Anonymous title", "author": (media or {}).get("author") or "",
                "duration_ms": 2000, "size_bytes": len(STUB_MEDIA),
                "sha256": hashlib.sha256(STUB_MEDIA).hexdigest(), "content_type": "video/mp4"}
    monkeypatch.setattr(network_download, "download_media", stub_download)
    return SimpleNamespace(client=client, library=library, workspace=workspace, worker=worker, upstream=upstream,
                           settings=settings, clock=clock, downloads=downloads, root=tmp_path / "managed",
                           headers={"X-Local-Media-Token": workspace.session_token})


def post(context, path, body, expected=200):
    response = context.client.post("/api/network-media" + path, json=body, headers=context.headers)
    assert response.status_code == expected, response.text
    return response.json()


def resolve(context, text=LONG, **body):
    return post(context, "/tikhub/resolve", {"text": text, "confirm_paid": True, **body})


def add(context, preview, item_ids=None):
    return post(context, "/tikhub/import", {"preview_id": preview["preview_id"],
                                            "item_ids": item_ids or [item["item_id"] for item in preview["items"]]}, 201)


def download(context, source_id, expected=202, **body):
    return post(context, f"/sources/{source_id}/download", body, expected)


def status(context):
    response = context.client.get("/api/network-media/tikhub/status")
    assert response.status_code == 200, response.text
    return response.json()


def stored_text(context) -> str:
    return "\n".join(path.read_text(encoding="utf-8", errors="replace")
                     for path in context.root.rglob("*") if path.is_file() and path.suffix == ".json")


def test_status_reports_configuration_without_requests_or_token(context):
    body = status(context)
    assert body["configured"] is True and body["endpoint"] == "api.tikhub.io" and body["cookie_access"] is False
    assert body["usage"]["today"]["requests"] == 0 and body["usage"]["daily_limit"] == 20
    assert body["prices"]["per_paid_request_usd"] == 0.001 and body["page_size"] == 20
    assert context.upstream.requests == []
    assert TOKEN not in json.dumps(body)
    context.settings.tikhub_api_key = SecretStr("")
    assert status(context)["configured"] is False


def test_paid_requests_need_a_token_and_a_confirmation_for_that_action(context):
    body = post(context, "/tikhub/resolve", {"text": LONG}, 409)
    assert body["detail"]["code"] == "tikhub_confirmation_required" and "$0.001" in body["detail"]["message"]
    body = post(context, "/tikhub/creator", {"text": "https://v.douyin.com/TestUserA/"}, 409)
    assert "2 次付费请求" in body["detail"]["message"]
    context.settings.tikhub_api_key = SecretStr("  ")
    assert post(context, "/tikhub/resolve", {"text": LONG, "confirm_paid": True}, 409)["detail"]["code"] == "tikhub_not_configured"
    assert context.upstream.requests == [] and status(context)["usage"]["total"]["requests"] == 0
    assert context.client.post("/api/network-media/tikhub/resolve", json={"text": LONG, "confirm_paid": True}).status_code == 403


def test_resolving_a_link_previews_one_post_and_records_the_request(context):
    preview = resolve(context)
    assert preview["kind"] == "video" and preview["requests_used"] == 1 and preview["estimated_cost_usd"] == 0.001
    item = preview["items"][0]
    assert item["item_id"] == ITEM_ID and item["title"].startswith("合成演示作品") and item["author"] == "合成作者"
    assert item["downloadable"] is True and item["in_library"] is False and item["duration_ms"] == 29400
    assert "candidates" not in item and "media.example.com" not in json.dumps({**item, "cover_url": None})
    assert context.upstream.paths() == [P["video_by_id"]]
    assert context.library.list_sources() == []  # A preview saves nothing until the user selects it.
    usage = status(context)["usage"]
    assert usage["today"] == {"requests": 1, "succeeded": 1, "failed": 0, "paid_attempts": 1, "billable": 1,
                              "estimated_cost_usd": 0.001}
    assert usage["remaining_today"] == 19 and usage["last_request"]["endpoint"] == "video_by_id"


def test_short_and_long_links_of_one_post_become_a_single_source(context):
    first = add(context, resolve(context, "看看这个 " + SHORT + " 复制打开抖音"))
    assert (first["created"], first["duplicates"]) == (1, 0)
    source = first["items"][0]
    assert source["platform"] == "douyin" and source["provider"] == "tikhub" and source["platform_item_id"] == ITEM_ID
    assert source["id"] == hashlib.sha256(LONG.encode()).hexdigest() and source["url"] == SHORT
    assert context.upstream.paths() == [P["video_by_share_url"]]
    # Pasting either form again is answered from the library: no request, no second record.
    for text in (SHORT, LONG, LONG + "/", "https://www.iesdouyin.com/share/video/" + ITEM_ID + "/?from_ssr=1"):
        again = resolve(context, text, confirm_paid=False)
        assert again["requests_used"] == 0 and again["items"][0]["in_library"] is True
        assert again["items"][0]["source_id"] == source["id"]
        assert add(context, again)["duplicates"] == 1
    saved = post(context, "/sources", {"text": LONG + "/\n" + SHORT}, 201)
    assert (saved["created"], saved["duplicates"]) == (0, 2)
    assert len(context.upstream.requests) == 1 and len(context.library.list_sources()) == 1


def test_a_link_saved_earlier_is_upgraded_in_place_and_keeps_its_notes(context):
    legacy = post(context, "/sources", {"text": SHORT}, 201)["items"][0]
    context.library.patch(legacy["id"], {"note": "我的笔记", "tags": ["待看"], "title": "自定义标题"}, 0)
    result = add(context, resolve(context, SHORT))
    assert (result["created"], result["duplicates"]) == (0, 1)
    upgraded = result["items"][0]
    assert upgraded["id"] == legacy["id"] and upgraded["platform_item_id"] == ITEM_ID and upgraded["provider"] == "tikhub"
    assert upgraded["note"] == "我的笔记" and upgraded["tags"] == ["待看"] and upgraded["title"] == "自定义标题"
    assert upgraded["author"] == "合成作者"
    assert add(context, resolve(context, LONG, confirm_paid=False))["items"][0]["id"] == legacy["id"]
    assert len(context.library.list_sources()) == 1


def test_creator_import_fetches_one_page_and_adds_only_the_selection(context):
    context.upstream.routes[P["creator_videos"]] = (200, samples.envelope({
        "has_more": 1, "max_cursor": 1754000000000,
        "aweme_list": [samples.aweme("7000000000000000011"), samples.aweme("7000000000000000012", images=[{"uri": "x"}]),
                       {"aweme_id": "broken"}, samples.aweme("7000000000000000013")] + [
                           samples.aweme(str(7000000000000000100 + index)) for index in range(30)]}))
    preview = post(context, "/tikhub/creator", {"text": PROFILE + "?from_tab_name=main", "confirm_paid": True})
    assert preview["kind"] == "creator" and preview["requests_used"] == 1
    assert preview["creator"] == {"sec_user_id": SEC_USER_ID, "nickname": "合成作者", "url": PROFILE}
    assert len(preview["items"]) == 19 and preview["has_more"] is True and preview["next_cursor"] == 1754000000000
    image = preview["items"][1]
    assert image["kind"] == "images" and image["downloadable"] is False and "图文" in image["reason_message"]
    assert context.upstream.paths() == [P["creator_videos"]]
    assert dict(context.upstream.requests[0].url.params)["count"] == "20"
    result = add(context, preview, ["7000000000000000011", "7000000000000000013", "7000000000000000012"])
    assert result["created"] == 3
    assert [item["supported"] for item in result["items"]] == [True, True, False]
    assert len(context.library.list_sources()) == 3
    post(context, "/tikhub/import", {"preview_id": preview["preview_id"], "item_ids": ["7999999999999999999"]}, 400)
    post(context, "/tikhub/import", {"preview_id": "0" * 32, "item_ids": ["7000000000000000011"]}, 404)
    # A profile short link needs one extra lookup, disclosed before it is sent.
    short = post(context, "/tikhub/creator", {"text": "https://v.douyin.com/TestUserA/", "confirm_paid": True})
    assert short["requests_used"] == 2 and short["estimated_cost_usd"] == 0.002
    assert context.upstream.paths()[1:] == [P["creator_id"], P["creator_videos"]]


def test_download_uses_addresses_from_the_preview_without_another_request(context):
    source = add(context, resolve(context))["items"][0]
    queued = download(context, source["id"])
    assert queued["status"] == "queued"
    context.worker.run_next()
    ready = context.library.detail(source["id"])
    assert ready["status"] == "ready" and ready["has_media"] is True and ready["author"] == "合成作者"
    call = context.downloads[0]
    assert call["url"] == LONG and len(call["media"]["candidates"]) == 6
    assert set(call["media"]) == {"candidates", "title", "author"}
    assert all(set(candidate) == {"url"} for candidate in call["media"]["candidates"])
    assert TOKEN not in json.dumps(call)
    attempt = context.library._attempt(ready["download_id"])
    assert attempt["method"] == "tikhub" and attempt["tikhub_requests"] == 0 and attempt["status"] == "ready"
    assert len(context.upstream.requests) == 1
    response = context.client.get(f"/api/network-media/sources/{source['id']}/media", headers={"Range": "bytes=0-9"})
    assert response.status_code == 206 and response.content == STUB_MEDIA[:10]


def test_expired_addresses_need_a_confirmed_request_before_the_download(context):
    source = add(context, resolve(context))["items"][0]
    context.clock.now += 7 * 60 * 60
    body = download(context, source["id"], 409)
    assert body["detail"]["code"] == "tikhub_confirmation_required"
    assert context.library.detail(source["id"])["status"] == "saved" and not context.worker.pending
    download(context, source["id"], confirm_paid=True)
    context.worker.run_next()
    ready = context.library.detail(source["id"])
    assert ready["status"] == "ready"
    assert context.library._attempt(ready["download_id"])["tikhub_requests"] == 1
    assert context.upstream.paths() == [P["video_by_id"], P["video_by_id"]]


def test_a_downloaded_post_is_not_requested_or_downloaded_again(context):
    source = add(context, resolve(context))["items"][0]
    download(context, source["id"])
    context.worker.run_next()
    requests, downloads = len(context.upstream.requests), len(context.downloads)
    for body in ({}, {"confirm_paid": True}):
        assert download(context, source["id"], 409, **body)["detail"]["code"] == "already_downloaded"
    assert resolve(context, SHORT.replace("TestDouyinA", "Unknown1"))["items"][0]["has_media"] is True
    assert len(context.downloads) == downloads and not context.worker.pending
    download(context, source["id"], force=True)
    context.worker.run_next()
    assert len(context.downloads) == downloads + 1
    assert len(context.upstream.requests) == requests + 1  # Only the new short link was looked up.


@pytest.mark.parametrize(("reason", "code", "status_name"), [(5, "private_content", "needs_login"),
                                                              (10, "restricted_content", "needs_login"),
                                                              (8, "content_unavailable", "failed")])
def test_permission_failures_keep_the_source_and_explain_the_reason(context, reason, code, status_name):
    saved = post(context, "/sources", {"text": LONG}, 201)["items"][0]
    context.library.patch(saved["id"], {"note": "保留"}, 0)
    context.upstream.routes[P["video_by_id"]] = (200, samples.filtered(reason))
    download(context, saved["id"], confirm_paid=True)
    context.worker.run_next()
    current = context.library.detail(saved["id"])
    assert current["status"] == status_name and current["error"]["code"] == code
    assert current["note"] == "保留" and current["url"] == LONG and current["has_media"] is False
    assert "PROVIDER-PRIVATE-TEXT" not in json.dumps(current, ensure_ascii=False)
    assert context.library._attempt(current["download_id"])["tikhub_requests"] == 1
    assert context.downloads == []


def test_provider_errors_are_safe_and_counted_as_unbilled(context):
    context.upstream.routes[P["video_by_id"]] = (401, samples.unauthorized())
    body = post(context, "/tikhub/resolve", {"text": LONG, "confirm_paid": True}, 502)
    assert body["detail"]["code"] == "tikhub_unauthorized"
    assert "PROVIDER-PRIVATE-TEXT" not in json.dumps(body) and TOKEN not in json.dumps(body)
    usage = status(context)["usage"]
    assert usage["today"]["failed"] == 1 and usage["today"]["billable"] == 0 and usage["today"]["estimated_cost_usd"] == 0
    assert usage["today"]["paid_attempts"] == 1 and usage["last_request"]["code"] == "tikhub_unauthorized"
    assert post(context, "/tikhub/resolve", {"text": "https://example.org/a", "confirm_paid": True}, 400)["detail"]["code"] == "not_douyin_video"
    assert post(context, "/tikhub/creator", {"text": LONG, "confirm_paid": True}, 400)["detail"]["code"] == "not_douyin_creator"
    assert len(context.upstream.requests) == 1


def test_local_daily_cap_stops_requests_before_they_are_sent(context):
    context.settings.tikhub_daily_request_limit = 2
    resolve(context, LONG)
    assert post(context, "/tikhub/creator", {"text": "https://v.douyin.com/TestUserA/", "confirm_paid": True}, 409)["detail"]["code"] == "tikhub_budget_exhausted"
    post(context, "/tikhub/creator", {"text": PROFILE, "confirm_paid": True})
    assert post(context, "/tikhub/resolve", {"text": SHORT, "confirm_paid": True}, 409)["detail"]["code"] == "tikhub_budget_exhausted"
    assert len(context.upstream.requests) == 2
    context.clock.now += 24 * 60 * 60  # The cap is per UTC day.
    resolve(context, SHORT)
    assert len(context.upstream.requests) == 3 and status(context)["usage"]["total"]["paid_attempts"] == 3


def test_without_a_token_douyin_downloads_keep_the_anonymous_path(context):
    context.settings.tikhub_api_key = SecretStr("")
    saved = post(context, "/sources", {"text": SHORT}, 201)["items"][0]
    download(context, saved["id"])
    context.worker.run_next()
    assert context.downloads == [{"url": SHORT, "media": None}]
    assert context.library._attempt(context.library.detail(saved["id"])["download_id"])["method"] == "anonymous"
    assert download(context, saved["id"], 409, method="tikhub")["detail"]["code"] == "already_downloaded"
    other = post(context, "/sources", {"text": "https://v.douyin.com/TestDouyinB/"}, 201)["items"][0]
    assert download(context, other["id"], 409, method="tikhub", confirm_paid=True)["detail"]["code"] == "tikhub_not_configured"
    assert context.upstream.requests == []


def test_anonymous_method_stays_available_for_comparison_and_other_platforms_never_use_tikhub(context):
    douyin = post(context, "/sources", {"text": LONG}, 201)["items"][0]
    youtube = post(context, "/sources", {"text": "https://www.youtube.com/watch?v=TestVideo01"}, 201)["items"][0]
    download(context, douyin["id"], method="anonymous")
    download(context, youtube["id"])
    assert download(context, post(context, "/sources", {"text": "https://example.org/a.mp4"}, 201)["items"][0]["id"],
                    409, method="tikhub")["detail"]["code"] == "tikhub_not_available"
    context.worker.run_next()
    context.worker.run_next()
    assert [call["media"] for call in context.downloads] == [None, None]
    assert context.upstream.requests == []
    comparison = status(context)["comparison"]
    assert comparison["methods"]["anonymous"]["attempts"] == 1 and comparison["methods"]["anonymous"]["ready"] == 1
    assert comparison["methods"]["tikhub"]["attempts"] == 0


def test_short_link_download_learns_the_post_id_and_refuses_a_second_copy(context):
    first = add(context, resolve(context, LONG))["items"][0]
    download(context, first["id"])
    context.worker.run_next()
    other = post(context, "/sources", {"text": "https://v.douyin.com/TestDouyinB/"}, 201)["items"][0]
    assert other["id"] != first["id"]
    download(context, other["id"], confirm_paid=True)
    context.worker.run_next()
    duplicate = context.library.detail(other["id"])
    assert duplicate["status"] == "failed" and duplicate["error"]["code"] == "duplicate_item"
    assert len(context.downloads) == 1
    # The first record now remembers that link, so the next paste costs nothing.
    assert resolve(context, "https://v.douyin.com/TestDouyinB/", confirm_paid=False)["requests_used"] == 0
    stats = status(context)["comparison"]
    assert stats["methods"]["tikhub"] == {"attempts": 2, "ready": 1, "needs_login": 0, "unsupported": 0, "failed": 1, "other": 0}
    assert stats["sources"] == {"resolved": 1, "downloaded": 1, "needs_permission": 0}


def test_live_checks_are_user_triggered_and_the_demo_sends_no_token(context):
    demo = post(context, "/tikhub/check", {"mode": "demo"})
    assert demo["ok"] is True and demo["sample"]["item_id"] == ITEM_ID and demo["sample"]["codec"] == "h264"
    assert "authorization" not in context.upstream.requests[0].headers
    account = post(context, "/tikhub/check", {"mode": "account"})
    assert account["ok"] is True and account["account"]["balance_usd"] == 1.5
    assert "private-owner" not in json.dumps(account) and TOKEN not in json.dumps(account)
    usage = status(context)["usage"]
    assert usage["today"]["requests"] == 2 and usage["today"]["paid_attempts"] == 0 and usage["today"]["estimated_cost_usd"] == 0
    context.upstream.routes[P["account"]] = (401, samples.unauthorized())
    failed = post(context, "/tikhub/check", {"mode": "account"})
    assert failed["ok"] is False and failed["code"] == "tikhub_unauthorized"
    assert status(context)["last_check"]["code"] == "tikhub_unauthorized"
    context.settings.tikhub_api_key = SecretStr("")
    assert post(context, "/tikhub/check", {"mode": "account"})["code"] == "tikhub_not_configured"
    assert post(context, "/tikhub/check", {"mode": "demo"})["ok"] is True
    post(context, "/tikhub/check", {"mode": "cookies"}, 422)


def test_previews_expire_and_only_listed_posts_can_be_imported(context):
    preview = resolve(context)
    context.clock.now += 31 * 60
    assert post(context, "/tikhub/import", {"preview_id": preview["preview_id"], "item_ids": [ITEM_ID]}, 404)["detail"]["code"] == "tikhub_preview_expired"
    post(context, "/tikhub/import", {"preview_id": preview["preview_id"], "item_ids": ["../../x"]}, 422)
    post(context, "/tikhub/resolve", {"text": LONG, "cookie": "sessionid=1"}, 422)
    assert context.library.list_sources() == []


def test_token_and_signed_addresses_never_reach_disk_or_api_responses(context):
    source = add(context, resolve(context, SHORT))["items"][0]
    post(context, "/tikhub/creator", {"text": PROFILE, "confirm_paid": True})
    post(context, "/tikhub/check", {"mode": "account"})
    download(context, source["id"])
    context.worker.run_next()
    listed = context.client.get("/api/network-media/sources").text + json.dumps(status(context))
    disk = stored_text(context)
    for secret in (TOKEN, "media.example.com", "sign=synthetic", "private-owner@example.com", "PRIVATE-KEY-NAME"):
        assert secret not in disk and secret not in listed
    assert TOKEN not in repr(context.library.tikhub._client())


def test_offline_report_script_summarises_results_without_touching_library_state(context, monkeypatch, capsys):
    from scripts import tikhub_check
    ready = add(context, resolve(context))["items"][0]
    download(context, ready["id"])
    context.worker.run_next()
    private = post(context, "/sources", {"text": "https://www.douyin.com/video/7000000000000000002"}, 201)["items"][0]
    context.upstream.routes[P["video_by_id"]] = (200, samples.filtered(5))
    download(context, private["id"], confirm_paid=True)
    context.worker.run_next()
    queued = post(context, "/sources", {"text": "https://v.douyin.com/TestDouyinB/"}, 201)["items"][0]
    download(context, queued["id"], method="anonymous")
    before = stored_text(context)
    monkeypatch.setenv("DOUYINMIND_LOCAL_MEDIA_ROOT", str(context.root))
    assert tikhub_check.main(["report"]) == 0
    output = capsys.readouterr().out
    assert "TikHub 成功解析并加入资料库的作品：1" in output and "已下载到本机（通过完整解码与 SHA-256 校验）：1" in output
    assert "TikHub 已应答但需要登录/权限：1" in output and "TikHub 请求：2 次" in output and "$0.002" in output
    assert "| 7000000000000000002 | 抖音视频 | needs_login | 无 | private_content |" in output
    assert status(context)["comparison"]["sources"] == {"resolved": 1, "downloaded": 1, "needs_permission": 1}
    assert "| TikHub | 2 | 1 | 1 | 0 | 0 | 0 |" in output and "| 原匿名解析 | 1 | 0 | 0 | 0 | 0 | 1 |" in output
    assert TOKEN not in output and "media.example.com" not in output
    # Reading the report must not mark the queued download as interrupted.
    assert stored_text(context) == before and context.library.detail(queued["id"])["status"] == "queued"


def test_a_library_without_the_provider_reports_it_and_downloads_as_before(context):
    context.library.tikhub = None
    assert context.client.get("/api/network-media/tikhub/status").status_code == 404
    saved = post(context, "/sources", {"text": LONG}, 201)["items"][0]
    assert saved["platform_item_id"] == ITEM_ID and saved["provider"] is None
    download(context, saved["id"])
    context.worker.run_next()
    assert context.downloads == [{"url": LONG, "media": None}]
