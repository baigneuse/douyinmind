"""Network source collection and explicit public downloads; no ASR pipeline."""
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.routes.local_media import failure, local_request
from app.services.local_media_workspace import WorkspaceError
from app.services.network_library import get_network_library

router = APIRouter(prefix="/network-media", tags=["network-media"], dependencies=[Depends(local_request)])


class SourcesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=20_000)


class SourcePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    title: str | None = Field(default=None, min_length=1, max_length=256)
    tags: list[Annotated[str, Field(min_length=1, max_length=40)]] | None = Field(default=None, max_length=20)
    note: str | None = Field(default=None, max_length=5000)
    starred: bool | None = None
    archived: bool | None = None


@router.get("/capabilities")
def capabilities():
    return get_network_library().capabilities()


@router.post("/sources", status_code=201)
def save_sources(data: SourcesInput):
    try:
        return get_network_library().save_sources(data.text)
    except WorkspaceError as error:
        failure(error)


@router.get("/sources")
def sources():
    try:
        return {"items": get_network_library().list_sources()}
    except WorkspaceError as error:
        failure(error)


@router.get("/sources/{source_id}")
def detail(source_id: str):
    try:
        return get_network_library().detail(source_id)
    except WorkspaceError as error:
        failure(error)


@router.patch("/sources/{source_id}")
def patch(source_id: str, data: SourcePatch):
    try:
        changes = data.model_dump(exclude_unset=True, exclude_none=True, exclude={"expected_revision"})
        return get_network_library().patch(source_id, changes, data.expected_revision)
    except WorkspaceError as error:
        failure(error)


@router.post("/sources/{source_id}/download", status_code=202)
def download(source_id: str):
    try:
        return get_network_library().download(source_id)
    except WorkspaceError as error:
        failure(error)


@router.post("/sources/{source_id}/cancel")
def cancel(source_id: str):
    try:
        return get_network_library().cancel(source_id)
    except WorkspaceError as error:
        failure(error)


@router.get("/sources/{source_id}/media")
def media(source_id: str, v: Annotated[str | None, Query(pattern=r"^[0-9a-f]{64}$")] = None):
    try:
        path, filename, content_type = get_network_library().media_path(source_id, version=v)
        return FileResponse(path, filename=filename, media_type=content_type, content_disposition_type="inline",
                            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})
    except WorkspaceError as error:
        failure(error)


@router.get("/sources/{source_id}/download-file")
def download_file(source_id: str):
    try:
        path, filename, content_type = get_network_library().media_path(source_id)
        return FileResponse(path, filename=filename, media_type=content_type,
                            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})
    except WorkspaceError as error:
        failure(error)
