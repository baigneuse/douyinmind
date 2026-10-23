"""Importer integration with per-job runtime control and explicit fake providers.

FFmpeg decodes synthetic local tones only. All model operations are local fakes.
"""
from dataclasses import replace
import json
import os
from threading import Event
from types import SimpleNamespace

import pytest

from app.services import omni_service as omni_module
from app.services.import_runtime import ImportControl, ProviderConfig
from app.services.local_media_import import LocalImportError, file_sha256, run_import
from app.services.omni_service import OmniServiceError, VideoAnalysis
from tests.test_local_media_import import (
    asset_for, local_case, mock_analysis, no_network_or_credentials, read_json, synthetic_media,
)


def test_explicit_provider_configuration_is_used_without_mutating_process_keys(local_case, monkeypatch):
    monkeypatch.setenv("QWEN_OMNI_API_KEY", "process-key-must-not-be-used")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "process-fallback-must-not-be-used")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "process-summary-must-not-be-used")
    before = {key: os.environ[key] for key in ("QWEN_OMNI_API_KEY", "DASHSCOPE_API_KEY", "DEEPSEEK_API_KEY")}
    captured = []

    class OfflineService:
        _client = None

        def __init__(self, configuration=None, request_control=None):
            captured.append((configuration, request_control))

        def analyze_audio(self, path, progress):
            return mock_analysis("Offline provider-injection test")

    monkeypatch.setattr(omni_module, "OmniService", OfflineService)
    options = replace(local_case, mode="qwen", subtitles=None, allow_cloud_asr=True)
    control = ImportControl(max_requests=2)
    run_import(options, control=control, providers=ProviderConfig(asr_key="explicit-test-key"), progress=lambda _: None)
    configuration, actual_control = captured[0]
    assert configuration.qwen_omni_api_key == "explicit-test-key"
    assert actual_control is control
    assert {key: os.environ[key] for key in before} == before
    manifest = (asset_for(options) / "source_manifest.json").read_text(encoding="utf-8")
    assert not any(value in manifest for value in [*before.values(), "explicit-test-key"])


def test_empty_explicit_provider_does_not_fall_back_to_process_credentials(local_case, monkeypatch):
    monkeypatch.setenv("QWEN_OMNI_API_KEY", "process-key-must-not-be-used")
    options = replace(local_case, mode="qwen", subtitles=None, allow_cloud_asr=True)
    with pytest.raises(LocalImportError) as caught:
        run_import(options, control=ImportControl(max_requests=1), providers=ProviderConfig(), progress=lambda _: None)
    assert caught.value.code == "missing_credentials"
    assert os.environ["QWEN_OMNI_API_KEY"] == "process-key-must-not-be-used"


def test_per_job_duration_limit_blocks_asr_and_keeps_original(local_case):
    options = replace(local_case, mode="qwen", subtitles=None, max_duration_ms=1000)
    before = file_sha256(options.input)
    calls = []
    with pytest.raises(LocalImportError) as caught:
        run_import(options, control=ImportControl(max_requests=1),
                   asr=lambda *args: calls.append(args), progress=lambda _: None)
    assert caught.value.code == "invalid_duration"
    assert calls == []
    assert file_sha256(options.input) == before
    assert not (asset_for(options) / "transcript.json").exists()


def test_cancelled_job_never_starts_asr_and_maps_to_cancelled_state(local_case):
    options = replace(local_case, mode="qwen", subtitles=None)
    control = ImportControl(max_requests=1)
    calls = []

    def cancel_at_progress(_):
        control.cancel()

    with pytest.raises(LocalImportError) as caught:
        run_import(options, control=control, asr=lambda *args: calls.append(args), progress=cancel_at_progress)
    assert caught.value.code == "cancelled"
    assert caught.value.exit_code == 130
    assert calls == []
    assert read_json(asset_for(options) / "source_manifest.json")["status"] == "cancelled"


@pytest.mark.parametrize("controlled, expected, exit_code", [(True, "no_speech", 0), (False, "empty_transcript", 1)])
def test_no_speech_is_explicit_for_ui_without_changing_legacy_empty_result(local_case, controlled, expected, exit_code):
    options = replace(local_case, mode="qwen", subtitles=None)
    result = VideoAnalysis("", "", {"transcript_segments": [], "warnings": []})
    with pytest.raises(LocalImportError) as caught:
        run_import(options, control=ImportControl(max_requests=1) if controlled else None,
                   asr=lambda *args: result, progress=lambda _: None)
    assert caught.value.code == expected
    assert caught.value.exit_code == exit_code
    assert not (asset_for(options) / "transcript.json").exists()
    if controlled:
        assert read_json(asset_for(options) / "source_manifest.json")["status"] == "no_speech"


def test_budget_exhaustion_preserves_existing_transcript_and_notes(local_case):
    run_import(local_case, progress=lambda _: None)
    asset = asset_for(local_case)
    notes = asset / "notes.md"
    notes.write_text("Human note must survive a denied summary request.\n", encoding="utf-8")
    transcript_before, notes_before = file_sha256(asset / "transcript.json"), file_sha256(notes)
    control = ImportControl(max_requests=1)
    control.before_request(provider="test", model="test", endpoint="https://example.invalid/v1")

    def exhausted_summary(**kwargs):
        control.before_request(provider="deepseek", model="test", endpoint="https://example.invalid/v1")
        pytest.fail("A fully spent budget admitted another request")

    with pytest.raises(LocalImportError) as caught:
        run_import(replace(local_case, summary="deepseek"), control=control,
                   summary_chat=exhausted_summary, progress=lambda _: None)
    assert caught.value.code == "request_budget_exhausted"
    assert file_sha256(asset / "transcript.json") == transcript_before
    assert file_sha256(notes) == notes_before
    assert read_json(asset / "source_manifest.json")["stages"]["transcription"] == "completed"


def test_provider_error_classification_reaches_importer_without_provider_message(local_case):
    options = replace(local_case, mode="qwen", subtitles=None)

    def failed_provider(*args):
        raise OmniServiceError("PRIVATE_PROVIDER_CANARY", code="rate_limited")

    with pytest.raises(LocalImportError) as caught:
        run_import(options, control=ImportControl(max_requests=1), asr=failed_provider, progress=lambda _: None)
    assert caught.value.code == "rate_limited"
    manifest = read_json(asset_for(options) / "source_manifest.json")
    assert manifest["error"]["code"] == "rate_limited"
    assert "PRIVATE_PROVIDER_CANARY" not in str(caught.value) + json.dumps(manifest)


def test_real_worker_survives_cancelled_import_and_completes_next_media_job(local_case, tmp_path, monkeypatch):
    from app.services import local_media_workspace as workspace_module
    from app.services.local_media_workspace import LocalMediaWorkspace
    from app.services.worker import Worker

    worker = Worker()
    config = SimpleNamespace(qwen_omni_api_key="", dashscope_api_key="",
                             qwen_omni_model="offline-test-model", qwen_omni_base_url="https://example.invalid/v1")
    workspace = LocalMediaWorkspace(tmp_path / "worker-workspace", worker=worker, configuration=config)
    entered, release, cancelled, published = Event(), Event(), Event(), Event()
    calls = []
    real_import = workspace_module.run_import
    real_execute = workspace._execute
    real_publish = workspace._publish_result

    def uploaded(path, kind):
        upload_id, target, _ = workspace.begin_upload(path.name, kind)
        target.write_bytes(path.read_bytes())
        workspace.finish_upload(upload_id, path.name, kind, target.stat().st_size, file_sha256(target))
        return upload_id

    data = {"media_upload_id": uploaded(local_case.input, "media"),
            "subtitle_upload_id": uploaded(local_case.subtitles, "subtitles"),
            "mode": "subtitles", "allow_cloud_asr": False, "max_requests": 1}

    def gated_import(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            entered.set()
            assert release.wait(timeout=5), "The test did not release its first offline import"
        return real_import(*args, **kwargs)

    def observe_execute(job_id, *, task_id):
        real_execute(job_id, task_id=task_id)
        if workspace.job(job_id)["status"] == "cancelled":
            cancelled.set()

    def observe_publish(job_id, result):
        real_publish(job_id, result)
        published.set()

    monkeypatch.setattr(workspace_module, "run_import", gated_import)
    monkeypatch.setattr(workspace, "_execute", observe_execute)
    monkeypatch.setattr(workspace, "_publish_result", observe_publish)
    original_hash = file_sha256(local_case.input)
    worker.start()
    try:
        first = workspace.create_job(data)
        assert entered.wait(timeout=5)
        assert workspace.cancel(first["id"])["status"] == "cancel_requested"
        release.set()
        assert cancelled.wait(timeout=5), "Cancellation must return through the worker, not kill its thread"
        assert workspace.job(first["id"])["status"] == "cancelled"
        assert worker._thread.is_alive()

        second = workspace.create_job(data)
        assert published.wait(timeout=10), "The same worker did not finish the next real offline media import"
        assert workspace.job(second["id"])["status"] == "completed"
        assert workspace.detail(second["asset_id"])["transcript"]["origin"] == "subtitles"
        assert worker._thread.is_alive()
        assert file_sha256(local_case.input) == original_hash
        assert len(calls) == 2
    finally:
        release.set()
        worker.stop()
        worker._thread.join(timeout=3)
    assert not worker._thread.is_alive()
