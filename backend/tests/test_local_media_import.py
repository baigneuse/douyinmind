"""Local importer integration tests with real ffmpeg and explicitly synthetic media.

No test uses personal media, a real API key, external speech recognition, or a
network request. Injected providers must remain visibly labelled as mocks.
"""

from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess

import pytest

from app.services import local_media_import as importer
from app.services import subtitle_import
from app.services.local_media_import import ImportOptions, LocalImportError, file_sha256, run_import
from app.services.omni_service import VideoAnalysis


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def must_not_call(*args, **kwargs):
    raise AssertionError("This operation must remain offline or reuse the existing result")


@pytest.fixture(autouse=True)
def no_network_or_credentials(monkeypatch):
    for name in ("QWEN_OMNI_API_KEY", "DASHSCOPE_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(socket.socket, "connect", must_not_call)
    monkeypatch.setattr(socket, "create_connection", must_not_call)


@pytest.fixture(scope="session")
def synthetic_media(tmp_path_factory):
    """Encode actual WAV/MP3/M4A/MP4 files; tones are not speech/ASR evidence."""
    fallback = Path(__file__).resolve().parents[3] / ".phase0-tools/bin/ffmpeg.exe"
    located = shutil.which("ffmpeg") or (str(fallback) if fallback.is_file() else None)
    if located is None:
        pytest.fail("Local media integration tests require ffmpeg on PATH")
    executable = Path(located)
    folder = tmp_path_factory.mktemp("synthetic-media")
    files = {}
    for extension, codec in ((".wav", "pcm_s16le"), (".mp3", "libmp3lame"), (".m4a", "aac"), (".mp4", "aac")):
        target = folder / f"synthetic-tone{extension}"
        command = [str(executable), "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
        if extension == ".mp4":
            command += ["-f", "lavfi", "-i", "color=c=black:s=32x32:r=10:d=2"]
        command += ["-f", "lavfi", "-i", "sine=frequency=440:duration=2:sample_rate=16000"]
        if extension == ".mp4":
            command += ["-c:v", "mpeg4", "-shortest"]
        command += ["-c:a", codec, str(target)]
        completed = subprocess.run(command, capture_output=True, timeout=30)
        assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
        files[extension] = target
    return executable, files


@pytest.fixture
def local_case(tmp_path, monkeypatch, synthetic_media):
    executable, samples = synthetic_media
    monkeypatch.setenv("PATH", str(executable.parent) + os.pathsep + os.environ.get("PATH", ""))
    input_dir = tmp_path / "中文输入" / "日本語の音声"
    input_dir.mkdir(parents=True)
    media = input_dir / "合成音声.wav"
    shutil.copyfile(samples[".wav"], media)
    subtitles = input_dir / "已有字幕.srt"
    subtitles.write_text("1\n00:00:00,123 --> 00:00:01,456\n这是测试字幕，数字 42。\n\n2\n00:00:01,456 --> 00:00:01,900\n日本語のテスト字幕。\n", encoding="utf-8")
    return ImportOptions(input=media, subtitles=subtitles, output_dir=tmp_path / "本地输出" / "結果")


def asset_for(options):
    return options.output_dir / ("local-media-" + file_sha256(options.input))


@pytest.mark.parametrize("extension", [".wav", ".mp3", ".m4a", ".mp4"])
def test_offline_import_real_encoded_formats_creates_five_truthful_exports(local_case, synthetic_media, extension, monkeypatch):
    _, samples = synthetic_media
    media = local_case.input.with_suffix(extension)
    if extension != ".wav":
        shutil.copyfile(samples[extension], media)
    options = replace(local_case, input=media)
    original_hash = file_sha256(media)
    subtitle_hash = file_sha256(options.subtitles)
    monkeypatch.setattr(importer, "_transcribe", must_not_call)
    monkeypatch.setattr(importer, "_summary_chat", must_not_call)
    progress = []
    result = run_import(options, progress=progress.append)
    asset = Path(result["output_dir"])
    assert {"source_manifest.json", "transcript.json", "transcript.md", "knowledge.md", "notes.md"} <= {item.name for item in asset.iterdir()}
    transcript = read_json(asset / "transcript.json")
    manifest = read_json(asset / "source_manifest.json")
    assert transcript["origin"] == "subtitles"
    assert transcript["segments"][0]["start_ms"] == 123
    assert transcript["segments"][0]["end_ms"] == 1456
    assert transcript["segments"][1]["text"] == "日本語のテスト字幕。"
    assert manifest["source_type"] == "local"
    assert manifest["source_sha256"] == original_hash
    assert manifest["duration_ms"] >= 1900
    assert manifest["stages"]["transcription"] == "completed"
    assert manifest["stages"]["summary"] == "not_requested"
    assert manifest["transcription"]["parser_version"] == subtitle_import.PARSER_VERSION
    assert manifest["source_integrity"]["unchanged"] is True
    assert manifest["consent"] == {"cloud_asr": False, "cloud_summary": False}
    assert "00:00:00.123" in (asset / "transcript.md").read_text(encoding="utf-8")
    knowledge = (asset / "knowledge.md").read_text(encoding="utf-8")
    assert "未生成 AI 总结" in knowledge
    assert "transcript.md#s000001" in knowledge
    assert "douyin.com" not in knowledge
    assert result["status"] == "transcript_complete_summary_not_generated"
    assert file_sha256(media) == original_hash
    assert file_sha256(options.subtitles) == subtitle_hash
    assert progress


def test_duplicate_import_reuses_audio_and_transcript_and_never_overwrites_notes(local_case, monkeypatch):
    first = run_import(local_case, progress=lambda _: None)
    asset = Path(first["output_dir"])
    notes = asset / "notes.md"
    notes.write_bytes("# 人工笔记\r\n\r\n用户保留：自分のメモ。\r\n".encode("utf-8"))
    notes_before, source_before = notes.read_bytes(), file_sha256(local_case.input)
    transcript_before = (asset / "transcript.json").read_bytes()
    monkeypatch.setattr(importer, "_normalize", must_not_call)
    monkeypatch.setattr(subtitle_import, "parse_subtitles", must_not_call)
    second = run_import(local_case, progress=lambda _: None)
    assert second["output_dir"] == first["output_dir"]
    assert second["transcript_reused"] is True
    assert notes.read_bytes() == notes_before
    assert (asset / "transcript.json").read_bytes() == transcript_before
    assert file_sha256(local_case.input) == source_before


def test_changed_subtitles_invalidate_transcript_but_keep_media_identity(local_case):
    first = run_import(local_case, progress=lambda _: None)
    local_case.subtitles.write_text("1\n00:00:00,456 --> 00:00:01,789\n修订后的真实字幕输入。\n", encoding="utf-8")
    second = run_import(local_case, progress=lambda _: None)
    assert second["output_dir"] == first["output_dir"]
    assert second["transcript_reused"] is False
    transcript = read_json(Path(second["output_dir"]) / "transcript.json")
    assert transcript["segments"][0]["start_ms"] == 456
    assert transcript["segments"][0]["text"] == "修订后的真实字幕输入。"


@pytest.mark.parametrize("module, name", [(subtitle_import, "PARSER_VERSION"), (importer, "PIPELINE_VERSION")])
def test_processing_versions_invalidate_transcript_cache(local_case, monkeypatch, module, name):
    run_import(local_case, progress=lambda _: None)
    monkeypatch.setattr(module, name, "changed-test-version")
    result = run_import(local_case, progress=lambda _: None)
    assert result["transcript_reused"] is False


def test_corrupt_transcript_cache_is_rebuilt_from_subtitles(local_case):
    first = run_import(local_case, progress=lambda _: None)
    asset = Path(first["output_dir"])
    (asset / "transcript.json").write_text("{incomplete", encoding="utf-8")
    result = run_import(local_case, progress=lambda _: None)
    assert result["transcript_reused"] is False
    assert read_json(asset / "transcript.json")["segments"][0]["start_ms"] == 123


@pytest.mark.parametrize("limit, code", [("MAX_MEDIA_BYTES", "invalid_file"), ("MAX_DURATION_MS", "invalid_duration")])
def test_media_size_and_duration_limits_are_enforced(local_case, monkeypatch, limit, code):
    monkeypatch.setattr(importer, limit, 1000)
    before = file_sha256(local_case.input)
    with pytest.raises(LocalImportError) as caught:
        run_import(local_case, progress=lambda _: None)
    assert caught.value.code == code
    assert file_sha256(local_case.input) == before


def test_missing_ffmpeg_gives_actionable_error_and_preserves_source(local_case, monkeypatch):
    from app.services import media_service
    def unavailable():
        raise media_service.MediaPipelineError("missing test executable")
    monkeypatch.setattr(media_service, "_resolve_ffmpeg_path", unavailable)
    before = file_sha256(local_case.input)
    with pytest.raises(LocalImportError) as caught:
        run_import(local_case, progress=lambda _: None)
    assert caught.value.code == "ffmpeg_not_found"
    assert "PATH" in str(caught.value)
    assert file_sha256(local_case.input) == before


@pytest.mark.parametrize("kind, expected_code", [("empty", "invalid_file"), ("bad-media", "invalid_media"), ("wrong-type", "unsupported_type")])
def test_invalid_input_is_explicit_and_never_modified(local_case, kind, expected_code):
    if kind == "empty":
        local_case.input.write_bytes(b"")
    elif kind == "bad-media":
        local_case.input.write_bytes(b"not an audio file")
    else:
        local_case = replace(local_case, input=local_case.input.with_suffix(".avi"))
        local_case.input.write_bytes(b"unsupported")
    before = file_sha256(local_case.input)
    with pytest.raises(LocalImportError) as caught:
        run_import(local_case, progress=lambda _: None)
    assert caught.value.code == expected_code
    assert file_sha256(local_case.input) == before
    assert not (asset_for(local_case) / "transcript.json").exists()


@pytest.mark.parametrize("subtitle_content", ["", "WEBVTT\n\nNOTE no cues", "WEBVTT\n\n00:00.000 --> 00:01.000\n<b> </b>"])
def test_empty_transcript_is_not_reported_as_success(local_case, subtitle_content):
    subtitle = local_case.subtitles.with_suffix(".vtt")
    subtitle.write_text(subtitle_content, encoding="utf-8")
    options = replace(local_case, subtitles=subtitle)
    before = file_sha256(options.input)
    with pytest.raises(LocalImportError) as caught:
        run_import(options, progress=lambda _: None)
    assert caught.value.code == "invalid_subtitles"
    assert not (asset_for(options) / "transcript.json").exists()
    assert file_sha256(options.input) == before


@pytest.mark.parametrize("consent, code", [(False, "cloud_asr_not_authorized"), (True, "missing_credentials")])
def test_cloud_asr_requires_consent_and_credentials(local_case, consent, code):
    options = replace(local_case, mode="qwen", subtitles=None, allow_cloud_asr=consent)
    before = file_sha256(options.input)
    with pytest.raises(LocalImportError) as caught:
        run_import(options, progress=lambda _: None)
    assert caught.value.code == code
    assert caught.value.exit_code == 3
    manifest = read_json(asset_for(options) / "source_manifest.json")
    assert manifest["status"] == "blocked"
    assert manifest["stages"]["transcription"] == "blocked"
    assert not (asset_for(options) / "transcript.json").exists()
    assert file_sha256(options.input) == before


@pytest.mark.parametrize("consent, code", [(False, "cloud_summary_not_authorized"), (True, "missing_credentials")])
def test_cloud_summary_block_preserves_successful_transcript(local_case, consent, code):
    options = replace(local_case, summary="deepseek", allow_cloud_summary=consent)
    with pytest.raises(LocalImportError) as caught:
        run_import(options, progress=lambda _: None)
    assert caught.value.code == code
    asset = asset_for(options)
    assert read_json(asset / "transcript.json")["origin"] == "subtitles"
    manifest = read_json(asset / "source_manifest.json")
    assert manifest["stages"]["transcription"] == "completed"
    assert manifest["stages"]["summary"] == "blocked"
    assert "未生成 AI 总结" in (asset / "knowledge.md").read_text(encoding="utf-8")


def mock_analysis(text="这是 Mock 语音替身，不是真实识别。"):
    return VideoAnalysis(text, "", {"transcript_segments": [
        {"start_seconds": 0.123, "end_seconds": 1.456, "text": text}
    ], "warnings": [], "audio_processing": {"test_only": True}})


def successful_mock_summary(**kwargs):
    return json.dumps({"schema_version": 1, "summary": [
        {"text": "测试替身的摘要。", "kind": "source_statement", "segment_ids": ["s000001"]}
    ], "topics": [], "key_arguments": [], "important_numbers": []}, ensure_ascii=False)


def test_injected_asr_is_always_labelled_mock_and_uses_an_owned_copy(local_case):
    options = replace(local_case, mode="qwen", subtitles=None)
    seen = []
    def fake_asr(path, progress):
        assert path != options.input
        assert path.is_relative_to(options.output_dir)
        seen.append(path)
        return mock_analysis()
    result = run_import(options, asr=fake_asr, progress=lambda _: None)
    asset = Path(result["output_dir"])
    assert len(seen) == 1
    assert result["origin"] == "mock"
    assert read_json(asset / "transcript.json")["origin"] == "mock"
    assert read_json(asset / "source_manifest.json")["models"]["transcription"]["provider"] == "mock"
    assert "Mock / 测试替身" in (asset / "transcript.md").read_text(encoding="utf-8")
    assert "Mock / 测试替身" in (asset / "knowledge.md").read_text(encoding="utf-8")


def test_empty_mock_asr_is_a_failure_and_retry_preserves_checkpoint(local_case, monkeypatch):
    options = replace(local_case, mode="qwen", subtitles=None)
    checkpoint_name = "test-only-checkpoint.json"
    def empty_asr(path, progress):
        (path.parent / checkpoint_name).write_text('{"test_only": true}', encoding="utf-8")
        return VideoAnalysis("", "", {"transcript_segments": [], "warnings": []})
    with pytest.raises(LocalImportError) as caught:
        run_import(options, asr=empty_asr, progress=lambda _: None)
    assert caught.value.code == "empty_transcript"
    asset = asset_for(options)
    assert (asset / "cache" / checkpoint_name).exists()
    assert not (asset / "transcript.json").exists()
    monkeypatch.setattr(importer, "_normalize", must_not_call)
    result = run_import(options, asr=lambda *args: mock_analysis(), progress=lambda _: None)
    assert result["origin"] == "mock"
    assert result["transcript_reused"] is False
    assert (asset / "cache" / checkpoint_name).exists()


def test_summary_failure_preserves_transcript_checkpoint_and_resumes_without_asr(local_case, monkeypatch):
    options = replace(local_case, mode="qwen", subtitles=None, summary="deepseek")
    def fake_asr(path, progress):
        (path.parent / "test-only-checkpoint.json").write_text('{"test_only": true}', encoding="utf-8")
        return mock_analysis()
    def broken_summary(**kwargs):
        raise RuntimeError("PRIVATE-ERROR-CONTENT-should-not-be-exported")
    original_hash = file_sha256(options.input)
    with pytest.raises(LocalImportError):
        run_import(options, asr=fake_asr, summary_chat=broken_summary, progress=lambda _: None)
    asset = asset_for(options)
    before = (asset / "transcript.json").read_bytes()
    manifest = read_json(asset / "source_manifest.json")
    assert manifest["stages"]["transcription"] == "completed"
    assert manifest["stages"]["summary"] == "failed"
    assert "PRIVATE-ERROR-CONTENT" not in (asset / "source_manifest.json").read_text(encoding="utf-8")
    assert (asset / "cache/test-only-checkpoint.json").exists()
    (asset / "notes.md").write_text("用户保留的人工记录。", encoding="utf-8")
    monkeypatch.setattr(importer, "_normalize", must_not_call)
    result = run_import(options, asr=must_not_call, summary_chat=successful_mock_summary, progress=lambda _: None)
    assert result["status"] == "completed"
    assert result["transcript_reused"] is True
    assert (asset / "transcript.json").read_bytes() == before
    assert (asset / "notes.md").read_text(encoding="utf-8") == "用户保留的人工记录。"
    assert "Mock" in (asset / "knowledge.md").read_text(encoding="utf-8")
    assert file_sha256(options.input) == original_hash


def test_mock_summary_with_real_subtitles_is_visibly_mock(local_case):
    options = replace(local_case, summary="deepseek")
    result = run_import(options, summary_chat=successful_mock_summary, progress=lambda _: None)
    asset = Path(result["output_dir"])
    assert read_json(asset / "transcript.json")["origin"] == "subtitles"
    assert read_json(asset / "source_manifest.json")["models"]["summary"]["provider"] == "mock"
    assert "Mock" in (asset / "knowledge.md").read_text(encoding="utf-8")


def test_cancellation_retains_owned_checkpoint_and_original_then_can_retry(local_case):
    options = replace(local_case, mode="qwen", subtitles=None)
    before = file_sha256(options.input)
    def cancel_asr(path, progress):
        (path.parent / "test-only-checkpoint.json").write_text('{"test_only": true}', encoding="utf-8")
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        run_import(options, asr=cancel_asr, progress=lambda _: None)
    asset = asset_for(options)
    assert read_json(asset / "source_manifest.json")["status"] == "cancelled"
    assert (asset / "cache/test-only-checkpoint.json").exists()
    assert file_sha256(options.input) == before
    result = run_import(options, asr=lambda *args: mock_analysis(), progress=lambda _: None)
    assert result["status"] == "transcript_complete_summary_not_generated"
    assert file_sha256(options.input) == before


@pytest.mark.parametrize("changed", ["model", "prompt"])
def test_asr_model_or_prompt_changes_invalidate_cached_mock_transcript(local_case, monkeypatch, changed):
    options = replace(local_case, mode="qwen", subtitles=None)
    calls = []
    def fake_asr(*args):
        calls.append(True)
        return mock_analysis()
    run_import(options, asr=fake_asr, progress=lambda _: None)
    if changed == "model":
        options = replace(options, asr_model="changed-test-model")
    else:
        from app.services import omni_service
        monkeypatch.setattr(omni_service, "AUDIO_PROMPT", "changed test prompt")
    result = run_import(options, asr=fake_asr, progress=lambda _: None)
    assert result["transcript_reused"] is False
    assert len(calls) == 2


@pytest.mark.parametrize("changed", ["model", "prompt"])
def test_summary_cache_reuse_and_model_or_prompt_invalidation(local_case, monkeypatch, changed):
    options = replace(local_case, summary="deepseek")
    run_import(options, summary_chat=successful_mock_summary, progress=lambda _: None)
    second = run_import(options, summary_chat=must_not_call, progress=lambda _: None)
    asset = Path(second["output_dir"])
    assert second["transcript_reused"] is True
    assert read_json(asset / "source_manifest.json")["summary_reused"] is True
    if changed == "model":
        options = replace(options, summary_model="changed-test-model")
    else:
        from app.services import local_markdown
        monkeypatch.setattr(local_markdown, "SUMMARY_PROMPT_VERSION", "changed-test-prompt")
    calls = []
    def new_summary(**kwargs):
        calls.append(True)
        return successful_mock_summary(**kwargs)
    third = run_import(options, summary_chat=new_summary, progress=lambda _: None)
    assert third["transcript_reused"] is True
    assert read_json(asset / "source_manifest.json")["summary_reused"] is False
    assert len(calls) == 1


def test_cancellation_at_first_progress_keeps_completed_transcript_and_summary_reusable(local_case):
    options = replace(local_case, mode="qwen", subtitles=None, summary="deepseek")
    calls = {"asr": 0, "summary": 0}
    def counted_asr(*args):
        calls["asr"] += 1
        return mock_analysis()
    def counted_summary(**kwargs):
        calls["summary"] += 1
        return successful_mock_summary(**kwargs)
    first = run_import(options, asr=counted_asr, summary_chat=counted_summary, progress=lambda _: None)
    asset = Path(first["output_dir"])
    transcript_before = (asset / "transcript.json").read_bytes()
    summary_before = (asset / "cache/summary.json").read_bytes()
    progress_calls = []
    def cancel_immediately(message):
        progress_calls.append(message)
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        run_import(options, asr=counted_asr, summary_chat=counted_summary, progress=cancel_immediately)
    assert len(progress_calls) == 1
    assert read_json(asset / "source_manifest.json")["status"] == "cancelled"
    third = run_import(options, asr=counted_asr, summary_chat=counted_summary, progress=lambda _: None)
    assert third["transcript_reused"] is True
    assert read_json(asset / "source_manifest.json")["summary_reused"] is True
    assert calls == {"asr": 1, "summary": 1}
    assert (asset / "transcript.json").read_bytes() == transcript_before
    assert (asset / "cache/summary.json").read_bytes() == summary_before


def test_failed_new_summary_model_cannot_reuse_previous_models_summary_on_retry(local_case):
    options_a = replace(local_case, summary="deepseek", summary_model="test-model-A")
    calls = []
    def summary_a(**kwargs):
        calls.append("A-success")
        summary = json.loads(successful_mock_summary(**kwargs))
        summary["summary"][0]["text"] = "Only model A produced this old summary."
        return json.dumps(summary)
    first = run_import(options_a, summary_chat=summary_a, progress=lambda _: None)
    asset = Path(first["output_dir"])
    transcript_before = (asset / "transcript.json").read_bytes()
    options_b = replace(options_a, summary_model="test-model-B")
    def failed_summary_b(**kwargs):
        calls.append("B-failed")
        raise RuntimeError("Test model B is temporarily unavailable")
    with pytest.raises(LocalImportError):
        run_import(options_b, summary_chat=failed_summary_b, progress=lambda _: None)
    assert read_json(asset / "source_manifest.json")["stages"]["summary"] == "failed"
    def successful_summary_b(**kwargs):
        calls.append("B-success")
        summary = json.loads(successful_mock_summary(**kwargs))
        summary["summary"][0]["text"] = "Only model B produced this new summary."
        return json.dumps(summary)
    third = run_import(options_b, summary_chat=successful_summary_b, progress=lambda _: None)
    assert calls == ["A-success", "B-failed", "B-success"]
    assert third["transcript_reused"] is True
    manifest = read_json(asset / "source_manifest.json")
    assert manifest["summary_reused"] is False
    assert manifest["summary_config"]["model"] == "test-model-B"
    assert read_json(asset / "cache/summary.json")["summary"][0]["text"] == "Only model B produced this new summary."
    assert (asset / "transcript.json").read_bytes() == transcript_before
