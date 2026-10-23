"""Offline request admission, cancellation and provider audit contracts.

No test sends audio, reads credentials, or contacts a model. The SDK-shaped
responses below are deliberate local fakes, never real transcription evidence.
"""
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier, Event, Thread
from types import SimpleNamespace
import wave

import httpx
from openai import APIStatusError, APITimeoutError
import pytest

from app.core.config import Settings
from app.services import omni_service as omni_module
from app.services.audio_media import AudioSegment
from app.services.import_runtime import ImportControl, RuntimeControlError
from app.services.omni_service import OmniService, OmniServiceError
from tests.test_local_media_import import no_network_or_credentials


def admit(control):
    return control.before_request(provider="qwen", model="test-model", endpoint="https://example.invalid/v1")


def test_parallel_admission_cannot_overspend_shared_request_budget():
    control = ImportControl(max_requests=3)
    start = Barrier(12)

    def compete(_):
        start.wait(timeout=5)
        try:
            return admit(control)
        except RuntimeControlError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(compete, range(12)))
    accepted = [value for value in results if isinstance(value, int)]
    assert len(accepted) == len(set(accepted)) == 3
    assert results.count("request_budget_exhausted") == 9
    assert control.snapshot()["request_count"] == 3
    assert len(control.snapshot()["requests"]) == 3


def test_cancellation_refuses_new_work_but_retains_a_finishing_request():
    control = ImportControl(max_requests=2)
    index = admit(control)
    control.cancel()
    for action in (control.check, lambda: admit(control)):
        with pytest.raises(RuntimeControlError) as caught:
            action()
        assert caught.value.code == "cancelled"
    control.finish_request(index, status="completed", request_id="request-finished-after-cancel",
                           usage={"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16})
    snapshot = control.snapshot()
    assert snapshot["cancel_requested"] is True
    assert snapshot["request_count"] == 1
    assert snapshot["requests"][0]["request_id"] == "request-finished-after-cancel"
    assert snapshot["requests"][0]["usage"]["total_tokens"] == 16


def test_audit_and_callback_snapshots_exclude_untrusted_usage_text_and_are_independent():
    updates = []
    control = ImportControl(max_requests=2, on_update=updates.append)
    index = admit(control)
    control.finish_request(index, status="completed", request_id="request-safe",
                           usage={"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10,
                                  "api_key": "PRIVATE_KEY_CANARY", "transcript": "PRIVATE_SPEECH_CANARY",
                                  "prompt_tokens_details": {"audio_tokens": 6,
                                                            "raw": "PRIVATE_AUDIO_CANARY"}})
    snapshot = control.snapshot()
    assert snapshot["requests"][0]["usage"]["total_tokens"] == 10
    encoded = json.dumps([snapshot, updates])
    assert "PRIVATE_" not in encoded
    assert updates
    snapshot["requests"].clear()
    for update in updates:
        update["requests"].clear()
    assert len(control.snapshot()["requests"]) == 1


def test_late_cancellation_snapshot_cannot_replace_completed_request_audit(tmp_path):
    from app.services.local_media_workspace import LocalMediaWorkspace

    workspace = LocalMediaWorkspace(tmp_path / "audit-race-workspace")
    job_id = "a" * 32
    workspace._save_job({"id": job_id, "request_count": 0})
    old_snapshot_waiting, release_old_snapshot = Event(), Event()

    def delayed_update(state):
        if state["cancel_requested"] and state["requests"][0]["status"] == "in_flight":
            old_snapshot_waiting.set()
            assert release_old_snapshot.wait(timeout=5)
        workspace._control_update(job_id, state)

    control = ImportControl(max_requests=2, on_update=delayed_update)
    index = admit(control)
    cancelling = Thread(target=control.cancel)
    cancelling.start()
    try:
        assert old_snapshot_waiting.wait(timeout=5)
        control.finish_request(index, "completed", request_id="request-audit-retained",
                               usage={"total_tokens": 12})
    finally:
        release_old_snapshot.set()
        cancelling.join(timeout=3)
    assert not cancelling.is_alive()
    stored = workspace.job(job_id)["requests"][0]
    assert stored["status"] == "completed"
    assert stored["request_id"] == "request-audit-retained"
    assert stored["usage"]["total_tokens"] == 12


class AuditStream:
    def __init__(self, raw, *, on_read=None):
        self.raw, self.on_read, self.closed = raw, on_read, False

    def __iter__(self):
        if self.on_read:
            self.on_read()
        yield SimpleNamespace(id="response-offline-test", usage=None,
                              choices=[SimpleNamespace(delta=SimpleNamespace(content=self.raw),
                                                       finish_reason=None)])
        yield SimpleNamespace(id="response-offline-test", usage=None,
                              choices=[SimpleNamespace(delta=SimpleNamespace(content=None),
                                                       finish_reason="stop")])
        yield SimpleNamespace(id="response-offline-test", choices=[],
                              usage=SimpleNamespace(model_dump=lambda: {
                                  "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}))

    def close(self):
        self.closed = True


class AuditClient:
    def __init__(self, responses):
        self.responses, self.calls, self.streams = list(responses), [], []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        self.streams.append(response)
        return response


def service_with_control(control, client=None):
    config = Settings(_env_file=None, qwen_omni_api_key="local-test-placeholder",
                      dashscope_api_key="", deepseek_api_key="",
                      qwen_omni_base_url="https://example.invalid/v1",
                      qwen_omni_model="offline-test-model", audio_boundary_review_enabled=False)
    service = OmniService(configuration=config, request_control=control)
    if client is not None:
        service._client = client
    return service


def test_controlled_sdk_disables_hidden_transport_retries(monkeypatch):
    constructor = []

    def fake_openai(**kwargs):
        constructor.append(kwargs)
        return AuditClient([])

    monkeypatch.setattr(omni_module, "OpenAI", fake_openai)
    service_with_control(ImportControl(max_requests=2))._get_client()
    assert constructor[0]["max_retries"] == 0
    legacy = service_with_control(None)
    legacy._get_client()
    assert constructor[1]["max_retries"] == 1


def test_stream_usage_and_id_are_retained_without_recording_request_content():
    control = ImportControl(max_requests=2)
    client = AuditClient([AuditStream("PRIVATE_TRANSCRIPT_CANARY")])
    service = service_with_control(control, client)
    assert service._request([{"type": "text", "text": "PRIVATE_INPUT_CANARY"}]) == "PRIVATE_TRANSCRIPT_CANARY"
    snapshot = control.snapshot()
    assert snapshot["request_count"] == 1
    assert snapshot["requests"][0]["request_id"] == "response-offline-test"
    assert snapshot["requests"][0]["usage"]["total_tokens"] == 15
    assert "PRIVATE_" not in json.dumps(snapshot)
    assert client.streams[0].closed


def write_segment(tmp_path, seconds=16):
    path = tmp_path / "synthetic-silence.wav"
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        output.writeframes(b"\x00\x00" * 16000 * seconds)
    return AudioSegment(path, 0, seconds)


def test_recovery_and_split_requests_obey_the_same_job_budget(tmp_path):
    control = ImportControl(max_requests=3)
    valid = json.dumps({"transcript_segments": [{"start_seconds": 0, "end_seconds": 1,
                                               "text": "Local fake speech"}], "warnings": []})
    client = AuditClient([AuditStream("bad JSON"), AuditStream("bad JSON"), AuditStream(valid)])
    service = service_with_control(control, client)
    with pytest.raises(RuntimeControlError) as caught:
        service._analyze_audio_resilient(write_segment(tmp_path))
    assert caught.value.code == "request_budget_exhausted"
    assert len(client.calls) == control.snapshot()["request_count"] == 3
    assert all(stream.closed for stream in client.streams)


def test_cancel_during_response_prevents_malformed_response_retry(tmp_path):
    control = ImportControl(max_requests=10)
    client = AuditClient([AuditStream("bad JSON", on_read=control.cancel)])
    service = service_with_control(control, client)
    with pytest.raises(RuntimeControlError) as caught:
        service._analyze_audio_resilient(write_segment(tmp_path))
    assert caught.value.code == "cancelled"
    assert len(client.calls) == control.snapshot()["request_count"] == 1
    assert client.streams[0].closed


@pytest.mark.parametrize("status, expected", [(401, "authentication_failed"), (403, "authentication_failed"),
                                             (429, "rate_limited"), (503, "provider_unavailable")])
def test_provider_http_failures_keep_safe_actionable_classification(status, expected):
    control = ImportControl(max_requests=2)
    request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    response = httpx.Response(status, request=request)
    error = APIStatusError("PRIVATE_PROVIDER_ERROR_CANARY", response=response,
                           body={"raw": "PRIVATE_PAYLOAD_CANARY"})
    client = AuditClient([error])
    service = service_with_control(control, client)
    with pytest.raises(OmniServiceError) as caught:
        service._request([{"type": "text", "text": "test"}])
    assert caught.value.code == expected
    assert control.snapshot()["requests"][0]["error_code"] == expected
    assert "PRIVATE_" not in str(caught.value) + json.dumps(control.snapshot())


def test_provider_timeout_is_distinct_from_a_bad_transcript():
    control = ImportControl(max_requests=1)
    error = APITimeoutError(request=httpx.Request("POST", "https://example.invalid/v1"))
    service = service_with_control(control, AuditClient([error]))
    with pytest.raises(OmniServiceError) as caught:
        service._request([{"type": "text", "text": "test"}])
    assert caught.value.code == "provider_timeout"
    assert control.snapshot()["requests"][0]["error_code"] == "provider_timeout"
