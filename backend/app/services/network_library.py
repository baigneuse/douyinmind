"""Save network sources independently of downloading, transcription or indexing.

This library never reads login cookies or provider credentials. A successful
download is published separately from its attempt so a retry cannot erase media.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile
from threading import Event, RLock
from uuid import uuid4

from app.services.local_media_workspace import WorkspaceError, get_workspace, now, write_json

ACTIVE = {"queued", "downloading", "cancel_requested"}
PLATFORMS = [{"id": name, "label": label, "download_verified": False} for name, label in (
    ("douyin", "抖音"), ("bilibili", "哔哩哔哩"), ("youtube", "YouTube"),
    ("direct", "公开直链视频"), ("unknown", "其他网站（可保存来源）"))]


class NetworkLibrary:
    def __init__(self, workspace=None, *, worker=None):
        self.workspace = workspace if workspace is not None else get_workspace()
        self._worker = worker
        self._lock = RLock()
        self._cancel: dict[str, Event] = {}
        self.path().mkdir(exist_ok=True)
        for category in ("sources", "media", "attempts"):
            self.path(category).mkdir(exist_ok=True)
        self.recover_interrupted()

    def path(self, *parts: str) -> Path:
        return self.workspace.path("network", *parts)

    def source(self, source_id: str) -> dict:
        self.workspace.identifier(source_id, 64)
        return self._read(self.path("sources", source_id + ".json"))

    @staticmethod
    def _read(path: Path) -> dict:
        import json
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except FileNotFoundError:
            raise WorkspaceError("not_found", "来源记录不存在。", 404) from None
        except (OSError, ValueError):
            raise WorkspaceError("invalid_record", "来源记录无法读取，请保留文件并检查存储。", 409) from None

    def _save_source(self, source: dict) -> None:
        source["updated_at"] = now()
        write_json(self.path("sources", source["id"] + ".json"), source)

    def _attempt(self, attempt_id: str) -> dict:
        self.workspace.identifier(attempt_id)
        return self._read(self.path("attempts", attempt_id, "attempt.json"))

    def _save_attempt(self, attempt: dict) -> None:
        attempt["updated_at"] = now()
        write_json(self.path("attempts", attempt["id"], "attempt.json"), attempt)

    @staticmethod
    def public(source: dict) -> dict:
        value = {key: value for key, value in source.items() if key not in {"media", "title_custom"}}
        value["media_version"] = source.get("media", {}).get("sha256")
        return deepcopy(value)

    def capabilities(self) -> dict:
        from app.services.network_download import downloader_capabilities
        return {**downloader_capabilities(), "max_batch": 20, "platforms": deepcopy(PLATFORMS),
                "limits": {"media_bytes": 500 * 1024**2, "duration_seconds": 7200,
                           "download_seconds": 900, "max_height": 720},
                "session_token": self.workspace.session_token}

    def save_sources(self, text: str) -> dict:
        from app.services.network_download import NetworkDownloadError, parse_sources
        try:
            parsed = parse_sources(text)
        except NetworkDownloadError as error:
            raise WorkspaceError(error.code, str(error)) from None
        if not 1 <= len(parsed) <= 20:
            raise WorkspaceError("invalid_batch", "一次请保存 1–20 个有效链接。")
        with self._lock:
            items, created, duplicates = [], 0, 0
            for entry in parsed:
                canonical = entry["canonical_url"]
                source_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
                path = self.path("sources", source_id + ".json")
                if path.is_file():
                    source = self.source(source_id)
                    duplicates += 1
                else:
                    source = {"id": source_id, "url": entry["url"], "canonical_url": canonical,
                              "platform": entry["platform"], "supported": bool(entry["supported"]),
                              "title": entry.get("title") or canonical, "author": "", "tags": [], "note": "",
                              "starred": False, "archived": False, "revision": 0, "status": "saved", "progress": None,
                              "error": None, "has_media": False, "download_id": None,
                              "filename": None, "size_bytes": None, "duration_ms": None, "content_type": None,
                              "created_at": now(), "updated_at": now()}
                    self._save_source(source)
                    created += 1
                items.append(self.public(source))
            return {"items": items, "created": created, "duplicates": duplicates}

    def list_sources(self) -> list[dict]:
        with self._lock:
            values = [self.source(path.stem) for path in self.path("sources").glob("*.json")]
            return [self.public(value) for value in sorted(values, key=lambda item: item["created_at"], reverse=True)]

    def detail(self, source_id: str) -> dict:
        with self._lock:
            return self.public(self.source(source_id))

    def patch(self, source_id: str, changes: dict, expected_revision: int) -> dict:
        with self._lock:
            source = self.source(source_id)
            if expected_revision != source["revision"]:
                raise WorkspaceError("source_conflict", "来源信息已在其他页面更新；请保留草稿并重新读取后合并。", 409)
            allowed = {"title", "tags", "note", "starred", "archived"}
            if not set(changes).issubset(allowed):
                raise WorkspaceError("invalid_fields", "只能修改标题、标签、笔记、星标和归档状态。")
            if "title" in changes:
                changes["title"] = changes["title"].strip()
                if not changes["title"]:
                    raise WorkspaceError("empty_title", "标题不能为空。")
                source["title_custom"] = True
            if "tags" in changes:
                changes["tags"] = list(dict.fromkeys(tag.strip() for tag in changes["tags"] if tag.strip()))
            source.update(changes)
            source["revision"] += 1
            self._save_source(source)
            return self.public(source)

    def recover_interrupted(self) -> None:
        with self._lock:
            for path in self.path("sources").glob("*.json"):
                source = self.source(path.stem)
                if source["status"] in ACTIVE:
                    source.update(status="interrupted", error={"code": "interrupted", "message": "上次下载已中断；来源和旧媒体保留，请手动重试。"})
                    self._save_source(source)
            for path in self.path("attempts").glob("*/attempt.json"):
                attempt = self._attempt(path.parent.name)
                if attempt["status"] in ACTIVE:
                    attempt.update(status="interrupted", error={"code": "interrupted", "message": "服务重启，未自动重新下载。"})
                    self._save_attempt(attempt)

    def download(self, source_id: str) -> dict:
        with self._lock:
            source = self.source(source_id)
            if source["status"] in ACTIVE:
                raise WorkspaceError("download_busy", "此来源已有下载任务。", 409)
            attempt_id = uuid4().hex
            self.path("attempts", attempt_id).mkdir(exist_ok=False)
            attempt = {"id": attempt_id, "source_id": source_id, "status": "queued", "progress": None,
                       "error": None, "created_at": now(), "updated_at": now()}
            self._save_attempt(attempt)
            source.update(status="queued", progress=None, error=None, download_id=attempt_id)
            self._save_source(source)
            self._cancel[attempt_id] = Event()
            try:
                if self._worker is None:
                    from app.services.worker import worker
                else:
                    worker = self._worker
                worker.submit("network-media-" + attempt_id, self._execute, attempt_id)
            except Exception:
                self._cancel.pop(attempt_id, None)
                error = {"code": "worker_busy", "message": "下载队列暂时不可用，请重试。"}
                attempt.update(status="failed", error=error)
                source.update(status="failed", error=error)
                self._save_attempt(attempt)
                self._save_source(source)
                raise WorkspaceError(error["code"], error["message"], 409) from None
            return self.public(source)

    def cancel(self, source_id: str) -> dict:
        with self._lock:
            source = self.source(source_id)
            if source["status"] not in ACTIVE:
                return self.public(source)
            attempt = self._attempt(source["download_id"])
            event = self._cancel.get(attempt["id"])
            if event is not None:
                event.set()
            status = "cancelled" if source["status"] == "queued" else "cancel_requested"
            source["status"] = attempt["status"] = status
            self._save_attempt(attempt)
            self._save_source(source)
            return self.public(source)

    def _progress(self, attempt_id: str, value: dict) -> None:
        with self._lock:
            attempt = self._attempt(attempt_id)
            source = self.source(attempt["source_id"])
            if source["download_id"] != attempt_id or source["status"] not in ACTIVE:
                return
            # Store only counters; downloader messages and signed URLs are not metadata.
            progress = {key: value[key] for key in ("downloaded_bytes", "total_bytes")
                        if type(value.get(key)) is int and value[key] >= 0}
            attempt["progress"] = source["progress"] = progress or None
            self._save_attempt(attempt)
            self._save_source(source)

    def _execute(self, attempt_id: str, *, task_id: str) -> None:
        from app.services.network_download import NetworkDownloadError, download_media
        with self._lock:
            attempt = self._attempt(attempt_id)
            source = self.source(attempt["source_id"])
            if attempt["status"] != "queued" or source["download_id"] != attempt_id:
                self._cancel.pop(attempt_id, None)
                return
            event = self._cancel[attempt_id]
            source["status"] = attempt["status"] = "downloading"
            self._save_attempt(attempt)
            self._save_source(source)
        try:
            target = self.path("attempts", attempt_id, "download")
            target.mkdir(exist_ok=False)
            result = download_media(source["canonical_url"], target,
                                    progress=lambda value: self._progress(attempt_id, value), cancelled=event.is_set)
            self._publish(attempt_id, result)
        except Exception as error:
            known = isinstance(error, (NetworkDownloadError, WorkspaceError))
            code = error.code if known else "download_failed"
            message = str(error) if known else "下载未完成；来源和已有媒体保留，请稍后重试。"
            status = "cancelled" if event.is_set() or code == "cancelled" else {
                "needs_login": "needs_login", "authentication_required": "needs_login",
                "unsupported": "unsupported", "unsupported_source": "unsupported",
            }.get(code, "failed")
            with self._lock:
                attempt = self._attempt(attempt_id)
                source = self.source(attempt["source_id"])
                attempt.update(status=status, error={"code": code, "message": message})
                self._save_attempt(attempt)
                if source["download_id"] == attempt_id:
                    source.update(status=status, error=attempt["error"])
                    self._save_source(source)
        finally:
            with self._lock:
                self._cancel.pop(attempt_id, None)

    def _publish(self, attempt_id: str, result: dict) -> None:
        from app.services.local_media_import import file_sha256
        with self._lock:
            attempt = self._attempt(attempt_id)
            source = self.source(attempt["source_id"])
            if self._cancel[attempt_id].is_set() or source["status"] == "cancel_requested":
                raise WorkspaceError("cancelled", "已取消下载；此前媒体保留。")
            supplied = Path(result["path"])
            base = self.path("attempts", attempt_id)
            if not supplied.resolve().is_relative_to(base.resolve()):
                raise WorkspaceError("invalid_download", "下载结果越出受管理目录。")
            relative = supplied.relative_to(base)
            supplied = self.path("attempts", attempt_id, *relative.parts)
            if (not supplied.is_file() or supplied.stat().st_size != result["size_bytes"]
                    or not 0 < result["size_bytes"] <= 500 * 1024**2
                    or file_sha256(supplied) != result["sha256"]):
                raise WorkspaceError("invalid_download", "下载文件不完整或校验失败；已有媒体保留。")
            suffix = supplied.suffix.lower()
            if suffix not in {".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi", ".ogv"}:
                raise WorkspaceError("invalid_download", "下载结果不是支持的完整视频文件。")
            folder = self.path("media", source["id"])
            folder.mkdir(exist_ok=True)
            destination = self.path("media", source["id"], attempt_id + suffix)
            descriptor, temporary_name = tempfile.mkstemp(prefix=".publish-", dir=folder)
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as outgoing, supplied.open("rb") as incoming:
                    shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
                if file_sha256(temporary) != result["sha256"]:
                    raise WorkspaceError("invalid_download", "发布前文件校验失败；已有媒体保留。")
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            if not source.get("title_custom") and result.get("title"):
                title = str(result["title"])[:256]
                if source["title"] != title:
                    source["title"] = title
                    source["revision"] += 1
            filename = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", source["title"])[:120].strip(". ") or "network-video"
            source.update(status="ready", error=None, has_media=True,
                          filename=filename + suffix, size_bytes=result["size_bytes"],
                          duration_ms=result.get("duration_ms"), author=str(result.get("author") or "")[:256],
                          content_type={".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm",
                                        ".mkv": "video/x-matroska", ".mov": "video/quicktime",
                                        ".avi": "video/x-msvideo", ".ogv": "video/ogg"}[suffix],
                          media={"filename": destination.name, "sha256": result["sha256"]})
            attempt.update(status="ready", error=None, sha256=result["sha256"], size_bytes=result["size_bytes"])
            self._save_attempt(attempt)
            self._save_source(source)

    def media_path(self, source_id: str, version: str | None = None) -> tuple[Path, str, str]:
        with self._lock:
            source = self.source(source_id)
            if not source.get("has_media") or not source.get("media"):
                raise WorkspaceError("no_media", "此来源尚未成功下载媒体。", 404)
            if version is not None and version != source["media"]["sha256"]:
                raise WorkspaceError("media_version_changed", "媒体版本已更新，请重新载入播放器。", 409)
            path = self.path("media", source_id, source["media"]["filename"])
            if not path.is_file() or path.stat().st_size != source["size_bytes"]:
                raise WorkspaceError("media_unavailable", "受管理媒体已丢失或变化，请重新下载；来源和笔记仍保留。", 409)
            return path, source["filename"], source["content_type"]


_library: NetworkLibrary | None = None
_library_lock = RLock()


def get_network_library() -> NetworkLibrary:
    global _library
    with _library_lock:
        if _library is None:
            _library = NetworkLibrary()
        return _library
