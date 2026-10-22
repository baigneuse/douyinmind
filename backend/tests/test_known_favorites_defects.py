"""Known Phase 0 defects: desired behavior, with narrowly scoped strict xfails.

These tests use synthetic snapshots, isolated SQLite and ephemeral Chroma.
They do not log in, fetch media, call models, or change existing assertions.
Remove the relevant marker when fixing its issue; an unexpected pass fails CI.
"""
from uuid import uuid4

import chromadb
import pytest
from chromadb.config import Settings as ChromaSettings
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.entities import FavoriteCollection, FavoriteVideo, VideoCache
from app.services import chroma_service
from app.services.douyin_collector import (
    FavoriteScrapeSnapshot,
    FavoriteScrapedCollection,
    FavoriteScrapedVideo,
)
from app.services.favorites_service import FavoritesService
from app.services.knowledge_base_service import knowledge_base_service
from tests.test_knowledge_bases import context  # Reuse the existing SQLite fixture.


class KnownFavoritesDefect(AssertionError):
    """Only a specifically reproduced known defect may be expected to fail."""


@pytest.mark.xfail(
    strict=True,
    raises=KnownFavoritesDefect,
    reason="FAV-001: platform_item_id uniqueness prevents two collection memberships",
)
def test_fav_001_same_video_can_belong_to_two_favorite_collections(context):
    db = context.db
    snapshot = FavoriteScrapeSnapshot(
        collections=[
            FavoriteScrapedCollection("folder-a", "SYNTHETIC A", 1),
            FavoriteScrapedCollection("folder-b", "SYNTHETIC B", 1),
        ],
        videos=[FavoriteScrapedVideo(
            "same-video", "https://example.invalid/synthetic",
            "SYNTHETIC SHARED VIDEO", "SYNTHETIC AUTHOR", 10,
            {"folder-a", "folder-b"},
        )],
    )
    service = FavoritesService()
    collections = service._sync_collections(db, snapshot)
    try:
        service._sync_videos_and_cache(db, snapshot, collections)
        db.commit()
    except IntegrityError as error:
        db.rollback()
        if "UNIQUE constraint failed: favorite_videos.platform_item_id" not in str(error.orig):
            raise  # An unrelated integrity failure must fail normally.
        raise KnownFavoritesDefect("FAV-001: duplicate platform_item_id rejected") from error

    memberships = db.scalars(select(FavoriteVideo).where(
        FavoriteVideo.platform_item_id == "same-video"
    )).all()
    assert {row.collection_id for row in memberships} == {
        collections["folder-a"].id, collections["folder-b"].id,
    }
    assert len(db.scalars(select(VideoCache).where(
        VideoCache.platform_item_id == "same-video"
    )).all()) == 1


@pytest.mark.xfail(
    strict=True,
    raises=KnownFavoritesDefect,
    reason="FAV-002: unfavorite deletes unclassified SQLite cache but leaves Chroma chunks",
)
def test_fav_002_unfavorite_removes_orphan_chunks_and_preserves_named_library(context, monkeypatch):
    db = context.db
    collection = FavoriteCollection(
        platform_collection_id="original", title="SYNTHETIC", video_count=2,
    )
    db.add(collection)
    db.flush()
    db.add_all([FavoriteVideo(
        collection_id=collection.id, platform_item_id=item, title="SYNTHETIC " + item,
    ) for item in ("ready", "pending")])
    pending = db.scalar(select(VideoCache).where(VideoCache.platform_item_id == "pending"))
    pending.status = "done"
    pending.transcript_text = "SYNTHETIC UNCLASSIFIED SAVED TRANSCRIPT"
    library_id = knowledge_base_service.create(db, "SYNTHETIC PRESERVED LIBRARY")["id"]
    knowledge_base_service.assign_videos(db, library_id, ["ready"])
    db.commit()

    client = chromadb.EphemeralClient(ChromaSettings(anonymized_telemetry=False))
    collection_name = "phase1-known-defect-" + uuid4().hex
    index = chroma_service.ChromaService.__new__(chroma_service.ChromaService)
    index._client = client
    index._collection = client.create_collection(collection_name, embedding_function=None)
    # Future cleanup implementations using the existing singleton see this isolated index.
    monkeypatch.setattr(chroma_service, "_chroma_service", index)
    try:
        for item in ("ready", "pending"):
            index.upsert_video_chunks(item, "SYNTHETIC " + item,
                                      ["SYNTHETIC transcript " + item], [[1.0, 0.0]])
        assert index.count() == 2
        service = FavoritesService()
        snapshot = FavoriteScrapeSnapshot(
            collections=[FavoriteScrapedCollection("original", "SYNTHETIC", 0)],
        )
        collections = service._sync_collections(db, snapshot)
        service._sync_videos_and_cache(db, snapshot, collections)
        db.commit()

        assert db.scalars(select(FavoriteVideo)).all() == []
        assert db.scalar(select(VideoCache).where(VideoCache.platform_item_id == "pending")) is None
        preserved = db.scalar(select(VideoCache).where(VideoCache.platform_item_id == "ready"))
        assert preserved is not None and preserved.transcript_text == "保留全部原话"
        documents = index.get_documents()
        assert len([doc for doc in documents if doc["platform_item_id"] == "ready"]) == 1
        orphans = [doc for doc in documents if doc["platform_item_id"] == "pending"]
        if orphans:
            assert [doc["chunk_id"] for doc in orphans] == ["pending:0"]
            raise KnownFavoritesDefect("FAV-002: SQLite deleted pending, Chroma retained pending:0")
        assert index.count() == 1
    finally:
        client.delete_collection(collection_name)
