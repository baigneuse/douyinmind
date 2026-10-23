"""Managed, local-only media jobs and immutable successful result snapshots.

Jobs are recoverable records, not an automatic retry queue. Credentials and
control objects remain in memory; persisted records never contain credentials.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import tempfile
from threading import RLock
from urllib.parse import urlsplit
from uuid import uuid4

from app.services.local_media_import import ImportOptions, LocalImportError, file_sha256, run_import

MEDIA_BYTES = 500 * 1024**2
SUBTITLE_BYTES = 10 * 1024**2
DURATION_SECONDS = 30 * 60
ACTIVE = {"queued", "running", "cancel_requested"}
EXPORTS = {"source_manifest.json", "transcript.json", "transcript.md", "knowledge.md", "notes.md"}
DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "storage/local_media_workspace"


class WorkspaceError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_text(path: Path, value: str) -> None:
    if path.is_symlink() or os.path.isjunction(path):
        raise WorkspaceError("unsafe_path", "受管理路径无效。")
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_json(path: Path, value: dict) -> None:
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


class LocalMediaWorkspace:
    def __init__(self, root: Path = DEFAULT_ROOT, *, worker=None, configuration=None):
        self.root = Path(root).resolve()
        self._lock = RLock()
        self._controls: dict = {}
        self._worker = worker
        self._configuration = configuration
        self.session_token = secrets.token_urlsafe(32)
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ("uploads", "assets", "jobs", "working", "results"):
            self.path(name).mkdir(exist_ok=True)
        self.recover_interrupted()

    def path(self, *parts: str) -> Path:
        current = self.root
        for part in parts:
            if not part or part in {".", ".."} or "/" in part or "\\" in part:
                raise WorkspaceError("unsafe_path", "受管理路径无效。")
            current = current / part
            if current.is_symlink() or os.path.isjunction(current):
                raise WorkspaceError("unsafe_path", "受管理路径无效。")
        if not current.resolve().is_relative_to(self.root):
            raise WorkspaceError("unsafe_path", "受管理路径无效。")
        return current

    @staticmethod
    def identifier(value: str, length: int = 32) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{%d}" % length, value):
            raise WorkspaceError("not_found", "记录不存在。", 404)
        return value

    def record(self, category: str, identifier: str) -> dict:
        self.identifier(identifier, 64 if category == "assets" else 32)
        path = self.path(category, identifier, "asset.json") if category == "assets" else self.path(category, identifier + ".json")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except FileNotFoundError:
            raise WorkspaceError("not_found", "记录不存在。", 404) from None
        except (ValueError, OSError):
            raise WorkspaceError("invalid_record", "本地记录无法读取，请保留文件并检查存储。", 409) from None

    def configuration(self):
        if self._configuration is None:
            from app.core.config import settings
            return settings
        return self._configuration

    def capabilities(self) -> dict:
        from app.services.media_service import _resolve_ffmpeg_path, MediaPipelineError
        try:
            _resolve_ffmpeg_path()
            ready = True
        except MediaPipelineError:
            ready = False
        config = self.configuration()
        # Only a boolean leaves the server. No credential is copied to records.
        configured = bool((config.qwen_omni_api_key or config.dashscope_api_key).strip())
        try:
            parsed = urlsplit(config.qwen_omni_base_url)
            safe_endpoint = (parsed.scheme == "https" and parsed.hostname and parsed.username is None
                             and parsed.password is None and not parsed.query and not parsed.fragment)
        except ValueError:
            safe_endpoint = False
        return {"ffmpeg_ready": ready, "asr_configured": configured,
                "asr_model": config.qwen_omni_model,
                "asr_endpoint": config.qwen_omni_base_url if safe_endpoint else "无效的 HTTPS 服务地址",
                "limits": {"media_bytes": MEDIA_BYTES, "subtitle_bytes": SUBTITLE_BYTES,
                           "duration_seconds": DURATION_SECONDS},
                "session_token": self.session_token, "cloud_verified": False}

    def begin_upload(self, filename: str, kind: str) -> tuple[str, Path, int]:
        suffix = Path(filename).suffix.lower()
        allowed = {".mp4", ".mp3", ".m4a", ".wav"} if kind == "media" else {".srt", ".vtt"}
        if (kind not in {"media", "subtitles"} or not filename or len(filename) > 240
                or any(char in filename for char in ("/", "\\", "\0", "\r", "\n")) or suffix not in allowed):
            raise WorkspaceError("unsupported_type", "请选择支持的媒体或 SRT/VTT 字幕文件。")
        upload_id = uuid4().hex
        return upload_id, self.path("uploads", upload_id + suffix), MEDIA_BYTES if kind == "media" else SUBTITLE_BYTES

    def finish_upload(self, upload_id: str, filename: str, kind: str, size: int, digest: str) -> dict:
        record = {"upload_id": upload_id, "filename": filename, "kind": kind,
                  "size_bytes": size, "sha256": digest, "created_at": now()}
        write_json(self.path("uploads", upload_id + ".json"), record)
        return {key: record[key] for key in ("upload_id", "filename", "size_bytes")}

    def upload(self, upload_id: str, kind: str) -> tuple[dict, Path]:
        record = self.record("uploads", upload_id)
        if record.get("kind") != kind:
            raise WorkspaceError("invalid_upload", "上传文件类型不匹配。")
        if record.get("asset_id"):
            asset = self.record("assets", record["asset_id"])
            path = self.path("assets", asset["id"], "media" + Path(asset["filename"]).suffix.lower())
        else:
            path = self.path("uploads", upload_id + Path(record["filename"]).suffix.lower())
        if not path.is_file() or file_sha256(path) != record["sha256"]:
            raise WorkspaceError("invalid_upload", "上传内容已变化或丢失，请重新选择文件。", 409)
        return record, path

    def _save_job(self, job: dict) -> None:
        job["updated_at"] = now()
        write_json(self.path("jobs", job["id"] + ".json"), job)

    def list_jobs(self) -> list[dict]:
        with self._lock:
            values = [self.record("jobs", path.stem) for path in self.path("jobs").glob("*.json")]
            return sorted(values, key=lambda value: value["created_at"], reverse=True)

    def job(self, job_id: str) -> dict:
        with self._lock:
            return deepcopy(self.record("jobs", job_id))

    def recover_interrupted(self) -> None:
        with self._lock:
            for job in self.list_jobs():
                if job["status"] in ACTIVE:
                    job.update(status="interrupted", message="上次运行已中断；已有结果保留，请手动重试。",
                               error={"code": "interrupted", "message": "服务重启；未自动重新发送云端请求。"})
                    self._save_job(job)

    @staticmethod
    def public_asset(asset: dict) -> dict:
        return {key: value for key, value in asset.items() if key not in {"result_id"}}

    def list_assets(self) -> list[dict]:
        with self._lock:
            assets = [self.record("assets", path.parent.name) for path in self.path("assets").glob("*/asset.json")]
            return [self.public_asset(asset) for asset in sorted(assets, key=lambda value: value["updated_at"], reverse=True)]

    def create_job(self, data: dict) -> dict:
        from app.services.import_runtime import ImportControl
        with self._lock:
            if any(job["status"] in ACTIVE for job in self.list_jobs()):
                raise WorkspaceError("job_busy", "已有媒体任务正在处理，请等待或取消后重试。", 409)
            mode = data["mode"]
            if mode == "qwen" and not data["allow_cloud_asr"]:
                raise WorkspaceError("cloud_asr_not_authorized", "需要单独确认音频上传及可能费用。", 403)
            if mode == "subtitles" and not data.get("subtitle_upload_id"):
                raise WorkspaceError("missing_subtitles", "字幕模式需要 SRT/VTT 文件。")
            if mode == "qwen" and data.get("subtitle_upload_id"):
                raise WorkspaceError("ambiguous_input", "云端识别模式不接受同时导入字幕。")
            media, source = self.upload(data["media_upload_id"], "media")
            if data.get("subtitle_upload_id"):
                self.upload(data["subtitle_upload_id"], "subtitles")
            asset_id = media["sha256"]
            folder = self.path("assets", asset_id)
            folder.mkdir(exist_ok=True)
            asset_path = self.path("assets", asset_id, "asset.json")
            if asset_path.exists():
                asset = self.record("assets", asset_id)
                temporary = None
                try:
                    managed = self.path("assets", asset_id, "media" + Path(asset["filename"]).suffix.lower())
                    if not managed.is_file() or file_sha256(managed) != asset_id:
                        # A verified fresh upload can repair a lost/damaged managed
                        # copy. Keep the upload until the replacement is verified;
                        # successful results and user notes are never touched.
                        if media.get("asset_id") or (managed.exists() and not managed.is_file()):
                            raise OSError("Managed media cannot be repaired from this upload")
                        descriptor, name = tempfile.mkstemp(prefix=".media-repair-", dir=folder)
                        temporary = Path(name)
                        with os.fdopen(descriptor, "wb") as outgoing, source.open("rb") as incoming:
                            shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
                            outgoing.flush()
                            os.fsync(outgoing.fileno())
                        if file_sha256(temporary) != asset_id:
                            raise OSError("Uploaded content changed during repair")
                        os.replace(temporary, managed)
                        if file_sha256(managed) != asset_id:
                            raise OSError("Managed media changed after repair")
                except (OSError, WorkspaceError):
                    raise WorkspaceError("media_repair_failed", "原媒体副本无法安全恢复；新上传已保留，请检查存储权限后重试。", 409) from None
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
            else:
                asset = {"id": asset_id, "title": Path(media["filename"]).stem,
                         "filename": media["filename"], "size_bytes": media["size_bytes"],
                         "duration_ms": None, "has_result": False, "updated_at": now()}
                managed = self.path("assets", asset_id, "media" + Path(media["filename"]).suffix.lower())
                os.replace(source, managed)
                write_json(asset_path, asset)
            if not media.get("asset_id"):
                # A duplicate upload is redundant after a verified asset exists.
                if source.exists():
                    source.unlink()
                media["asset_id"] = asset_id
                write_json(self.path("uploads", media["upload_id"] + ".json"), media)
            notes_path = self.path("assets", asset_id, "notes.md")
            if not notes_path.exists():
                self._write_notes(asset_id, "# 人工笔记\n\n此文件由你维护；再次导入不会覆盖。\n")
            job_id = uuid4().hex
            job = {"id": job_id, "asset_id": asset_id, "filename": asset["filename"], "title": asset["title"],
                   "status": "queued", "stage": "queued",
                   "message": "等待本地处理。", "progress": None, "error": None,
                   "request_count": 0, "transcript_reused": False, "created_at": now(), "updated_at": now(),
                   "media_upload_id": data["media_upload_id"], "subtitle_upload_id": data.get("subtitle_upload_id"),
                   "mode": mode, "allow_cloud_asr": bool(data["allow_cloud_asr"]), "max_requests": data["max_requests"]}
            self._save_job(job)
            self._controls[job_id] = ImportControl(max_requests=data["max_requests"],
                                                  on_update=lambda state: self._control_update(job_id, state))
            try:
                if self._worker is None:
                    from app.services.worker import worker
                else:
                    worker = self._worker
                worker.submit("local-media-" + job_id, self._execute, job_id)
            except Exception:
                self._controls.pop(job_id, None)
                job.update(status="failed", error={"code": "worker_busy", "message": "工作线程暂时不可用，请重试。"})
                self._save_job(job)
                raise WorkspaceError("worker_busy", "工作线程暂时不可用，请重试。", 409) from None
            return deepcopy(job)

    def _control_update(self, job_id: str, state: dict) -> None:
        with self._lock:
            job = self.record("jobs", job_id)
            if state.get("revision", 0) < job.get("runtime_revision", -1):
                return
            job["runtime_revision"] = state.get("revision", 0)
            job["request_count"] = state.get("request_count", job["request_count"])
            # Runtime metadata contains provider usage and sanitized errors only.
            job["requests"] = state.get("requests", [])
            self._save_job(job)

    def _progress(self, job_id: str, message: str) -> None:
        with self._lock:
            job = self.record("jobs", job_id)
            job["message"] = message
            match = re.search(r"(?:ASR|MOCK)\s+(\d+)/(\d+)", message)
            if match:
                job.update(stage="transcription", progress={"done": int(match[1]), "total": int(match[2])})
            elif "ASR" in message or "字幕" in message or "转写" in message:
                job["stage"] = "transcription"
            elif "保存" in message:
                job["stage"] = "export"
            self._save_job(job)

    def _execute(self, job_id: str, *, task_id: str) -> None:
        from app.services.import_runtime import ProviderConfig, RuntimeControlError
        with self._lock:
            job = self.record("jobs", job_id)
            if job["status"] not in {"queued", "cancel_requested"}:
                return
            control = self._controls[job_id]
            if job["status"] == "queued":
                job.update(status="running", stage="normalization", message="检查媒体并提取音轨。")
                self._save_job(job)
        try:
            control.check()
            asset = self.record("assets", job["asset_id"])
            media_record, media = self.upload(job["media_upload_id"], "media")
            subtitle_record, subtitles = self.upload(job["subtitle_upload_id"], "subtitles") if job.get("subtitle_upload_id") else ({}, None)
            config = self.configuration()
            # Credentials are only read for an explicitly authorized attempt.
            providers = ProviderConfig(asr_key=(config.qwen_omni_api_key or config.dashscope_api_key)
                                       if job["mode"] == "qwen" and job["allow_cloud_asr"] else "",
                                       summary_key="")
            options = ImportOptions(input=media, subtitles=subtitles, output_dir=self.path("working"),
                                    mode=job["mode"], summary="none", title=asset["title"],
                                    allow_cloud_asr=job["allow_cloud_asr"], concurrency=1,
                                    asr_model=config.qwen_omni_model, asr_base_url=config.qwen_omni_base_url,
                                    max_duration_ms=DURATION_SECONDS * 1000,
                                    source_display_name=media_record["filename"],
                                    subtitle_display_name=subtitle_record.get("filename", ""))
            result = run_import(options, progress=lambda message: self._progress(job_id, message),
                                control=control, providers=providers)
            control.check()
            self._publish_result(job_id, result)
        except Exception as error:
            # Never persist repr(error): provider errors may contain input or secrets.
            known = isinstance(error, (LocalImportError, WorkspaceError, RuntimeControlError))
            code = error.code if known else "processing_failed"
            message = str(error) if known else "处理未完成；已有结果保留，请检查配置并重试。"
            with self._lock:
                job = self.record("jobs", job_id)
                status = "cancelled" if code == "cancelled" else "no_speech" if code == "no_speech" else "blocked" if code in {"missing_credentials", "cloud_asr_not_authorized", "request_budget_exhausted"} else "failed"
                job.update(status=status, message=message, error={"code": code, "message": message})
                self._save_job(job)
        finally:
            with self._lock:
                self._controls.pop(job_id, None)

    def _publish_result(self, job_id: str, result: dict) -> None:
        with self._lock:
            job = self.record("jobs", job_id)
            if job["status"] == "cancel_requested":
                raise LocalImportError("已取消；此前成功结果保留。", "cancelled", 130)
            self._controls[job_id].check()
            asset = self.record("assets", job["asset_id"])
            source = self.path("working", "local-media-" + asset["id"])
            manifest = json.loads((source / "source_manifest.json").read_text(encoding="utf-8"))
            if manifest["status"] not in {"completed", "transcript_complete_summary_not_generated"}:
                raise WorkspaceError("incomplete_result", "结果尚未完整保存。", 409)
            target = self.path("results", job_id)
            target.mkdir(exist_ok=False)
            for name in EXPORTS - {"notes.md"}:
                candidate = self.path("working", "local-media-" + asset["id"], name)
                if name != "source_manifest.json" and manifest["artifacts"].get(name) != file_sha256(candidate):
                    raise WorkspaceError("invalid_result", "结果校验失败；保留此前成功结果。", 409)
                shutil.copyfile(candidate, self.path("results", job_id, name))
            # Publish only after every result file has been copied and validated.
            asset.update(result_id=job_id, has_result=True, duration_ms=manifest["duration_ms"],
                         origin=result["origin"], updated_at=now())
            write_json(self.path("assets", asset["id"], "asset.json"), asset)
            job.update(status="completed", stage="completed", message="逐字稿已保存；未生成 AI 总结。",
                       transcript_reused=result.get("transcript_reused", False), error=None)
            self._save_job(job)

    def cancel(self, job_id: str) -> dict:
        with self._lock:
            job = self.record("jobs", job_id)
            if job["status"] not in ACTIVE:
                return deepcopy(job)
            control = self._controls.get(job_id)
            job.update(status="cancel_requested", message="正在取消；已发送的请求可能仍在完成并计费。")
            self._save_job(job)
        if control is not None:
            control.cancel()
        return self.job(job_id)

    def retry(self, job_id: str, data: dict) -> dict:
        previous = self.job(job_id)
        if previous["status"] in ACTIVE:
            raise WorkspaceError("job_busy", "当前任务尚未结束。", 409)
        # The request supplies fresh consent and a new request budget.
        return self.create_job({"media_upload_id": previous["media_upload_id"],
                                "subtitle_upload_id": previous.get("subtitle_upload_id"),
                                "mode": previous["mode"], "allow_cloud_asr": data["allow_cloud_asr"],
                                "max_requests": data["max_requests"]})

    def _write_notes(self, asset_id: str, text: str) -> dict:
        value = {"text": text, "revision": hashlib.sha256(text.encode("utf-8")).hexdigest()}
        atomic_text(self.path("assets", asset_id, "notes.md"), text)
        return value

    def notes(self, asset_id: str) -> dict:
        self.record("assets", asset_id)
        text = self.path("assets", asset_id, "notes.md").read_text(encoding="utf-8")
        return {"text": text, "revision": hashlib.sha256(text.encode("utf-8")).hexdigest()}

    def save_notes(self, asset_id: str, text: str, revision: str) -> dict:
        if len(text.encode("utf-8")) > 1024**2:
            raise WorkspaceError("notes_too_large", "人工笔记不得超过 1 MiB。", 413)
        with self._lock:
            current = self.notes(asset_id)
            if revision != current["revision"]:
                raise WorkspaceError("notes_conflict", "笔记已在其他页面更新，请先重新读取后合并。", 409)
            return self._write_notes(asset_id, text)

    def media_path(self, asset_id: str) -> tuple[Path, str]:
        asset = self.record("assets", asset_id)
        return self.path("assets", asset_id, "media" + Path(asset["filename"]).suffix.lower()), asset["filename"]

    def export_path(self, asset_id: str, filename: str) -> Path:
        if filename not in EXPORTS:
            raise WorkspaceError("not_found", "导出文件不存在。", 404)
        asset = self.record("assets", asset_id)
        if filename == "notes.md":
            return self.path("assets", asset_id, "notes.md")
        if not asset.get("has_result"):
            raise WorkspaceError("no_result", "此媒体尚无成功结果。", 404)
        self.identifier(asset["result_id"])
        return self.path("results", asset["result_id"], filename)

    def detail(self, asset_id: str) -> dict:
        with self._lock:
            asset = self.record("assets", asset_id)
            transcript = knowledge = None
            if asset.get("has_result"):
                transcript = json.loads(self.export_path(asset_id, "transcript.json").read_text(encoding="utf-8"))
                knowledge = self.export_path(asset_id, "knowledge.md").read_text(encoding="utf-8")
            job = next((job for job in self.list_jobs() if job["asset_id"] == asset_id), None)
            return {"asset": self.public_asset(asset), "transcript": transcript, "knowledge": knowledge,
                    "notes": self.notes(asset_id), "job": job}


_workspace: LocalMediaWorkspace | None = None
_workspace_lock = RLock()


def get_workspace() -> LocalMediaWorkspace:
    global _workspace
    with _workspace_lock:
        if _workspace is None:
            _workspace = LocalMediaWorkspace(Path(os.environ.get("DOUYINMIND_LOCAL_MEDIA_ROOT", str(DEFAULT_ROOT))))
        return _workspace
