"""Exercise the real thin CLI in isolated, network-denied child processes.

Fixtures are synthetic silence plus authored test subtitles, not evidence of
speech-recognition quality. These tests do not read or forward any real API key.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import wave

import pytest


BACKEND = Path(__file__).resolve().parents[1]
CLI = BACKEND / "scripts" / "import_local.py"
_GUARD = '''"""Child-process test guard: no outbound Python sockets."""
import builtins
import os
from pathlib import Path
import socket

Path(os.environ["PHASE1_GUARD_LOADED"]).write_text("loaded", encoding="utf-8")

def forbidden_network(*args, **kwargs):
    Path(os.environ["PHASE1_NETWORK_ATTEMPTS"]).write_text("attempted", encoding="utf-8")
    raise RuntimeError("TEST: implicit network access is forbidden")

socket.socket.connect = forbidden_network
socket.socket.connect_ex = forbidden_network
socket.socket.sendto = forbidden_network
socket.create_connection = forbidden_network
socket.getaddrinfo = forbidden_network

if os.environ.get("PHASE1_FORBID_APP_IMPORT") == "1":
    original_import = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        if name == "app" or name.startswith("app."):
            raise RuntimeError("TEST: --help must not import application services")
        return original_import(name, *args, **kwargs)
    builtins.__import__ = guarded_import
'''


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def cli_case(tmp_path):
    folder = tmp_path / "中文输入" / "日本語の音声"
    folder.mkdir(parents=True)
    media = folder / "合成無音テスト.wav"
    with wave.open(str(media), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\x00\x00" * 32000)
    subtitles = folder / "合成测试字幕.srt"
    subtitles.write_text(
        "1\n00:00:00,125 --> 00:00:01,250\n这是合成测试字幕，数字 42。\n\n"
        "2\n00:00:01,250 --> 00:00:01,875\n日本語のテスト字幕です。\n",
        encoding="utf-8",
    )
    cwd = tmp_path / "临时工作目录"
    cwd.mkdir()
    # A normal Settings() load from this cwd would fail integer validation.
    # This file is deliberately synthetic and never touches backend/.env.
    dotenv = cwd / ".env"
    dotenv.write_text("QWEN_OMNI_MAX_TOKENS=INVALID-TEST-NOT-AN-INTEGER\n", encoding="utf-8")
    guard = tmp_path / "network_guard"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(_GUARD, encoding="utf-8")
    # Copy only basic OS process configuration. Do not enumerate or read any
    # credential environment variable from the test runner's environment.
    environment = {
        name: os.environ[name]
        for name in ("SystemRoot", "WINDIR", "PATH", "PATHEXT", "COMSPEC", "TEMP", "TMP", "USERPROFILE")
        if name in os.environ
    }
    fallback = BACKEND.parent.parent / ".phase0-tools" / "bin" / "ffmpeg.exe"
    executable = shutil.which("ffmpeg") or (str(fallback) if fallback.is_file() else None)
    if executable:
        environment["PATH"] = str(Path(executable).parent) + os.pathsep + environment.get("PATH", "")
    environment.update({
        "PYTHONPATH": str(guard),
        "PYTHONNOUSERSITE": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "ANONYMIZED_TELEMETRY": "False",
        "PHASE1_GUARD_LOADED": str(tmp_path / "guard-loaded.txt"),
        "PHASE1_NETWORK_ATTEMPTS": str(tmp_path / "network-attempted.txt"),
    })
    output_dir = tmp_path / "本地输出" / "日本語の結果"

    def run(*arguments, environment_overrides=None):
        child_environment = dict(environment)
        child_environment.update(environment_overrides or {})
        marker = Path(child_environment["PHASE1_GUARD_LOADED"])
        marker.unlink(missing_ok=True)
        completed = subprocess.run(
            [sys.executable, str(CLI), *map(str, arguments)],
            cwd=cwd,
            env=child_environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        assert marker.read_text(encoding="utf-8") == "loaded", completed.stderr
        assert not Path(child_environment["PHASE1_NETWORK_ATTEMPTS"]).exists(), completed.stderr
        assert dotenv.read_text(encoding="utf-8") == "QWEN_OMNI_MAX_TOKENS=INVALID-TEST-NOT-AN-INTEGER\n"
        return completed

    return {
        "media": media,
        "subtitles": subtitles,
        "output": output_dir,
        "run": run,
        "offline_args": ["--input", media, "--subtitles", subtitles, "--output-dir", output_dir],
    }


def test_help_needs_no_application_import_ffmpeg_or_external_service(cli_case):
    result = cli_case["run"]("--help", environment_overrides={"PATH": "", "PHASE1_FORBID_APP_IMPORT": "1"})
    assert result.returncode == 0, result.stderr
    assert "--input" in result.stdout
    assert "--subtitles" in result.stdout
    assert "--allow-cloud-asr" in result.stdout
    assert "不会自动上传" in result.stdout
    assert not cli_case["output"].exists()


def test_default_offline_cli_exports_unicode_paths_and_ignores_dotenv(cli_case):
    before = {name: sha256(cli_case[name]) for name in ("media", "subtitles")}
    result = cli_case["run"](*cli_case["offline_args"])
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    asset = Path(report["output_dir"])
    assert report["origin"] == "subtitles"
    assert report["status"] == "transcript_complete_summary_not_generated"
    assert report["source_unchanged"] is True
    assert {"source_manifest.json", "transcript.json", "transcript.md", "knowledge.md", "notes.md"} <= {
        path.name for path in asset.iterdir()
    }
    transcript = json.loads((asset / "transcript.json").read_text(encoding="utf-8"))
    assert transcript["segments"][0]["start_ms"] == 125
    assert transcript["segments"][0]["end_ms"] == 1250
    assert transcript["segments"][1]["text"] == "日本語のテスト字幕です。"
    assert "00:00:00.125" in (asset / "transcript.md").read_text(encoding="utf-8")
    assert "未生成 AI 总结" in (asset / "knowledge.md").read_text(encoding="utf-8")
    manifest = json.loads((asset / "source_manifest.json").read_text(encoding="utf-8"))
    assert manifest["source_type"] == "local"
    assert manifest["duration_ms"] == 2000
    assert manifest["consent"] == {"cloud_asr": False, "cloud_summary": False}
    assert "离线读取字幕" in result.stderr
    assert {name: sha256(cli_case[name]) for name in before} == before


def test_cli_repeat_reuses_transcript_preserves_original_and_user_notes(cli_case):
    first = cli_case["run"](*cli_case["offline_args"])
    assert first.returncode == 0, first.stderr
    report = json.loads(first.stdout)
    asset = Path(report["output_dir"])
    notes = asset / "notes.md"
    notes.write_bytes("# 人工笔记\r\n\r\n用户笔记を保存してください。\r\n".encode("utf-8"))
    notes_before = notes.read_bytes()
    transcript_before = (asset / "transcript.json").read_bytes()
    original_before = sha256(cli_case["media"])
    second = cli_case["run"](*cli_case["offline_args"])
    assert second.returncode == 0, second.stderr
    second_report = json.loads(second.stdout)
    assert second_report["output_dir"] == report["output_dir"]
    assert second_report["transcript_reused"] is True
    assert "没有新转写请求" in second.stderr
    assert notes.read_bytes() == notes_before
    assert (asset / "transcript.json").read_bytes() == transcript_before
    assert sha256(cli_case["media"]) == original_before


def test_cli_missing_required_input_is_usage_error_before_app_import(cli_case):
    result = cli_case["run"](environment_overrides={"PHASE1_FORBID_APP_IMPORT": "1"})
    assert result.returncode == 2
    assert "--input" in result.stderr
    assert not cli_case["output"].exists()


def test_cli_invalid_media_is_explicit_and_original_bytes_remain_unchanged(cli_case):
    cli_case["media"].write_bytes(b"synthetic invalid WAV test fixture")
    before = cli_case["media"].read_bytes()
    result = cli_case["run"](*cli_case["offline_args"])
    assert result.returncode == 2, result.stderr
    error = json.loads(result.stderr.splitlines()[-1])
    assert error["status"] == "FAILED"
    assert error["error_code"] == "invalid_media"
    assert cli_case["media"].read_bytes() == before


@pytest.mark.parametrize("authorized", [False, True])
def test_cloud_asr_is_blocked_without_consent_or_credentials(cli_case, authorized):
    arguments = ["--input", cli_case["media"], "--output-dir", cli_case["output"], "--mode", "qwen"]
    if authorized:
        arguments.append("--allow-cloud-asr")
    before = sha256(cli_case["media"])
    result = cli_case["run"](*arguments)
    assert result.returncode == 3, result.stderr
    error = json.loads(result.stderr.splitlines()[-1])
    assert error["status"] == "BLOCKED"
    assert error["error_code"] == ("missing_credentials" if authorized else "cloud_asr_not_authorized")
    assert "BLOCKED" in result.stderr
    assert sha256(cli_case["media"]) == before


@pytest.mark.parametrize("authorized", [False, True])
def test_cloud_summary_block_retains_completed_offline_transcript(cli_case, authorized):
    arguments = [*cli_case["offline_args"], "--summary", "deepseek"]
    if authorized:
        arguments.append("--allow-cloud-summary")
    result = cli_case["run"](*arguments)
    assert result.returncode == 3, result.stderr
    error = json.loads(result.stderr.splitlines()[-1])
    assert error["error_code"] == ("missing_credentials" if authorized else "cloud_summary_not_authorized")
    asset = cli_case["output"] / ("local-media-" + sha256(cli_case["media"]))
    assert (asset / "transcript.json").is_file()
    assert (asset / "transcript.md").is_file()
    assert "未生成 AI 总结" in (asset / "knowledge.md").read_text(encoding="utf-8")
    manifest = json.loads((asset / "source_manifest.json").read_text(encoding="utf-8"))
    assert manifest["stages"]["transcription"] == "completed"
    assert manifest["stages"]["summary"] == "blocked"


def test_placeholder_credential_is_blocked_and_not_echoed(cli_case):
    placeholder = "sk-your-synthetic-cli-placeholder-do-not-send"
    result = cli_case["run"](
        "--input", cli_case["media"], "--output-dir", cli_case["output"], "--mode", "qwen", "--allow-cloud-asr",
        environment_overrides={"QWEN_OMNI_API_KEY": placeholder},
    )
    assert result.returncode == 3, result.stderr
    assert json.loads(result.stderr.splitlines()[-1])["error_code"] == "missing_credentials"
    assert placeholder not in result.stdout + result.stderr
