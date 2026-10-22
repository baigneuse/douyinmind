"""Offline, read-only subtitle ingestion and malformed-input regressions."""

import hashlib
from pathlib import Path

import pytest

from app.services.subtitle_import import MAX_SUBTITLE_BYTES, parse_subtitles


def subtitle(tmp_path, text, suffix=".srt", encoding="utf-8"):
    path = tmp_path / f"字幕{suffix}"
    path.write_bytes(text.encode(encoding))
    return path


def test_srt_preserves_multiline_unicode_and_exact_milliseconds(tmp_path):
    path = subtitle(tmp_path, "1\r\n00:00:00,123 --> 00:00:01,456\r\n你好，世界。\r\nこんにちは。\r\n\r\n2\r\n00:00:01,456 --> 00:00:03,000\r\n第二句。\r\n", encoding="utf-8-sig")
    assert parse_subtitles(path, 3000) == [
        {"start_ms": 123, "end_ms": 1456, "text": "你好，世界。\nこんにちは。"},
        {"start_ms": 1456, "end_ms": 3000, "text": "第二句。"},
    ]


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "utf-16-be"])
def test_supported_encoding(tmp_path, encoding):
    text = "1\n00:00:00,000 --> 00:00:01,000\n中文と日本語"
    data = text.encode(encoding)
    if encoding == "utf-16-be":
        data = b"\xfe\xff" + data
    path = tmp_path / "字幕.SRT"
    path.write_bytes(data)
    assert parse_subtitles(path, 1000)[0]["text"] == "中文と日本語"


def test_chinese_and_japanese_path_is_never_modified(tmp_path):
    directory = tmp_path / "中文文件夹" / "日本語のフォルダー"
    directory.mkdir(parents=True)
    path = subtitle(directory, "1\n00:00:00,000 --> 00:00:01,000\n実際の字幕内容")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    parse_subtitles(path, 1000)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_vtt_identifiers_settings_blocks_and_markup(tmp_path):
    path = subtitle(tmp_path, """WEBVTT - example
Language: ja

STYLE
::cue { color: lime; }

REGION
id:main
width:80%

NOTE This is metadata, not a transcript.
An ignored note can have multiple lines.

chapter-one
00:00.001 --> 00:02.123 align:start position:10% line:90%
<v Speaker><b>Hello</b> &amp; <c.green>world</c>.</v>
<lang ja><ruby>漢字<rt>かんじ</rt></ruby></lang>

NOTE A later comment.

00:02.123 --> 00:03.456
<00:02.124>Text<br>next line
""", suffix=".vtt")
    assert parse_subtitles(path, 4000) == [
        {"start_ms": 1, "end_ms": 2123, "text": "Speaker: Hello & world.\n漢字(かんじ)"},
        {"start_ms": 2123, "end_ms": 3456, "text": "Text\nnext line"},
    ]


def test_ordered_overlap_and_equal_starts_are_preserved(tmp_path):
    path = subtitle(tmp_path, "1\n00:00:00,000 --> 00:00:03,000\nA\n\n2\n00:00:00,000 --> 00:00:01,000\nB\n\n3\n00:00:01,000 --> 00:00:02,000\nC")
    assert [item["text"] for item in parse_subtitles(path, 3000)] == ["A", "B", "C"]


def test_srt_without_numeric_identifiers(tmp_path):
    path = subtitle(tmp_path, "00:00:00,000 --> 00:00:01,000\nA\n\n00:00:01,000 --> 00:00:02,000\nB")
    assert len(parse_subtitles(path, 2000)) == 2


def test_hours_do_not_wrap(tmp_path):
    path = subtitle(tmp_path, "WEBVTT\n\n01:02:03.004 --> 01:02:04.005\nLong media", suffix=".vtt")
    assert parse_subtitles(path, 4000000)[0] == {"start_ms": 3723004, "end_ms": 3724005, "text": "Long media"}


def test_literal_unknown_tags_and_escaped_tags_are_preserved(tmp_path):
    path = subtitle(tmp_path, "1\n00:00:00,000 --> 00:00:01,000\n<font color=\"red\">A</font> &lt;b&gt; <custom>literal</custom>")
    assert parse_subtitles(path, 1000)[0]["text"] == "A <b> <custom>literal</custom>"


@pytest.mark.parametrize("text, error", [
    ("", "empty"),
    (" \n\t", "no transcription cues"),
    ("1\n00:00:00,000 --> 00:00:01,000", "text is empty"),
    ("1\n00:00:00,000 --> 00:00:01,000\n<b> </b>", "text is empty"),
    ("1\n00:00:01,000 --> 00:00:01,000\nA", "require 0"),
    ("1\n00:00:02,000 --> 00:00:01,000\nA", "require 0"),
    ("1\n-00:00:00,001 --> 00:00:01,000\nA", "invalid timestamp"),
    ("1\n00:00:00,000 --> 00:00:10,001\nA", "media duration"),
    ("1\n00:60:00,000 --> 01:01:00,000\nA", "out of range"),
    ("1\n00:00:60,000 --> 00:01:01,000\nA", "out of range"),
    ("1\n00:00:00,00 --> 00:00:01,000\nA", "invalid timestamp"),
    ("1\n00:00:00.000 --> 00:00:01.000\nA", "invalid timestamp"),
    ("1\n00:00:00,000 --> NaN\nA", "invalid timestamp"),
    ("1\n00:00:00,000 --> inf\nA", "invalid timestamp"),
    ("1\n00:00:00,000 -> 00:00:01,000\nA", "invalid timing line"),
    ("garbage\n00:00:00,000 --> 00:00:01,000\nA", "missing or invalid"),
    ("1\n00:00:00,000 --> 00:00:01,000 align:start\nA", "cue settings"),
    ("1\n00:00:00,000 --> 00:00:01,000\nA\n2\n00:00:01,000 --> 00:00:02,000\nB", "cue separators"),
    ("1\n00:00:02,000 --> 00:00:03,000\nA\n\n2\n00:00:01,000 --> 00:00:02,000\nB", "preceding cue"),
])
def test_invalid_srt_fails_without_silently_dropping_cues(tmp_path, text, error):
    with pytest.raises(ValueError, match=error):
        parse_subtitles(subtitle(tmp_path, text), 10000)


@pytest.mark.parametrize("text, error", [
    ("00:00.000 --> 00:01.000\nA", "WEBVTT header"),
    ("WEBVTT\n00:00.000 --> 00:01.000\nA", "blank line"),
    ("WEBVTT", "no transcription cues"),
    ("WEBVTT\n\nNOTE only comment", "no transcription cues"),
    ("WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:900000\n\n00:00.000 --> 00:01.000\nA", "X-TIMESTAMP-MAP"),
    ("WEBVTT\n\nx-timestamp-map=LOCAL:00:00:00.000,MPEGTS:900000\n\n00:00.000 --> 00:01.000\nA", "X-TIMESTAMP-MAP"),
    ("WEBVTT\n\n00:00,000 --> 00:01,000\nA", "invalid timestamp"),
    ("WEBVTT\n\n00:00.000 --> 00:01.000 invalid-setting\nA", "cue settings"),
    ("WEBVTT\n\n00:00.000 --> 00:01.000\nA\n\nSTYLE\n::cue { color: red; }", "STYLE block"),
    ("WEBVTT\n\nREGION\n00:00.000 --> 00:01.000\nA", "REGION block"),
    ("WEBVTT\n\nidentifier\nnot a timing line\ntext", "invalid timing line"),
])
def test_invalid_vtt_is_explicit(tmp_path, text, error):
    with pytest.raises(ValueError, match=error):
        parse_subtitles(subtitle(tmp_path, text, suffix=".vtt"), 10000)


@pytest.mark.parametrize("duration", [0, -1, True, 1.5, float("inf"), float("nan"), "1000", None])
def test_invalid_media_duration(tmp_path, duration):
    path = subtitle(tmp_path, "1\n00:00:00,000 --> 00:00:01,000\nA")
    with pytest.raises(ValueError, match="positive integer"):
        parse_subtitles(path, duration)


@pytest.mark.parametrize("data", [b"\x80\xff\x00", b"1\x00\n\x00", b"\xff\xfe\x00"])
def test_binary_or_invalid_encoding_rejected(tmp_path, data):
    path = tmp_path / "broken.srt"
    path.write_bytes(data)
    with pytest.raises(ValueError, match="UTF"):
        parse_subtitles(path, 1000)


def test_file_type_size_and_existence(tmp_path):
    with pytest.raises(ValueError, match="file type"):
        parse_subtitles(tmp_path / "test.txt", 1000)
    with pytest.raises(ValueError, match="not a readable file"):
        parse_subtitles(tmp_path / "missing.srt", 1000)
    directory = tmp_path / "directory.srt"
    directory.mkdir()
    with pytest.raises(ValueError, match="not a readable file"):
        parse_subtitles(directory, 1000)
    path = tmp_path / "huge.srt"
    with path.open("wb") as stream:
        stream.truncate(MAX_SUBTITLE_BYTES + 1)
    with pytest.raises(ValueError, match="10 MiB"):
        parse_subtitles(path, 1000)


def test_read_permission_failure_is_clear_and_does_not_write(tmp_path, monkeypatch):
    path = subtitle(tmp_path, "1\n00:00:00,000 --> 00:00:01,000\nA")
    def denied_open(self, *args, **kwargs):
        raise PermissionError("read denied")
    monkeypatch.setattr(Path, "open", denied_open)
    with pytest.raises(ValueError, match="Cannot read subtitle.*read denied"):
        parse_subtitles(path, 1000)
