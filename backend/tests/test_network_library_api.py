"""Offline source management, durable queues and publication; downloader stubbed.

The stub's bytes test persistence and Range delivery, not real video decoding or
any platform's downloader support. Transport/media validation has separate tests.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import socket
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.routes import local_media, network_media
from app.services import network_download
from app.services import local_media_import
from app.services.local_media_workspace import LocalMediaWorkspace
from app.services.network_library import NetworkLibrary


class QueueWorker:
    def __init__(self):
        self.pending = []

    def submit(self, task_id, function, *args, **kwargs):
        self.pending.append((task_id, function, args, kwargs))

    def run_next(self):
        task_id, function, args, kwargs = self.pending.pop(0)
        return function(*args, task_id=task_id, **kwargs)


def forbidden(*args, **kwargs):
    raise AssertionError("External network, ASR and provider configuration are forbidden in source-library tests")


@pytest.fixture
def context(tmp_path, monkeypatch):
    connect = socket.socket.connect
    def local_only(sock, address):
        # Windows asyncio uses a loopback TCP socketpair for its event loop.
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return connect(sock, address)
        forbidden(sock, address)
    monkeypatch.setattr(socket.socket, "connect", local_only)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(local_media_import, "run_import", forbidden)
    workspace = LocalMediaWorkspace(tmp_path / "managed")
    monkeypatch.setattr(workspace, "configuration", forbidden)
    worker = QueueWorker()
    library = NetworkLibrary(workspace, worker=worker)
    monkeypatch.setattr(local_media, "get_workspace", lambda: workspace)
    monkeypatch.setattr(network_media, "get_network_library", lambda: library)
    app = FastAPI()
    app.include_router(network_media.router, prefix="/api")
    client = TestClient(app, base_url="http://127.0.0.1")
    return SimpleNamespace(client=client, library=library, workspace=workspace, worker=worker,
                           headers={"X-Local-Media-Token": workspace.session_token})


def save(context, url="https://www.youtube.com/watch?v=jNQXAC9IVRw"):
    response = context.client.post("/api/network-media/sources", json={"text": url}, headers=context.headers)
    assert response.status_code == 201, response.text
    return response.json()["items"][0]


def start(context, source_id):
    response = context.client.post(f"/api/network-media/sources/{source_id}/download", headers=context.headers)
    assert response.status_code == 202, response.text
    return response.json()


STUB_MEDIA = b"TEST-ONLY-DOWNLOADER-BYTES-NOT-REAL-MEDIA" * 10


def stub_download(url, target_dir, *, progress, cancelled):
    assert not cancelled()
    target = target_dir / "complete.mp4"
    target.write_bytes(STUB_MEDIA)
    progress({"downloaded_bytes": len(STUB_MEDIA), "total_bytes": len(STUB_MEDIA),
              "url": "https://signed.invalid/?token=must-not-persist"})
    return {"path": target, "title": "Downloader title", "author": "Example author",
            "duration_ms": 2000, "size_bytes": len(STUB_MEDIA),
            "sha256": hashlib.sha256(STUB_MEDIA).hexdigest(), "content_type": "video/mp4"}


def test_save_and_deduplicate_without_network_download_or_provider_reads(context, monkeypatch):
    monkeypatch.setattr(network_download, "download_media", forbidden)
    first = save(context)
    second = context.client.post("/api/network-media/sources", json={"text": first["url"]}, headers=context.headers)
    assert second.status_code == 201
    assert second.json()["created"] == 0
    assert second.json()["duplicates"] == 1
    assert first["status"] == "saved" and first["has_media"] is False
    assert first["revision"] == 0
    assert first["media_version"] is None
    assert not context.worker.pending
    assert len(context.library.list_sources()) == 1


def test_unknown_site_can_be_collected_without_claiming_download_support(context):
    source = save(context, "https://example.org/research-video-page")
    assert source["status"] == "saved"
    assert source["has_media"] is False
    assert context.client.get(f"/api/network-media/sources/{source['id']}/media").status_code == 404


def test_capabilities_never_read_asr_configuration_and_never_claim_platform_validation(context):
    response = context.client.get("/api/network-media/capabilities")
    assert response.status_code == 200
    body = response.json()
    assert body["session_token"] == context.workspace.session_token
    assert body["max_batch"] == 20
    assert body["limits"]["media_bytes"] == 500 * 1024**2
    assert all(platform["download_verified"] is False for platform in body["platforms"])
    assert "asr_configured" not in body


@pytest.mark.parametrize("headers", [{"Host": "attacker.example"}, {"Origin": "https://attacker.example"}])
def test_source_reads_reject_external_origins_and_hosts(context, headers):
    assert context.client.get("/api/network-media/sources", headers=headers).status_code == 403


def test_mutation_requires_shared_session_token(context):
    response = context.client.post("/api/network-media/sources", json={"text": "https://example.com/video.mp4"})
    assert response.status_code == 403
    assert context.library.list_sources() == []


def test_metadata_edit_cas_reversible_archive_and_field_limits(context):
    source = save(context)
    url = f"/api/network-media/sources/{source['id']}"
    response = context.client.patch(url, json={"expected_revision": 0, "title": "我的素材", "tags": ["学习", "学习", "待看"],
                                              "note": "私人整理", "starred": True, "archived": True}, headers=context.headers)
    assert response.status_code == 200
    edited = response.json()
    assert edited["tags"] == ["学习", "待看"]
    assert edited["revision"] == 1
    assert context.client.patch(url, json={"expected_revision": 0, "note": "stale overwrite"}, headers=context.headers).status_code == 409
    assert context.library.detail(source["id"])["note"] == "私人整理"
    response = context.client.patch(url, json={"expected_revision": 1, "archived": False}, headers=context.headers)
    assert response.json()["archived"] is False
    assert context.client.patch(url, json={"expected_revision": 2, "status": "ready"}, headers=context.headers).status_code == 422
    assert context.client.patch(url, json={"expected_revision": 2, "note": "x" * 5001}, headers=context.headers).status_code == 422


def test_sources_text_limit_rejects_before_any_persistence(context):
    response = context.client.post("/api/network-media/sources", json={"text": "x" * 20001}, headers=context.headers)
    assert response.status_code == 422
    assert context.library.list_sources() == []


def test_explicit_download_publishes_media_and_serves_ranges_without_starting_asr(context, monkeypatch):
    monkeypatch.setattr(network_download, "download_media", stub_download)
    source = save(context)
    started = start(context, source["id"])
    assert started["status"] == "queued"
    context.worker.run_next()
    complete = context.library.detail(source["id"])
    assert complete["status"] == "ready" and complete["has_media"] is True
    assert complete["title"] == "Downloader title"
    assert complete["revision"] == 1
    version = complete["media_version"]
    assert version == hashlib.sha256(STUB_MEDIA).hexdigest()
    context.library.patch(source["id"], {"starred": True, "note": "播放时编辑"}, complete["revision"])
    assert context.library.detail(source["id"])["media_version"] == version
    assert set(complete["progress"]) == {"downloaded_bytes", "total_bytes"}
    response = context.client.get(f"/api/network-media/sources/{source['id']}/media", headers={"Range": "bytes=0-9"})
    assert response.status_code == 206 and response.content == STUB_MEDIA[:10]
    response = context.client.get(f"/api/network-media/sources/{source['id']}/download-file")
    assert response.status_code == 200 and response.content == STUB_MEDIA
    assert "attachment" in response.headers["content-disposition"]
    raw = context.library.path("sources", source["id"] + ".json").read_text(encoding="utf-8")
    assert "must-not-persist" not in raw


def test_download_progress_does_not_change_metadata_revision_and_preserves_midflight_edits(context, monkeypatch):
    source = save(context)
    def edit_during_download(url, target_dir, *, progress, cancelled):
        progress({"downloaded_bytes": 10, "total_bytes": 100})
        assert context.library.detail(source["id"])["revision"] == 0
        context.library.patch(source["id"], {"title": "我自定义的标题", "note": "下载期间编辑的笔记",
                                              "tags": ["收藏"], "starred": True, "archived": True}, 0)
        return stub_download(url, target_dir, progress=progress, cancelled=cancelled)
    monkeypatch.setattr(network_download, "download_media", edit_during_download)
    start(context, source["id"])
    context.worker.run_next()
    updated = context.library.detail(source["id"])
    assert updated["status"] == "ready"
    assert updated["title"] == "我自定义的标题"
    assert updated["note"] == "下载期间编辑的笔记"
    assert updated["tags"] == ["收藏"] and updated["starred"] and updated["archived"]
    assert updated["revision"] == 1


def test_multiple_sources_queue_serially_and_same_source_is_not_queued_twice(context, monkeypatch):
    monkeypatch.setattr(network_download, "download_media", stub_download)
    first, second = save(context), save(context, "https://example.org/another.mp4")
    start(context, first["id"])
    assert context.client.post(f"/api/network-media/sources/{first['id']}/download", headers=context.headers).status_code == 409
    start(context, second["id"])
    assert len(context.worker.pending) == 2
    context.worker.run_next()
    assert context.library.detail(first["id"])["status"] == "ready"
    assert context.library.detail(second["id"])["status"] == "queued"
    context.worker.run_next()
    assert context.library.detail(second["id"])["status"] == "ready"


def test_queued_cancel_never_calls_downloader_and_new_attempt_can_complete(context, monkeypatch):
    monkeypatch.setattr(network_download, "download_media", forbidden)
    source = save(context)
    first = start(context, source["id"])
    assert context.client.post(f"/api/network-media/sources/{source['id']}/cancel", headers=context.headers).json()["status"] == "cancelled"
    context.worker.run_next()
    monkeypatch.setattr(network_download, "download_media", stub_download)
    second = start(context, source["id"])
    assert first["download_id"] != second["download_id"]
    context.worker.run_next()
    assert context.library.detail(source["id"])["status"] == "ready"


def test_cancel_between_download_and_publication_keeps_previous_media(context, monkeypatch):
    source = save(context)
    monkeypatch.setattr(network_download, "download_media", stub_download)
    start(context, source["id"])
    context.worker.run_next()
    original_path = context.library.media_path(source["id"])[0]
    def cancel_before_return(url, target_dir, **kwargs):
        result = stub_download(url, target_dir, **kwargs)
        context.library.cancel(source["id"])
        return result
    monkeypatch.setattr(network_download, "download_media", cancel_before_return)
    start(context, source["id"])
    context.worker.run_next()
    assert context.library.detail(source["id"])["status"] == "cancelled"
    assert context.library.media_path(source["id"])[0] == original_path
    assert original_path.read_bytes() == STUB_MEDIA


@pytest.mark.parametrize("code", ["authentication_required", "unsupported_source", "download_failed"])
def test_failed_retry_keeps_source_notes_and_previous_media(context, monkeypatch, code):
    source = save(context)
    monkeypatch.setattr(network_download, "download_media", stub_download)
    start(context, source["id"])
    context.worker.run_next()
    original = context.library.media_path(source["id"])[0]
    saved = context.library.detail(source["id"])
    context.library.patch(source["id"], {"note": "失败也保留"}, saved["revision"])
    def fail(*args, **kwargs):
        raise network_download.NetworkDownloadError(code, "测试中的安全错误")
    monkeypatch.setattr(network_download, "download_media", fail)
    start(context, source["id"])
    context.worker.run_next()
    current = context.library.detail(source["id"])
    assert current["status"] == {"authentication_required": "needs_login", "unsupported_source": "unsupported"}.get(code, "failed")
    assert current["has_media"] and current["note"] == "失败也保留"
    assert current["url"] == source["url"]
    assert context.library.media_path(source["id"])[0] == original


@pytest.mark.parametrize("status", ["queued", "downloading", "cancel_requested"])
def test_restart_marks_active_attempts_interrupted_without_resubmitting(context, status):
    source = save(context)
    queued = start(context, source["id"])
    persisted = context.library.source(source["id"])
    persisted["status"] = status
    context.library._save_source(persisted)
    attempt = context.library._attempt(queued["download_id"])
    attempt["status"] = status
    context.library._save_attempt(attempt)
    replacement = QueueWorker()
    restored = NetworkLibrary(context.workspace, worker=replacement)
    assert restored.detail(source["id"])["status"] == "interrupted"
    assert restored._attempt(queued["download_id"])["status"] == "interrupted"
    assert replacement.pending == []


def test_invalid_download_cannot_publish_or_overwrite_an_existing_result(context, monkeypatch):
    source = save(context)
    monkeypatch.setattr(network_download, "download_media", stub_download)
    start(context, source["id"])
    context.worker.run_next()
    original = context.library.media_path(source["id"])[0]
    def bad_checksum(url, target_dir, **kwargs):
        result = stub_download(url, target_dir, **kwargs)
        result["sha256"] = "0" * 64
        return result
    monkeypatch.setattr(network_download, "download_media", bad_checksum)
    start(context, source["id"])
    context.worker.run_next()
    assert context.library.detail(source["id"])["error"]["code"] == "invalid_download"
    assert context.library.media_path(source["id"])[0] == original


def test_arbitrary_exception_details_never_enter_source_record(context, monkeypatch):
    source = save(context)
    def fail(*args, **kwargs):
        raise ValueError("private-cookie-token-or-signed-url")
    monkeypatch.setattr(network_download, "download_media", fail)
    start(context, source["id"])
    context.worker.run_next()
    assert context.library.detail(source["id"])["status"] == "failed"
    assert "private-cookie" not in json.dumps(context.library.detail(source["id"]))


def test_media_range_version_rejects_new_bytes_for_an_old_player_url(context, monkeypatch):
    monkeypatch.setattr(network_download, "download_media", stub_download)
    source = save(context)
    start(context, source["id"])
    context.worker.run_next()
    first = context.library.detail(source["id"])
    url = f"/api/network-media/sources/{source['id']}/media"
    context.library.patch(source["id"], {"note": "编辑不改变视频版本"}, first["revision"])
    response = context.client.get(url, params={"v": first["media_version"]}, headers={"Range": "bytes=0-9"})
    assert response.status_code == 206 and response.content == STUB_MEDIA[:10]
    replacement_bytes = b"REPLACED-VIDEO-TEST-BYTES" * 10
    def replacement(url, target_dir, **kwargs):
        result = stub_download(url, target_dir, **kwargs)
        result["path"].write_bytes(replacement_bytes)
        result.update(size_bytes=len(replacement_bytes), sha256=hashlib.sha256(replacement_bytes).hexdigest())
        return result
    monkeypatch.setattr(network_download, "download_media", replacement)
    start(context, source["id"])
    context.worker.run_next()
    current = context.library.detail(source["id"])
    assert current["media_version"] != first["media_version"]
    stale = context.client.get(url, params={"v": first["media_version"]}, headers={"Range": "bytes=10-19"})
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "media_version_changed"
    fresh = context.client.get(url, params={"v": current["media_version"]}, headers={"Range": "bytes=10-19"})
    assert fresh.status_code == 206 and fresh.content == replacement_bytes[10:20]
    assert context.client.get(url, params={"v": "invalid"}).status_code == 422
