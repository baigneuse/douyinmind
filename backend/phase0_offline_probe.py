"""Run a synthetic, network-blocked Phase 0 persistence/export probe.

Run from backend with: uv run python phase0_offline_probe.py

This reuses tests.test_omni_ingestion.saved_result. It does NOT transcribe media,
test a provider, evaluate semantic retrieval quality, or exercise the API/UI.
The worker process isolates settings from the caller's environment and .env.
All generated data lives in a temporary directory, removed after worker exit
so Chroma's native file handles have been released on Windows.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from unittest.mock import patch


BACKEND_ROOT = Path(__file__).resolve().parent


def run_worker(root: Path) -> dict:
    # This function runs only in the isolated child. No application module has
    # been imported before the empty working directory and clean environment.
    assert Path.cwd().resolve() == root.resolve()
    assert not (root / ".env").exists()
    sys.path.insert(0, str(BACKEND_ROOT))
    blocked_attempts: list[str] = []

    def forbidden_network(*args, **kwargs):
        blocked_attempts.append("network operation attempted")
        raise AssertionError("Phase 0 probe forbids Python socket networking")

    network_patches = [
        patch.object(socket.socket, "connect", forbidden_network),
        patch.object(socket.socket, "connect_ex", forbidden_network),
        patch.object(socket.socket, "sendto", forbidden_network),
        patch.object(socket, "create_connection", forbidden_network),
        patch.object(socket, "getaddrinfo", forbidden_network),
        patch.object(socket, "gethostbyname", forbidden_network),
    ]
    from contextlib import ExitStack

    with ExitStack() as stack:
        for network_patch in network_patches:
            stack.enter_context(network_patch)
        # Check the guard without attempting an actual connection.
        try:
            socket.create_connection(("127.0.0.1", 9))
        except AssertionError:
            pass
        else:
            raise AssertionError("Network guard was not active")
        assert len(blocked_attempts) == 1
        blocked_attempts.clear()

        from sqlalchemy import select
        from app.core.config import settings
        from app.db.base import Base
        from app.db.session import engine, session_factory
        from app.models.entities import VideoCache
        from app.services import knowledge_service as knowledge_module
        from app.services import markdown_export
        from app.services.chroma_service import get_chroma_service
        from app.services.text_processing import RawSegment, build_chunks_from_segments
        from tests.test_omni_ingestion import saved_result

        assert not any((settings.deepseek_api_key, settings.dashscope_api_key,
                        settings.qwen_omni_api_key))
        assert settings.knowledge_analysis_mode == "audio"
        Base.metadata.create_all(engine)

        # Existing synthetic fixture; relabel its model to avoid suggesting that
        # the fixture's provider name proves an actual ASR request happened.
        analysis = saved_result()
        analysis.payload["model"] = "phase0-fixture-no-asr-request"
        # Adapt the existing mixed-source fixture to the default audio-only
        # ingest mode; preserve its transcript and exact timestamp records.
        analysis.visual_analysis_text = ""
        analysis.payload["visual_segments"] = []
        segments = analysis.payload["transcript_segments"]
        item_id = "phase0-fixture-omni"
        title = "[SYNTHETIC PHASE0] Existing test fixture"
        expected_chunks = [f"视频标题：{title}"] + analysis.knowledge_chunks()
        assert "[00:00–00:05]" in expected_chunks[1]

        # This older helper is a separate check, NOT the current ingest path.
        legacy_chunks = build_chunks_from_segments([
            RawSegment(text=s["text"], start_ms=int(s["start_seconds"] * 1000),
                       end_ms=int(s["end_seconds"] * 1000), lang="zh")
            for s in segments
        ])
        assert len(legacy_chunks) == 1
        assert (legacy_chunks[0].start_ms, legacy_chunks[0].end_ms) == (0, 5000)

        calls = {"download_mock": 0, "analysis_mock": 0, "embedding_mock": 0,
                 "llm_mock": 0}
        embedding_inputs: list[list[str]] = []
        fake_vectors: list[list[float]] = []

        def mock_download(url, source_id):
            assert source_id == item_id
            assert url == f"https://www.douyin.com/video/{item_id}"
            calls["download_mock"] += 1
            placeholder = root / "not-real-media.wav"
            placeholder.write_bytes(b"PHASE0 SYNTHETIC PLACEHOLDER; NOT VALID MEDIA")
            return placeholder

        def mock_analysis(path, progress):
            assert path == root / "not-real-media.wav"
            calls["analysis_mock"] += 1
            return analysis

        def mock_embedding(chunks):
            calls["embedding_mock"] += 1
            embedding_inputs.append(list(chunks))
            # Arbitrary nonzero vectors: storage/query mechanics only. These
            # contain no learned semantics and cannot establish retrieval quality.
            vectors = [[1.0, float(i + 1), 0.5, 0.25] for i in range(len(chunks))]
            fake_vectors[:] = vectors
            return vectors

        def forbidden_model(*args, **kwargs):
            raise AssertionError("Unexpected model or media route")

        stack.enter_context(patch.object(knowledge_module, "download_audio", mock_download))
        stack.enter_context(patch.object(knowledge_module, "download_video", forbidden_model))
        stack.enter_context(patch.object(knowledge_module.omni_service, "analyze_audio", mock_analysis))
        stack.enter_context(patch.object(knowledge_module.omni_service, "analyze_video", forbidden_model))
        stack.enter_context(patch.object(knowledge_module.embedding_client, "embed_texts", mock_embedding))

        with session_factory() as db:
            db.add(VideoCache(platform_item_id=item_id, title=title))
            db.commit()
            service = knowledge_module.KnowledgeService()
            service._process_videos(db, [item_id], "phase0-offline")
            db.expire_all()
            saved = db.scalar(select(VideoCache).where(VideoCache.platform_item_id == item_id))
            assert saved is not None and saved.status == "done"
            assert saved.transcript_text == analysis.transcript_text
            assert json.loads(saved.analysis_json)["transcript_segments"] == segments
            assert saved.visual_analysis_text == analysis.visual_analysis_text
            assert embedding_inputs == [expected_chunks]
            # Exercise the existing done-result reuse guard, without enqueuing.
            service._process_videos(db, [item_id], "phase0-duplicate")
            assert calls == {"download_mock": 1, "analysis_mock": 1,
                             "embedding_mock": 1, "llm_mock": 0}

        # Reload from a new real SQLite session for the exports.
        with session_factory() as db:
            saved = db.scalar(select(VideoCache).where(VideoCache.platform_item_id == item_id))
            with patch.object(markdown_export.llm_client, "chat", forbidden_model):
                original = markdown_export.export_original(saved)
            assert analysis.transcript_text in original
            assert "[00:00–00:05]" in original
            (root / "original.synthetic.md").write_text(original, encoding="utf-8")

            def mock_notes(**kwargs):
                calls["llm_mock"] += 1
                assert analysis.transcript_text in kwargs["user_prompt"]
                return ("### 内容摘要\n[SYNTHETIC MOCK LLM] 测试固定返回，不是模型整理。\n"
                        "### 核心观点\n逐字稿保留最后一句。\n"
                        "### 内容提纲\n第一句；最后一句。\n"
                        "### 行动建议\n暂无明确行动建议")

            with patch.object(markdown_export.llm_client, "chat", mock_notes):
                organized = markdown_export.export_ai_organized(saved)
            assert calls["llm_mock"] == 1
            assert "[SYNTHETIC MOCK LLM]" in organized
            assert analysis.transcript_text in organized
            assert "[00:00–00:05]" in organized
            (root / "notes.synthetic.md").write_text(organized, encoding="utf-8")

        chroma = get_chroma_service()  # The real PersistentClient, not a fake.
        documents = chroma.get_documents(allowed_video_ids={item_id})
        assert chroma.count() == len(expected_chunks) == len(documents)
        assert chroma.get_video_count() == 1
        assert {d["text"] for d in documents} == set(expected_chunks)
        hits = chroma.search(fake_vectors[1], top_k=1, use_mmr=False,
                             allowed_video_ids={item_id})
        assert len(hits) == 1 and hits[0]["chunk_id"] == f"{item_id}:1"
        assert "[00:00–00:05]" in hits[0]["text"]
        metadata = chroma._collection.get(include=["metadatas"])["metadatas"]
        metadata_fields = sorted(metadata[0])
        assert not {"start_ms", "end_ms", "start_seconds", "end_seconds"}.intersection(metadata_fields)
        assert (root / "probe.sqlite3").is_file()
        assert (root / "chroma" / "chroma.sqlite3").is_file()
        assert not blocked_attempts, "Unexpected networking was attempted"
        engine.dispose()

        return {
            "status": "PASS",
            "evidence_level": "synthetic fixture integration; NOT real media end-to-end",
            "fixture": "tests/test_omni_ingestion.py::saved_result",
            "fixture_adaptation": "audio mode: keep transcript/segments; clear visuals; label model as synthetic",
            "real_path": ["KnowledgeService._process_videos", "VideoAnalysis.knowledge_chunks",
                          "build_fixed_chunks", "SQLAlchemy VideoCache commit/reload",
                          "ChromaService.upsert_video_chunks", "ChromaService.get_documents",
                          "ChromaService.search", "export_original", "export_ai_organized"],
            "mock_calls": calls,
            "sqlite_rows": 1,
            "transcript_segments": len(segments),
            "chroma_chunks": chroma.count(),
            "vector_query_hits": len(hits),
            "timestamp_in_sqlite_json": True,
            "timestamp_in_index_text_and_markdown": True,
            "timestamp_metadata_fields": False,
            "chroma_metadata_fields": metadata_fields,
            "legacy_helper_separate_check": "build_chunks_from_segments: 1 chunk, 0..5000 ms",
            "completed_video_reused_without_second_download_or_model_call": True,
            "network_guard": "Python socket connect/connect_ex/sendto/create_connection/DNS blocked; not an OS firewall",
            "unexpected_python_network_attempts": len(blocked_attempts),
            "dotenv_loaded": False,
            "credentials": "empty isolated environment; no account/cookie/key",
            "limitations": ["download is a placeholder, not media processing",
                            "ASR is the existing saved-result fixture",
                            "embedding vectors and organized-note text are mocks",
                            "no model quality, RAG answer, HTTP/UI, or actual media verification",
                            "export still invents a Douyin URL for the synthetic source ID",
                            "timestamps are text in current chunks, not structured index metadata"],
        }


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    if len(sys.argv) == 3 and sys.argv[1] == "--_worker":
        report = run_worker(Path(sys.argv[2]))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if len(sys.argv) != 1:
        raise SystemExit("Usage: uv run python phase0_offline_probe.py")
    # Deliberately inherit only OS runtime variables, never API/account config.
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP",
                                      "COMSPEC", "PATHEXT", "SYSTEMDRIVE"}}
    environment.update(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1",
                       ANONYMIZED_TELEMETRY="False", TOKENIZERS_PARALLELISM="false")
    with tempfile.TemporaryDirectory(prefix="douyinmind-phase0-") as directory:
        root = Path(directory).resolve()
        assert root.parent == Path(tempfile.gettempdir()).resolve()
        environment.update(
            USERPROFILE=str(root), APPDATA=str(root / "appdata"),
            LOCALAPPDATA=str(root / "localappdata"),
            DATABASE_URL=f"sqlite:///{(root / 'probe.sqlite3').as_posix()}",
            CHROMA_PERSIST_DIR=str(root / "chroma"),
            INGESTION_METRICS_DIR=str(root / "ingestion-runs"),
            AUDIO_CACHE_DIR=str(root / "audio"), VIDEO_CACHE_DIR=str(root / "video"),
            PLAYWRIGHT_USER_DATA_DIR=str(root / "browser-profile"),
            PLAYWRIGHT_BROWSERS_PATH=str(root / "browser-binaries"),
            KNOWLEDGE_ANALYSIS_MODE="audio",
        )
        process = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--_worker", str(root)],
            cwd=root, env=environment, capture_output=True, text=True,
            encoding="utf-8", timeout=120,
        )
        if process.stderr:
            print(process.stderr, file=sys.stderr, end="")
        if process.returncode:
            print(process.stdout, end="")
            return process.returncode
        report = json.loads(process.stdout)
        report["temporary_storage_only"] = True
    report["temporary_directory_removed"] = not root.exists()
    assert report["temporary_directory_removed"]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
