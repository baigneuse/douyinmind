"""Local export boundaries: complete text, truthful status, and grounded notes."""
import copy
import json

import pytest

from app.services.local_markdown import (
    SUMMARY_CHUNK_CHARACTERS,
    SUMMARY_PROMPT_VERSION,
    generate_summary,
    render_knowledge,
    render_transcript,
)


@pytest.fixture
def transcript():
    return {
        "schema_version": 1,
        "source_id": "local-sha256:abc123",
        "origin": "subtitles",
        "model": {},
        "segments": [
            {"id": "s000001", "start_ms": 125, "end_ms": 2125, "text": "第一行。\n実験は 12 人。"},
            {"id": "s000002", "start_ms": 3601001, "end_ms": 3602999, "text": "これは私の意見です。"},
        ],
        "text": "第一行。\n実験は 12 人。\nこれは私の意見です。",
        "warnings": [],
    }


@pytest.fixture
def manifest():
    return {
        "source_type": "local",
        "source_id": "local-sha256:abc123",
        "source_sha256": "a" * 64,
        "original_name": "中文と日本語.mp4",
        "duration_ms": 3603000,
        "stages": {},
    }


def summary(segment_id="s000001", text="原文称实验有 12 人。", kind="source_statement"):
    return {
        "schema_version": 1,
        "summary": [{"text": text, "kind": kind, "segment_ids": [segment_id]}],
        "topics": [],
        "key_arguments": [],
        "important_numbers": [],
    }


def test_complete_transcript_preserves_multiline_text_milliseconds_and_local_source(manifest, transcript):
    result = render_transcript(manifest, transcript)
    assert "00:00:00.125 – 00:00:02.125" in result
    assert "01:00:01.001 – 01:00:02.999" in result
    assert "第一行。  \n実験は 12 人。" in result
    assert "これは私の意見です。" in result
    assert '<a id="s000002"></a>' in result
    assert "已有字幕（未运行 ASR" in result
    assert "douyin.com" not in result
    assert "中文と日本語\\.mp4" in result


@pytest.mark.parametrize("origin,label", [("subtitles", "已有字幕"), ("qwen_asr", "真实 ASR"), ("mock", "测试替身")])
def test_origin_is_explicit_in_both_exports(manifest, transcript, origin, label):
    transcript["origin"] = origin
    assert label in render_transcript(manifest, transcript)
    assert label in render_knowledge(manifest, transcript)


def test_model_export_does_not_dump_client_configuration(manifest, transcript):
    transcript["model"] = {"provider": "test", "name": "visible", "api_key": "DO-NOT-EXPORT"}
    result = render_transcript(manifest, transcript)
    assert "test / visible" in result
    assert "DO-NOT-EXPORT" not in result


def test_untrusted_source_model_text_and_filename_are_escaped(manifest, transcript):
    hostile = '<script>alert(1)</script> [evil](javascript:alert(1)) **heading**'
    manifest["original_name"] = hostile
    transcript["segments"][0]["text"] = hostile
    transcript["warnings"] = [hostile]
    result = render_transcript(manifest, transcript)
    assert "<script>" not in result
    assert "[evil](javascript:" not in result
    assert "**heading**" not in result
    assert "&lt;script&gt;" in result
    result = render_knowledge(manifest, transcript, summary=summary(text=hostile))
    assert "<script>" not in result
    assert "[evil](javascript:" not in result


def test_no_model_means_all_sections_explicitly_unavailable_with_only_original_numeric_excerpt(manifest, transcript):
    result = render_knowledge(manifest, transcript)
    for title in ("摘要", "主题", "关键论点", "重要数字"):
        assert f"## {title}\n\n未生成 AI 总结。" in result
    assert "确定性摘录，不是 AI 总结" in result
    assert "実験は 12 人。" in result
    assert "(transcript.md#s000001)" in result
    assert "已生成；" not in result


def test_failure_keeps_honest_status_even_if_caller_mislabels_done(manifest, transcript):
    result = render_knowledge(manifest, transcript, summary_status="done", summary_error="服务不可用")
    assert "**未生成 AI 总结**（done）" in result
    assert "服务不可用" in result


def test_numeric_excerpt_does_not_invent_numbers(manifest, transcript):
    transcript["segments"][0]["text"] = "没有明确数字。"
    result = render_knowledge(manifest, transcript)
    assert "未发现含阿拉伯数字的原文片段" in result


def test_classifications_and_each_note_reference_are_visible(manifest, transcript):
    value = summary()
    value["key_arguments"] = [{"text": "这是作者判断。", "kind": "author_opinion", "segment_ids": ["s000002"]}]
    value["important_numbers"] = [{"text": "12 人（未独立核验）。", "kind": "unverified_external_fact", "segment_ids": ["s000001"]}]
    result = render_knowledge(manifest, transcript, summary=value)
    assert "【原文陈述（未经外部核验）】" in result
    assert "【作者观点】" in result
    assert "【未经核实的外部事实】" in result
    assert "[01:00:01.001–01:00:02.999](transcript.md#s000002)" in result
    assert "本次模型输出未列出此类内容" in result


def test_mock_summary_is_explicit_even_when_input_is_real_subtitles(manifest, transcript):
    manifest["models"] = {"summary": {"provider": "mock", "name": "test-double"}}
    result = render_knowledge(manifest, transcript, summary=summary())
    assert "已有字幕" in result
    assert "总结模型为 Mock / 测试替身" in result
    assert "不构成真实 AI 总结验收" in result


def test_generation_uses_only_explicit_callback_and_strict_contract(transcript):
    calls = []
    def chat(**kwargs):
        calls.append(kwargs)
        return json.dumps(summary(), ensure_ascii=False)
    result = generate_summary(transcript, chat)
    assert result == summary()
    assert len(calls) == 1
    assert SUMMARY_PROMPT_VERSION in calls[0]["user_prompt"]
    assert "s000002" in calls[0]["user_prompt"]
    assert calls[0]["temperature"] == 0.0
    assert calls[0]["max_tokens"] == 4096


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(extra="unexpected"),
    lambda value: value.update(schema_version=True),
    lambda value: value.update(summary=[]),
    lambda value: value.update(topics="not-a-list"),
    lambda value: value["summary"][0].update(text="  "),
    lambda value: value["summary"][0].update(kind="verified_fact"),
    lambda value: value["summary"][0].update(segment_ids=[]),
    lambda value: value["summary"][0].update(segment_ids=["s999999"]),
    lambda value: value["summary"][0].update(segment_ids=[{}]),
    lambda value: value["summary"][0].update(untrusted="extra"),
])
def test_invalid_or_unreferenced_ai_output_is_not_success(manifest, transcript, mutation):
    invalid = summary()
    mutation(invalid)
    with pytest.raises(ValueError):
        generate_summary(transcript, lambda **_: json.dumps(invalid))
    with pytest.raises(ValueError):
        render_knowledge(manifest, transcript, summary=invalid)


@pytest.mark.parametrize("response", ["", "```json\n{}\n```", '{"schema_version":1,"schema_version":1}', None])
def test_non_json_or_ambiguous_output_fails(transcript, response):
    with pytest.raises(ValueError):
        generate_summary(transcript, lambda **_: response)


def test_long_transcript_preserves_every_character_and_original_ids_without_aggregation_truncation(transcript):
    # Quotes/backslashes exercise JSON-size accounting and Unicode boundaries.
    original = ('中日本語\\"\n ' * 6000) + "最后一个字符。"
    transcript["segments"][0]["text"] = original
    received = []
    def chat(**kwargs):
        payload = kwargs["user_prompt"].split("SOURCE_SEGMENTS_JSON:\n", 1)[1]
        assert len(payload) <= SUMMARY_CHUNK_CHARACTERS
        batch = json.loads(payload)
        received.extend(batch)
        return json.dumps(summary(segment_id=batch[0]["id"]))
    result = generate_summary(transcript, chat)
    assert "".join(item["text"] for item in received if item["id"] == "s000001") == original
    assert "".join(item["text"] for item in received if item["id"] == "s000002") == transcript["segments"][1]["text"]
    assert len(received) > 2
    assert len(result["summary"]) <= 2  # Identical cited notes are deduplicated.


def test_reference_must_be_in_current_batch_not_merely_somewhere_in_transcript(transcript):
    transcript["segments"][0]["text"] = "中" * 30_000
    with pytest.raises(ValueError, match="outside the provided transcript batch"):
        generate_summary(transcript, lambda **_: json.dumps(summary(segment_id="s000002")))


def test_later_batch_failure_propagates_and_does_not_mutate_transcript(transcript):
    transcript["segments"][0]["text"] = "长" * 30_000
    before = copy.deepcopy(transcript)
    calls = 0
    def chat(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("summary service unavailable")
        return json.dumps(summary())
    with pytest.raises(RuntimeError, match="summary service unavailable"):
        generate_summary(transcript, chat)
    assert calls == 2
    assert transcript == before


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(segments=[]),
    lambda value: value.update(origin="unknown"),
    lambda value: value["segments"][0].update(text=" "),
    lambda value: value["segments"][0].update(start_ms=-1),
    lambda value: value["segments"][0].update(end_ms=0),
    lambda value: value["segments"][0].update(start_ms=True),
    lambda value: value["segments"][0].update(id='bad" onclick="evil'),
    lambda value: value["segments"][1].update(id="s000001"),
])
def test_bad_transcripts_rejected_before_any_model_request(manifest, transcript, mutation):
    mutation(transcript)
    def forbidden(**kwargs):
        pytest.fail("Invalid transcript must not reach the model")
    with pytest.raises(ValueError):
        generate_summary(transcript, forbidden)
    with pytest.raises(ValueError):
        render_transcript(manifest, transcript)


def test_mismatched_source_or_platform_manifest_rejected(manifest, transcript):
    manifest["source_id"] = "different"
    with pytest.raises(ValueError, match="source IDs"):
        render_transcript(manifest, transcript)
    manifest["source_id"] = transcript["source_id"]
    manifest["source_type"] = "douyin"
    with pytest.raises(ValueError, match="local sources"):
        render_knowledge(manifest, transcript)
