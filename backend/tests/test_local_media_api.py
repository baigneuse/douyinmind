"""Local browser workflow against isolated files; no provider or network calls."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
from types import SimpleNamespace
import wave

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.routes import local_media as routes
from app.services import local_media_workspace as module
from app.services.local_media_import import LocalImportError
from app.services.local_media_workspace import LocalMediaWorkspace, WorkspaceError


class QueueWorker:
    def __init__(self):
        self.pending = []

    def submit(self, task_id, function, *args, **kwargs):
        self.pending.append((task_id, function, args, kwargs))

    def run_next(self):
        task_id, function, args, kwargs = self.pending.pop(0)
        function(*args, task_id=task_id, **kwargs)


def blocked_network(*args, **kwargs):
    raise AssertionError("No network requests are allowed in local API tests")


@pytest.fixture
def context(tmp_path, monkeypatch):
    original_connect = socket.socket.connect
    def loopback_only(sock, address):
        # Windows asyncio implements socketpair with a local TCP connection.
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return original_connect(sock, address)
        return blocked_network(sock, address)
    monkeypatch.setattr(socket.socket, "connect", loopback_only)
    monkeypatch.setattr(socket, "create_connection", blocked_network)
    for name in ("QWEN_OMNI_API_KEY", "DASHSCOPE_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    fallback = Path(__file__).resolve().parents[3] / ".phase0-tools/bin/ffmpeg.exe"
    executable = shutil.which("ffmpeg") or (str(fallback) if fallback.is_file() else None)
    if executable:
        monkeypatch.setenv("PATH", str(Path(executable).parent) + os.pathsep + os.environ.get("PATH", ""))
    worker = QueueWorker()
    configuration = SimpleNamespace(qwen_omni_api_key="", dashscope_api_key="", qwen_omni_model="test-model",
                                    qwen_omni_base_url="https://example.invalid/v1")
    workspace = LocalMediaWorkspace(tmp_path / "managed", worker=worker, configuration=configuration)
    monkeypatch.setattr(routes, "get_workspace", lambda: workspace)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    client = TestClient(app, base_url="http://127.0.0.1")
    return SimpleNamespace(client=client, workspace=workspace, worker=worker, config=configuration,
                           headers={"X-Local-Media-Token": workspace.session_token}, ffmpeg=executable)


def upload(context, data, *, filename="语音 日本語.wav", kind="media"):
    response = context.client.post("/api/local-media/uploads", params={"filename": filename, "kind": kind},
                                   content=data, headers=context.headers)
    assert response.status_code == 201, response.text
    return response.json()["upload_id"]


def wave_bytes():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\0\0" * 32000)
    return buffer.getvalue()


def inputs(context, text="1\n00:00:00,123 --> 00:00:01,456\n这是已有字幕，数字42。\n"):
    media = upload(context, wave_bytes())
    subtitles = upload(context, text.encode("utf-8"), filename="字幕.srt", kind="subtitles")
    return {"media_upload_id": media, "subtitle_upload_id": subtitles, "mode": "subtitles",
            "allow_cloud_asr": False, "max_requests": 8}


def submit(context, payload):
    response = context.client.post("/api/local-media/jobs", json=payload, headers=context.headers)
    assert response.status_code == 202, response.text
    return response.json()


def complete(context, payload=None):
    if not context.ffmpeg:
        pytest.fail("Local API integration tests require ffmpeg")
    job = submit(context, payload or inputs(context))
    context.worker.run_next()
    result = context.client.get("/api/local-media/jobs/" + job["id"]).json()
    assert result["status"] == "completed", result
    return result


@pytest.mark.parametrize("headers", [
    {"Host": "evil.example"}, {"Host": "localhost.evil.example"},
    {"Host": "evil@localhost"}, {"Origin": "https://evil.example"},
    {"Origin": "null"}, {"Origin": "http://localhost.evil.example:5173"},
    {"Sec-Fetch-Site": "cross-site"},
])
def test_capability_token_cannot_be_read_by_external_site_or_rebound_host(context, headers):
    response = context.client.get("/api/local-media/capabilities", headers=headers)
    assert response.status_code == 403
    assert context.workspace.session_token not in response.text


@pytest.mark.parametrize("host,origin", [("localhost:8123", "http://localhost:5174"),
                                           ("[::1]:8123", "http://[::1]:5174"),
                                           ("127.0.0.1:8000", "http://127.0.0.1:4000")])
def test_loopback_ports_and_ipv6_are_accepted(context, host, origin):
    response = context.client.get("/api/local-media/capabilities", headers={"Host": host, "Origin": origin})
    assert response.status_code == 200
    value = response.json()
    assert value["cloud_verified"] is False
    assert value["limits"] == {"media_bytes": 500 * 1024**2, "subtitle_bytes": 10 * 1024**2, "duration_seconds": 1800}
    assert value["asr_configured"] is False


@pytest.mark.parametrize("headers", [{}, {"X-Local-Media-Token": "wrong"}])
def test_upload_needs_session_token_before_reading_body(context, headers):
    response = context.client.post("/api/local-media/uploads?filename=x.wav&kind=media", content=b"private", headers=headers)
    assert response.status_code == 403
    assert not list(context.workspace.path("uploads").iterdir())


@pytest.mark.parametrize("filename,kind", [("../secret.wav", "media"), ("C:\\secret.wav", "media"),
                                           ("x.html", "media"), ("x.mp4", "subtitles")])
def test_upload_filenames_cannot_escape_or_select_other_types(context, filename, kind):
    response = context.client.post("/api/local-media/uploads", params={"filename": filename, "kind": kind},
                                   content=b"content", headers=context.headers)
    assert response.status_code == 400
    assert not list(context.workspace.path("uploads").iterdir())


def test_stream_size_limit_and_empty_upload_cleanup(context, monkeypatch):
    monkeypatch.setattr(module, "MEDIA_BYTES", 8)
    response = context.client.post("/api/local-media/uploads?filename=x.wav&kind=media", content=b"too much media", headers=context.headers)
    assert response.status_code == 413
    response = context.client.post("/api/local-media/uploads?filename=x.wav&kind=media", content=b"", headers=context.headers)
    assert response.status_code == 400
    assert not list(context.workspace.path("uploads").iterdir())


def test_untrusted_upload_id_kind_and_tampering_are_rejected(context):
    payload = inputs(context)
    bad = dict(payload, media_upload_id=payload["subtitle_upload_id"])
    response = context.client.post("/api/local-media/jobs", json=bad, headers=context.headers)
    assert response.status_code == 400
    record, path = context.workspace.upload(payload["media_upload_id"], "media")
    path.write_bytes(b"changed")
    response = context.client.post("/api/local-media/jobs", json=payload, headers=context.headers)
    assert response.status_code == 409
    assert not context.worker.pending


def test_unauthorized_cloud_is_rejected_before_worker_and_never_calls_provider(context, monkeypatch):
    monkeypatch.setattr(module, "run_import", blocked_network)
    media = upload(context, wave_bytes())
    response = context.client.post("/api/local-media/jobs", json={"media_upload_id": media, "mode": "qwen"}, headers=context.headers)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "cloud_asr_not_authorized"
    assert not context.worker.pending


def test_authorized_cloud_without_credentials_is_blocked_and_sends_nothing(context):
    if not context.ffmpeg:
        pytest.fail("ffmpeg required")
    payload = {"media_upload_id": upload(context, wave_bytes()), "mode": "qwen", "allow_cloud_asr": True, "max_requests": 8}
    job = submit(context, payload)
    context.worker.run_next()
    current = context.workspace.job(job["id"])
    assert current["status"] == "blocked"
    assert current["error"]["code"] == "missing_credentials"
    assert current["request_count"] == 0
    assert context.workspace.detail(job["asset_id"])["transcript"] is None


def test_real_offline_workflow_ranges_downloads_and_cached_repeat(context):
    payload = inputs(context)
    job = complete(context, payload)
    asset_id = job["asset_id"]
    detail = context.client.get("/api/local-media/assets/" + asset_id).json()
    assert detail["asset"]["has_result"] is True
    assert detail["asset"]["origin"] == "subtitles"
    assert detail["transcript"]["segments"][0]["start_ms"] == 123
    assert detail["transcript"]["segments"][0]["end_ms"] == 1456
    assert "未生成 AI 总结" in detail["knowledge"]
    media = context.client.get(f"/api/local-media/assets/{asset_id}/media", headers={"Range": "bytes=0-15"})
    assert media.status_code == 206
    assert media.content == wave_bytes()[:16]
    assert media.headers["content-range"].startswith("bytes 0-15/")
    manifest = None
    for filename in module.EXPORTS:
        response = context.client.get(f"/api/local-media/assets/{asset_id}/export/{filename}")
        assert response.status_code == 200
        if filename == "source_manifest.json":
            manifest = response.json()
    assert manifest["original_name"] == "语音 日本語.wav"
    assert manifest["subtitles"]["original_name"] == "字幕.srt"
    assert context.client.get(f"/api/local-media/assets/{asset_id}/export/asset.json").status_code == 404
    repeated = complete(context, payload)
    assert repeated["transcript_reused"] is True
    assert repeated["request_count"] == 0
    assert context.workspace.media_path(asset_id)[0].read_bytes() == wave_bytes()


def test_only_one_active_job_and_cancelled_queue_remains_retryable(context):
    payload = inputs(context)
    job = submit(context, payload)
    response = context.client.post("/api/local-media/jobs", json=payload, headers=context.headers)
    assert response.status_code == 409
    cancelled = context.client.post(f"/api/local-media/jobs/{job['id']}/cancel", headers=context.headers)
    assert cancelled.json()["status"] == "cancel_requested"
    context.worker.run_next()
    assert context.workspace.job(job["id"])["status"] == "cancelled"
    retry = context.client.post(f"/api/local-media/jobs/{job['id']}/retry", json={"allow_cloud_asr": False, "max_requests": 5}, headers=context.headers)
    assert retry.status_code == 202
    assert retry.json()["id"] != job["id"]
    assert retry.json()["mode"] == "subtitles"
    assert retry.json()["subtitle_upload_id"] == payload["subtitle_upload_id"]
    assert retry.json()["max_requests"] == 5


def test_parallel_submissions_admit_only_one_job(context):
    payload = inputs(context)
    def create():
        try:
            return context.workspace.create_job(payload)["id"]
        except WorkspaceError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create(), range(2)))
    assert results.count("job_busy") == 1
    assert len(context.worker.pending) == 1


@pytest.mark.parametrize("state", ["queued", "running", "cancel_requested"])
def test_restart_interrupts_persisted_jobs_without_resubmission(context, state):
    job = submit(context, inputs(context))
    job["status"] = state
    context.workspace._save_job(job)
    replacement_worker = QueueWorker()
    recovered = LocalMediaWorkspace(context.workspace.root, worker=replacement_worker, configuration=context.config)
    assert recovered.job(job["id"])["status"] == "interrupted"
    assert replacement_worker.pending == []
    assert recovered.session_token != context.workspace.session_token


def test_notes_compare_and_swap_detects_tabs_and_external_edits(context):
    job = submit(context, inputs(context))
    asset_id = job["asset_id"]
    initial = context.workspace.notes(asset_id)
    response = context.client.put(f"/api/local-media/assets/{asset_id}/notes", json={"text": "我自己的笔记", "revision": initial["revision"]}, headers=context.headers)
    assert response.status_code == 200
    response = context.client.put(f"/api/local-media/assets/{asset_id}/notes", json={"text": "stale tab", "revision": initial["revision"]}, headers=context.headers)
    assert response.status_code == 409
    current = context.workspace.notes(asset_id)
    context.workspace.path("assets", asset_id, "notes.md").write_text("外部编辑器修改", encoding="utf-8")
    response = context.client.put(f"/api/local-media/assets/{asset_id}/notes", json={"text": "old revision", "revision": current["revision"]}, headers=context.headers)
    assert response.status_code == 409
    assert context.workspace.notes(asset_id)["text"] == "外部编辑器修改"


def test_failed_attempt_keeps_successful_snapshot_and_user_notes(context, monkeypatch):
    payload = inputs(context)
    completed = complete(context, payload)
    asset_id = completed["asset_id"]
    snapshot = context.workspace.export_path(asset_id, "transcript.json").read_bytes()
    context.workspace.save_notes(asset_id, "我的人工笔记", context.workspace.notes(asset_id)["revision"])
    def failed_run(options, **kwargs):
        mutable = options.output_dir / ("local-media-" + asset_id)
        (mutable / "transcript.json").write_text("incomplete newer attempt", encoding="utf-8")
        raise LocalImportError("测试中的处理失败", "test_failure")
    monkeypatch.setattr(module, "run_import", failed_run)
    failed = submit(context, payload)
    context.worker.run_next()
    assert context.workspace.job(failed["id"])["status"] == "failed"
    assert context.workspace.export_path(asset_id, "transcript.json").read_bytes() == snapshot
    assert context.workspace.detail(asset_id)["asset"]["has_result"] is True
    assert context.workspace.notes(asset_id)["text"] == "我的人工笔记"
    assert context.workspace.media_path(asset_id)[0].read_bytes() == wave_bytes()


def test_worker_failure_details_are_sanitized_and_next_job_can_run(context, monkeypatch):
    payload = inputs(context)
    def fail(*args, **kwargs):
        raise RuntimeError("private transcript or credential must not appear")
    monkeypatch.setattr(module, "run_import", fail)
    job = submit(context, payload)
    context.worker.run_next()
    result = context.workspace.job(job["id"])
    assert result["status"] == "failed"
    assert "private transcript" not in json.dumps(result)
    submit(context, payload)
    assert len(context.worker.pending) == 1


def test_invalid_endpoint_credentials_and_query_never_leave_capabilities(context):
    context.config.qwen_omni_base_url = "https://private-user:private-password@example.invalid/v1?key=private-value"
    response = context.client.get("/api/local-media/capabilities")
    assert response.status_code == 200
    assert "private-" not in response.text
    assert response.json()["asr_endpoint"] == "无效的 HTTPS 服务地址"


def test_cancel_between_import_return_and_publish_preserves_previous_result(context, monkeypatch):
    payload = inputs(context)
    first = complete(context, payload)
    snapshot = context.workspace.export_path(first["asset_id"], "transcript.json")
    original = context.workspace._publish_result
    def cancel_before_publish(job_id, result):
        context.workspace.cancel(job_id)
        return original(job_id, result)
    monkeypatch.setattr(context.workspace, "_publish_result", cancel_before_publish)
    cancelled = submit(context, payload)
    context.worker.run_next()
    assert context.workspace.job(cancelled["id"])["status"] == "cancelled"
    assert context.workspace.export_path(first["asset_id"], "transcript.json") == snapshot


def test_retry_cloud_requires_new_consent_even_when_previous_attempt_authorized(context):
    payload = {"media_upload_id": upload(context, wave_bytes()), "mode": "qwen", "allow_cloud_asr": True, "max_requests": 8}
    job = submit(context, payload)
    context.workspace.cancel(job["id"])
    context.worker.run_next()
    response = context.client.post(f"/api/local-media/jobs/{job['id']}/retry", json={"allow_cloud_asr": False, "max_requests": 5}, headers=context.headers)
    assert response.status_code == 403
    assert not context.worker.pending


def test_out_of_order_runtime_snapshots_cannot_revert_completed_request(context):
    job = submit(context, inputs(context))
    completed = {"revision": 2, "request_count": 1, "requests": [{"index": 0, "status": "completed"}]}
    stale = {"revision": 1, "request_count": 1, "requests": [{"index": 0, "status": "in_flight"}]}
    context.workspace._control_update(job["id"], completed)
    context.workspace._control_update(job["id"], stale)
    saved = context.workspace.job(job["id"])
    assert saved["runtime_revision"] == 2
    assert saved["requests"][0]["status"] == "completed"


def test_duplicate_uploaded_copy_deduplicates_media_without_changing_source(context):
    payload = inputs(context)
    first = complete(context, payload)
    media_path, _ = context.workspace.media_path(first["asset_id"])
    original = media_path.read_bytes()
    payload["media_upload_id"] = upload(context, wave_bytes(), filename="同内容改名.wav")
    second = complete(context, payload)
    assert second["asset_id"] == first["asset_id"]
    assert media_path.read_bytes() == original
    assert len(context.workspace.list_assets()) == 1
    assert second["transcript_reused"] is True


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_correct_reupload_repairs_media_but_preserves_notes_and_success_snapshot(context, damage):
    payload = inputs(context)
    first = complete(context, payload)
    asset_id = first["asset_id"]
    media_path, _ = context.workspace.media_path(asset_id)
    snapshot_path = context.workspace.export_path(asset_id, "transcript.json")
    snapshot_bytes = snapshot_path.read_bytes()
    notes = context.workspace.save_notes(asset_id, "需要保留的人工笔记", context.workspace.notes(asset_id)["revision"])
    if damage == "missing":
        media_path.unlink()
    else:
        media_path.write_bytes(b"damaged managed copy")
    payload["media_upload_id"] = upload(context, wave_bytes(), filename="重新上传.wav")
    repaired = submit(context, payload)
    assert media_path.read_bytes() == wave_bytes()
    assert context.workspace.export_path(asset_id, "transcript.json") == snapshot_path
    assert snapshot_path.read_bytes() == snapshot_bytes
    assert context.workspace.notes(asset_id) == notes
    context.worker.run_next()
    assert context.workspace.job(repaired["id"])["status"] == "completed"
    assert context.workspace.job(repaired["id"])["transcript_reused"] is True
    assert context.workspace.notes(asset_id) == notes
    assert snapshot_path.read_bytes() == snapshot_bytes


def test_unavailable_media_repair_keeps_verified_upload_and_previous_results(context, monkeypatch):
    payload = inputs(context)
    first = complete(context, payload)
    asset_id = first["asset_id"]
    media_path, _ = context.workspace.media_path(asset_id)
    snapshot = context.workspace.export_path(asset_id, "transcript.json").read_bytes()
    notes = context.workspace.notes(asset_id)
    media_path.write_bytes(b"damaged managed copy")
    upload_id = upload(context, wave_bytes(), filename="重新上传.wav")
    payload["media_upload_id"] = upload_id
    original_replace = os.replace
    def fail_repair(source, destination):
        if Path(destination) == media_path:
            raise PermissionError("simulated locked destination")
        return original_replace(source, destination)
    monkeypatch.setattr(module.os, "replace", fail_repair)
    response = context.client.post("/api/local-media/jobs", json=payload, headers=context.headers)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "media_repair_failed"
    record, fresh = context.workspace.upload(upload_id, "media")
    assert not record.get("asset_id")
    assert fresh.read_bytes() == wave_bytes()
    assert context.workspace.export_path(asset_id, "transcript.json").read_bytes() == snapshot
    assert context.workspace.notes(asset_id) == notes
    assert not context.worker.pending
