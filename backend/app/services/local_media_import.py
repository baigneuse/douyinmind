"""Local-only media import orchestration; no web ingestion, DB schema or embedding.
Artifacts live under the existing app storage root. Cloud calls require separate
per-invocation consent. Injected test providers are always labelled as mock.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Callable
from urllib.parse import urlsplit

PIPELINE_VERSION = "local-media-v1"
NORMALIZATION_VERSION = "pcm16-mono-16k-v1"
TRANSCRIPT_SCHEMA_VERSION = 1
MAX_MEDIA_BYTES = 2 * 1024**3
MAX_DURATION_MS = 3 * 60 * 60 * 1000
BACKEND_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = BACKEND_ROOT / "app/storage/local_imports"
FORMATS = {".mp4": "mov", ".m4a": "mov", ".mp3": "mp3", ".wav": "wav"}


class LocalImportError(RuntimeError):
    def __init__(self, message: str, code: str = "import_failed", exit_code: int = 1):
        super().__init__(message)
        self.code, self.exit_code = code, exit_code


@dataclass(frozen=True)
class ImportOptions:
    input: Path
    subtitles: Path | None = None
    output_dir: Path = DEFAULT_OUTPUT
    mode: str = "subtitles"
    summary: str = "none"
    allow_cloud_asr: bool = False
    allow_cloud_summary: bool = False
    asr_model: str = "qwen3.8-omni-flash"
    asr_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    summary_model: str = "deepseek-chat"
    summary_base_url: str = "https://api.deepseek.com"
    segment_seconds: int = 60
    concurrency: int = 3
    title: str = ""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fingerprint(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_path(root: Path, *parts: str) -> Path:
    candidate = root.joinpath(*parts)
    if root.is_symlink() or os.path.isjunction(root):
        raise LocalImportError("输出目录不能是符号链接或目录联接。", "unsafe_output")
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink() or os.path.isjunction(current):
            raise LocalImportError("输出文件不能是符号链接或目录联接。", "unsafe_output")
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise LocalImportError("输出路径越出当前资产目录。", "unsafe_output")
    return candidate


def _write_text(path: Path, value: str) -> None:
    if path.is_symlink() or os.path.isjunction(path):
        raise LocalImportError("拒绝写入链接目标。", "unsafe_output")
    descriptor, temporary = tempfile.mkstemp(prefix=".write-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _write_json(path: Path, value: dict) -> None:
    _write_text(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


@contextmanager
def _asset_lock(asset: Path):
    path = _safe_path(asset, ".import.lock")
    with path.open("a+b") as stream:
        stream.seek(0, 2)
        if not stream.tell():
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise LocalImportError("此媒体已有导入任务运行，请等待它结束。", "asset_busy") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def _endpoint(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise LocalImportError("云端地址必须为不含凭证、查询或片段的 HTTPS URL。", "invalid_endpoint", 2)
    return value.rstrip("/")


def _credentials(name: str, fallback: str | None = None) -> str:
    value = (os.environ.get(name) or (os.environ.get(fallback) if fallback else "") or "").strip()
    if not value or value.startswith(("sk-your-", "your-", "offline-", "unit-", "synthetic-")):
        raise LocalImportError("BLOCKED：没有显式配置有效服务凭证；未发送媒体或文本。",
                               "missing_credentials", 3)
    return value


def _normalize(source: Path, cache: Path) -> tuple[Path, int]:
    """Only copied media is passed to ffmpeg; a forced demuxer forbids playlists."""
    from app.services.media_service import MediaPipelineError, _resolve_ffmpeg_path, _valid_wav
    from app.services.audio_media import probe_audio
    target = _safe_path(cache, "normalized.wav")
    try:
        executable = _resolve_ffmpeg_path()
    except MediaPipelineError:
        raise LocalImportError("未找到 ffmpeg；请安装并加入 PATH 后重试。", "ffmpeg_not_found") from None
    with tempfile.TemporaryDirectory(prefix="source-", dir=cache) as folder:
        owned = Path(folder) / ("input" + source.suffix.lower())
        with source.open("rb") as incoming, owned.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
        if file_sha256(owned) != file_sha256(source):
            raise LocalImportError("复制期间原文件发生变化，请在文件稳定后重试。", "source_changed")
        arguments = [executable, "-hide_banner", "-nostdin", "-protocol_whitelist", "file,pipe",
                     "-f", FORMATS[source.suffix.lower()], "-i", str(owned)]
        probe = subprocess.run(arguments, capture_output=True, timeout=30)
        description = probe.stderr.decode("utf-8", errors="replace")
        duration = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", description)
        if not duration or "Audio:" not in description:
            raise LocalImportError("媒体无法解码或不包含音轨。", "invalid_media", 2)
        hours, minutes, seconds = map(float, duration.groups())
        length = (hours * 3600 + minutes * 60 + seconds) * 1000
        if not math.isfinite(length) or not 0 < length <= MAX_DURATION_MS:
            raise LocalImportError("媒体时长必须大于零且不超过三小时。", "invalid_duration", 2)
        partial = Path(folder) / "complete.wav"
        converted = subprocess.run(
            [*arguments[:2], "-nostdin", "-loglevel", "error", "-xerror", "-y",
             *arguments[3:], "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
             "-c:a", "pcm_s16le", str(partial)], capture_output=True, timeout=300)
        if converted.returncode or not _valid_wav(partial):
            raise LocalImportError("音轨提取失败或媒体损坏；未修改原文件。", "invalid_media", 2)
        duration_ms = round(probe_audio(partial) * 1000)
        partial.replace(target)
    return target, duration_ms


def _segments(items: list[dict], duration_ms: int) -> list[dict]:
    if not items:
        raise LocalImportError("未获得非空转写，不会把空结果标记为成功。", "empty_transcript")
    result, previous = [], -1
    for index, item in enumerate(items, 1):
        start, end, text = item.get("start_ms"), item.get("end_ms"), item.get("text")
        if (type(start) is not int or type(end) is not int or not 0 <= start < end <= duration_ms
                or start < previous or not isinstance(text, str) or not text.strip()):
            raise LocalImportError("转写片段为空、倒序或时间越出媒体范围。", "invalid_transcript")
        result.append({"id": f"s{index:06d}", "start_ms": start, "end_ms": end, "text": text.strip()})
        previous = start
    return result


def _transcribe(wav: Path, options: ImportOptions, progress: Callable[[str], None]):
    if not options.allow_cloud_asr:
        raise LocalImportError("BLOCKED：云端 ASR 默认禁用；确认音频上传与可能费用后传 --allow-cloud-asr。",
                               "cloud_asr_not_authorized", 3)
    key = _credentials("QWEN_OMNI_API_KEY", "DASHSCOPE_API_KEY")
    from app.core.config import Settings
    from app.services.omni_service import OmniService
    config = Settings(_env_file=None, deepseek_api_key="", dashscope_api_key="",
                      qwen_omni_api_key=key, qwen_omni_model=options.asr_model,
                      qwen_omni_base_url=options.asr_base_url,
                      qwen_omni_max_tokens=8192, qwen_omni_timeout=180,
                      video_segment_seconds=options.segment_seconds,
                      audio_transcription_concurrency=options.concurrency,
                      audio_cache_dir=str(wav.parent), audio_boundary_review_enabled=False,
                      audio_boundary_batch_enabled=False)
    service = OmniService(configuration=config)
    try:
        return service.analyze_audio(wav, lambda done, total: progress(f"ASR {done}/{total} 段"))
    finally:
        if service._client is not None:
            service._client.close()


def _summary_chat(options: ImportOptions):
    if not options.allow_cloud_summary:
        raise LocalImportError("BLOCKED：云端总结默认禁用；确认文本上传与可能费用后传 --allow-cloud-summary。",
                               "cloud_summary_not_authorized", 3)
    key = _credentials("DEEPSEEK_API_KEY")
    from openai import OpenAI
    client = OpenAI(api_key=key, base_url=options.summary_base_url, max_retries=0, timeout=60)
    def chat(**kwargs):
        system = kwargs.pop("system_prompt")
        user = kwargs.pop("user_prompt")
        response = client.chat.completions.create(
            model=options.summary_model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            **kwargs)
        if not response.choices or response.choices[0].finish_reason != "stop":
            raise LocalImportError("总结响应不完整，已保留逐字稿。", "incomplete_summary")
        return response.choices[0].message.content or ""
    return client, chat


def run_import(options: ImportOptions, *, progress: Callable[[str], None] = print,
               asr=None, summary_chat=None) -> dict:
    """Injected asr/chat are tests only and are unconditionally marked mock."""
    if options.mode not in {"subtitles", "qwen"} or options.summary not in {"none", "deepseek"}:
        raise LocalImportError("未知处理模式。", "invalid_mode", 2)
    if not 5 <= options.segment_seconds <= 120 or not 1 <= options.concurrency <= 4:
        raise LocalImportError("分段需为5–120秒，并发需为1–4。", "invalid_configuration", 2)
    source = Path(options.input).expanduser().resolve()
    if source.suffix.lower() not in FORMATS:
        raise LocalImportError("支持的媒体类型为 MP4、MP3、M4A、WAV。", "unsupported_type", 2)
    try:
        if not source.is_file() or not 0 < source.stat().st_size <= MAX_MEDIA_BYTES:
            raise LocalImportError("输入必须为可读的非空文件，且不超过2 GiB。", "invalid_file", 2)
        source_hash = file_sha256(source)
    except OSError:
        raise LocalImportError("无法读取输入文件，请检查路径和权限。", "unreadable_file", 2) from None
    subtitle_hash = None
    subtitle = None
    if options.mode == "subtitles":
        if options.subtitles is None:
            raise LocalImportError("离线模式需要 --subtitles 指定 SRT/VTT。", "missing_subtitles", 2)
        subtitle = Path(options.subtitles).expanduser().resolve()
        try:
            if not subtitle.is_file() or not 0 < subtitle.stat().st_size <= 10 * 1024**2:
                raise ValueError()
            subtitle_hash = file_sha256(subtitle)
        except (OSError, ValueError):
            raise LocalImportError("字幕必须是可读且非空的文件，不超过10 MiB。", "invalid_subtitles", 2) from None
    elif options.subtitles is not None:
        raise LocalImportError("qwen 模式不能同时指定字幕。", "ambiguous_input", 2)
    if options.mode == "qwen":
        _endpoint(options.asr_base_url)
    if options.summary == "deepseek":
        _endpoint(options.summary_base_url)
    output = Path(options.output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    asset = _safe_path(output, "local-media-" + source_hash)
    asset.mkdir(exist_ok=True)
    # Source files may be located in an output directory; never overwrite them.
    if source.is_relative_to(asset) or (subtitle and subtitle.is_relative_to(asset)):
        raise LocalImportError("输入媒体/字幕不能位于该资产的输出目录内。", "input_output_overlap", 2)
    with _asset_lock(asset):
        return _run_locked(options, source, source_hash, subtitle, subtitle_hash, asset,
                           progress, asr, summary_chat)


def _run_locked(options, source, source_hash, subtitle, subtitle_hash, asset, progress, asr, summary_chat):
    from app.services.media_service import _valid_wav
    from app.services.audio_media import probe_audio
    from app.services.local_markdown import (
        SUMMARY_PROMPT_VERSION, generate_summary, render_knowledge, render_transcript)
    from app.services.subtitle_import import parse_subtitles

    manifest_path = _safe_path(asset, "source_manifest.json")
    notes = _safe_path(asset, "notes.md")
    if not notes.exists():
        with notes.open("x", encoding="utf-8") as stream:
            stream.write("# 人工笔记\n\n此文件由你维护；再次导入不会覆盖。\n")
    previous = {}
    if manifest_path.exists():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    if (not isinstance(previous, dict) or previous.get("source_sha256") != source_hash
            or not isinstance(previous.get("artifacts", {}), dict)):
        previous = {}
    cache = _safe_path(asset, "cache")
    cache.mkdir(exist_ok=True)
    transcript_path = _safe_path(asset, "transcript.json")
    transcript_md = _safe_path(asset, "transcript.md")
    knowledge_path = _safe_path(asset, "knowledge.md")
    summary_path = _safe_path(cache, "summary.json")
    source_id = "local_" + source_hash[:32]
    mode = "mock" if asr is not None and options.mode == "qwen" else (
        "subtitles" if options.mode == "subtitles" else "qwen_asr")
    trans_config = {"pipeline_version": PIPELINE_VERSION, "normalization_version": NORMALIZATION_VERSION,
                    "schema_version": TRANSCRIPT_SCHEMA_VERSION,
                    "origin": mode, "source_sha256": source_hash, "subtitle_sha256": subtitle_hash}
    if options.mode == "qwen":
        from app.services.omni_service import AUDIO_PROMPT
        trans_config.update(model=options.asr_model, endpoint=options.asr_base_url,
                            prompt_sha256=hashlib.sha256(AUDIO_PROMPT.encode()).hexdigest(),
                            segment_seconds=options.segment_seconds, concurrency=options.concurrency,
                            boundary_review=False, max_tokens=8192)
    else:
        from app.services.subtitle_import import PARSER_VERSION
        trans_config["parser_version"] = PARSER_VERSION
    trans_key = fingerprint(trans_config)
    manifest = {
        "schema_version": 1, "source_type": "local", "source_id": source_id, "source_sha256": source_hash,
        "original_name": source.name, "title": options.title or source.stem,
        "size_bytes": source.stat().st_size, "duration_ms": previous.get("duration_ms"),
        "pipeline_version": PIPELINE_VERSION, "normalization_version": NORMALIZATION_VERSION,
        "created_at": previous.get("created_at", _now()), "updated_at": _now(),
        "transcription": trans_config, "transcription_fingerprint": trans_key,
        "models": {"transcription": {"provider": mode, "name": options.asr_model if options.mode == "qwen" else None},
                   "summary": {"provider": "mock" if summary_chat else options.summary,
                               "name": options.summary_model if options.summary != "none" else None}},
        "stages": {"validation": "completed", "normalization": "pending", "transcription": "pending",
                   "summary": "not_requested" if options.summary == "none" else "pending", "export": "pending"},
        "artifacts": (dict(previous.get("artifacts", {}))
                      if previous.get("transcription_fingerprint") == trans_key else {}),
        "status": "running", "warnings": [],
        "consent": {"cloud_asr": options.allow_cloud_asr, "cloud_summary": options.allow_cloud_summary},
    }
    if subtitle:
        manifest["subtitles"] = {"original_name": subtitle.name, "sha256": subtitle_hash, "origin": "existing_subtitles"}
    if previous.get("transcription_fingerprint") == trans_key:
        for name in ("summary_fingerprint", "summary_config"):
            if name in previous:
                manifest[name] = previous[name]
    transcript = None
    stage = "normalization"
    summary_client = None
    def save():
        manifest["updated_at"] = _now()
        _write_json(manifest_path, manifest)
    save()
    try:
        progress("校验与 SHA-256 已完成；开始处理应用副本")
        normalized = _safe_path(cache, "normalized.wav")
        if (previous.get("normalization_version") == NORMALIZATION_VERSION and normalized.is_file()
                and previous.get("normalized_sha256") == file_sha256(normalized) and _valid_wav(normalized)):
            duration_ms = round(probe_audio(normalized) * 1000)
            progress("复用已校验的规范音频")
        else:
            normalized, duration_ms = _normalize(source, cache)
        if file_sha256(source) != source_hash:
            raise LocalImportError("处理期间原文件变化，停止导入。", "source_changed")
        manifest.update(duration_ms=duration_ms, normalized_sha256=file_sha256(normalized))
        manifest["stages"]["normalization"] = "completed"
        save()
        stage = "transcription"
        manifest["stages"][stage] = "running"
        save()
        if (previous.get("transcription_fingerprint") == trans_key and transcript_path.is_file()
                and previous.get("artifacts", {}).get("transcript.json") == file_sha256(transcript_path)):
            try:
                cached = json.loads(transcript_path.read_text(encoding="utf-8"))
                if cached["origin"] == mode and cached["source_id"] == source_id:
                    cached["segments"] = _segments(cached["segments"], duration_ms)
                    transcript = cached
            except (ValueError, KeyError, TypeError, LocalImportError):
                transcript = None
        if transcript is not None:
            progress("复用完整逐字稿；没有新转写请求")
            manifest["transcript_reused"] = True
        else:
            if subtitle:
                progress("离线读取字幕；不调用外部服务")
                try:
                    segments = parse_subtitles(subtitle, duration_ms)
                except (ValueError, OSError):
                    raise LocalImportError("字幕解析失败：格式、编码、时间范围或文字无效。", "invalid_subtitles", 2) from None
                if file_sha256(subtitle) != subtitle_hash:
                    raise LocalImportError("字幕在读取期间变化，请重试。", "source_changed")
                warnings = ["来自已有字幕；不是本工具的 ASR 输出，未验证字幕是否覆盖全部语音。"]
            else:
                progress("准备 ASR：仅在明确授权后发送" if asr is None else "TEST MOCK ASR")
                if asr is not None:
                    result = asr(normalized, lambda done, total: progress(f"MOCK {done}/{total}"))
                else:
                    result = _transcribe(normalized, options, progress)
                segments = [{"start_ms": round(item["start_seconds"] * 1000),
                             "end_ms": round(item["end_seconds"] * 1000), "text": item["text"]}
                            for item in result.payload["transcript_segments"]]
                warnings = list(result.payload.get("warnings", []))
                manifest["audio_processing"] = result.payload.get("audio_processing", {})
            segments = _segments(segments, duration_ms)
            transcript = {"schema_version": 1, "source_id": source_id, "origin": mode,
                          "model": manifest["models"]["transcription"], "segments": segments,
                          "text": "\n".join(item["text"] for item in segments), "warnings": warnings}
            _write_json(transcript_path, transcript)
            manifest["transcript_reused"] = False
        manifest["stages"]["transcription"] = "completed"
        manifest["artifacts"]["transcript.json"] = file_sha256(transcript_path)
        _write_text(transcript_md, render_transcript(manifest, transcript))
        manifest["artifacts"]["transcript.md"] = file_sha256(transcript_md)
        save()  # Never lose the successful transcript if notes fail.
        summary = None
        stage = "summary"
        from app.services import local_markdown
        notes_config = {"transcript_sha256": manifest["artifacts"]["transcript.json"],
                        "prompt_version": SUMMARY_PROMPT_VERSION, "pipeline_version": PIPELINE_VERSION,
                        "summary_implementation_sha256": file_sha256(Path(local_markdown.__file__)),
                        "provider": "mock" if summary_chat else options.summary,
                        "model": options.summary_model if options.summary != "none" else None,
                        "endpoint": options.summary_base_url if options.summary != "none" else None}
        notes_key = fingerprint(notes_config)
        if previous.get("summary_fingerprint") != notes_key:
            # A failed attempt under a new configuration must not certify the
            # previous model's summary as a completed result of this one.
            manifest["artifacts"].pop("summary.json", None)
        manifest.update(summary_config=notes_config, summary_fingerprint=notes_key)
        if options.summary != "none":
            manifest["stages"]["summary"] = "running"
            save()
            if (previous.get("summary_fingerprint") == notes_key and summary_path.is_file()
                    and previous.get("artifacts", {}).get("summary.json") == file_sha256(summary_path)):
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
                progress("复用已有知识笔记；没有新总结请求")
                manifest["summary_reused"] = True
            else:
                if summary_chat is None:
                    summary_client, chat = _summary_chat(options)
                else:
                    chat = summary_chat
                progress("生成结构化知识笔记" if summary_chat is None else "TEST MOCK SUMMARY")
                summary = generate_summary(transcript, chat)
                _write_json(summary_path, summary)
                manifest["summary_reused"] = False
            manifest["stages"]["summary"] = "completed"
            manifest["artifacts"]["summary.json"] = file_sha256(summary_path)
        _write_text(knowledge_path, render_knowledge(
            manifest, transcript, summary=summary, summary_status=manifest["stages"]["summary"]))
        manifest["artifacts"]["knowledge.md"] = file_sha256(knowledge_path)
        manifest["stages"]["export"] = "completed"
        manifest["status"] = "completed" if summary is not None else "transcript_complete_summary_not_generated"
        progress("逐字稿与来源信息已保存" + ("；未生成 AI 总结" if summary is None else "；知识笔记已保存"))
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            status, code, message = "cancelled", "cancelled", "已取消；原文件未修改，已有转写与 checkpoint 保留。"
        elif isinstance(exc, LocalImportError):
            status = "blocked" if exc.exit_code == 3 else "failed"
            code, message = exc.code, str(exc)
        else:
            # SDK/ffmpeg exceptions can include secret keys or source text.
            status, code, message = "failed", "stage_failed", f"{stage} 阶段失败；已有逐字稿与 checkpoint 保留，请检查配置后重试。"
        manifest["stages"][stage] = status
        manifest.update(status=status, error={"code": code, "message": message})
        if transcript is not None:
            _write_text(knowledge_path, render_knowledge(
                manifest, transcript, summary_status=status, summary_error=message))
            manifest["artifacts"]["knowledge.md"] = file_sha256(knowledge_path)
        else:
            _write_text(knowledge_path, "# 知识笔记\n\n未生成 AI 总结。转写尚未完成。\n")
        save()
        if isinstance(exc, (KeyboardInterrupt, SystemExit, LocalImportError)):
            raise
        raise LocalImportError(message, code) from None
    finally:
        if summary_client is not None:
            summary_client.close()
        try:
            after_hash = file_sha256(source)
        except OSError:
            after_hash = None
        unchanged = after_hash == source_hash
        manifest["source_integrity"] = {"sha256_after": after_hash, "unchanged": unchanged}
        if not unchanged:
            manifest.update(status="failed", error={"code": "source_changed", "message": "运行期间原文件已被外部修改。"})
        save()
    if not manifest["source_integrity"]["unchanged"]:
        raise LocalImportError("运行期间原文件已被外部修改；结果未标为完成。", "source_changed")
    return {"output_dir": str(asset), "status": manifest["status"],
            "source_sha256": source_hash, "origin": mode, "segments": len(transcript["segments"]),
            "transcript_reused": manifest.get("transcript_reused", False),
            "summary_status": manifest["stages"]["summary"], "source_unchanged": True}
