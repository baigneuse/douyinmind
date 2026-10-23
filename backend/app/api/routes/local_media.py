"""Browser-facing local media API; raw uploads avoid multipart dependencies."""
from __future__ import annotations

import hashlib
import os
import secrets
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from app.services.local_media_workspace import WorkspaceError, get_workspace


def failure(error: WorkspaceError):
    raise HTTPException(status_code=error.status, detail={"code": error.code, "message": str(error)}) from None


def loopback_url(value: str, *, host: bool = False) -> bool:
    try:
        parsed = urlsplit("//" + value if host else value)
        return (parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                and (host or parsed.scheme in {"http", "https"})
                and parsed.username is None and parsed.password is None
                and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
                and (parsed.port is None or 1 <= parsed.port <= 65535))
    except ValueError:
        return False


def local_request(request: Request):
    origin = request.headers.get("origin")
    if (not loopback_url(request.headers.get("host", ""), host=True)
            or (origin is not None and not loopback_url(origin))
            or (origin is None and request.headers.get("sec-fetch-site") == "cross-site")):
        raise HTTPException(403, detail={"code": "local_only", "message": "本功能仅允许本机页面访问。"})
    workspace = get_workspace()
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        supplied = request.headers.get("x-local-media-token", "")
        if not secrets.compare_digest(supplied, workspace.session_token):
            raise HTTPException(403, detail={"code": "invalid_token", "message": "本地会话已变化，请刷新页面后重试。"})
    return workspace


router = APIRouter(prefix="/local-media", tags=["local-media"], dependencies=[Depends(local_request)])


class JobInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    media_upload_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    subtitle_upload_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    mode: Literal["subtitles", "qwen"] = "subtitles"
    allow_cloud_asr: bool = False
    max_requests: int = Field(default=8, ge=1, le=100)


class NotesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=1024**2)
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class RetryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow_cloud_asr: bool = False
    max_requests: int = Field(default=8, ge=1, le=100)


@router.get("/capabilities")
def capabilities():
    return get_workspace().capabilities()


@router.post("/uploads", status_code=201)
async def upload(request: Request, filename: str, kind: Literal["media", "subtitles"]):
    workspace = get_workspace()
    try:
        upload_id, path, limit = workspace.begin_upload(filename, kind)
        size, digest = 0, hashlib.sha256()
        try:
            with path.open("xb") as stream:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > limit:
                        raise WorkspaceError("upload_too_large", "文件超过本地导入大小限制。", 413)
                    stream.write(chunk)
                    digest.update(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if not size:
                raise WorkspaceError("empty_upload", "上传文件为空。")
            return workspace.finish_upload(upload_id, filename, kind, size, digest.hexdigest())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
    except WorkspaceError as error:
        failure(error)


@router.post("/jobs", status_code=202)
def create_job(data: JobInput):
    try:
        return get_workspace().create_job(data.model_dump())
    except WorkspaceError as error:
        failure(error)


@router.get("/jobs")
def jobs():
    try:
        return {"items": get_workspace().list_jobs()}
    except WorkspaceError as error:
        failure(error)


@router.get("/jobs/{job_id}")
def job(job_id: str):
    try:
        return get_workspace().job(job_id)
    except WorkspaceError as error:
        failure(error)


@router.post("/jobs/{job_id}/cancel")
def cancel(job_id: str):
    try:
        return get_workspace().cancel(job_id)
    except WorkspaceError as error:
        failure(error)


@router.post("/jobs/{job_id}/retry", status_code=202)
def retry(job_id: str, data: RetryInput):
    try:
        return get_workspace().retry(job_id, data.model_dump())
    except WorkspaceError as error:
        failure(error)


@router.get("/assets")
def assets():
    try:
        return {"items": get_workspace().list_assets()}
    except WorkspaceError as error:
        failure(error)


@router.get("/assets/{asset_id}")
def asset(asset_id: str):
    try:
        return get_workspace().detail(asset_id)
    except WorkspaceError as error:
        failure(error)


@router.get("/assets/{asset_id}/media")
def media(asset_id: str):
    try:
        path, filename = get_workspace().media_path(asset_id)
        return FileResponse(path, filename=filename, content_disposition_type="inline",
                            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})
    except WorkspaceError as error:
        failure(error)


@router.put("/assets/{asset_id}/notes")
def notes(asset_id: str, data: NotesInput):
    try:
        return get_workspace().save_notes(asset_id, data.text, data.revision)
    except WorkspaceError as error:
        failure(error)


@router.get("/assets/{asset_id}/export/{filename}")
def export(asset_id: str, filename: str):
    try:
        return FileResponse(get_workspace().export_path(asset_id, filename), filename=filename,
                            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})
    except WorkspaceError as error:
        failure(error)
