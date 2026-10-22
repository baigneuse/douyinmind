"""Read local SRT/WebVTT subtitles into the local-media transcript contract.

This module never writes its input or calls external services. Presentation tags
are converted to plain text, while cue boundaries remain integer milliseconds.
Unsupported clock mappings are rejected instead of inventing media-relative
timestamps. A malformed cue fails the entire import; no speech is silently lost.
"""

from __future__ import annotations

import html
import re
from pathlib import Path


PARSER_VERSION = "subtitles-v1"
MAX_SUBTITLE_BYTES = 10 * 1024 * 1024
_SRT_TIME = re.compile(r"(?P<h>\d{2,}):(?P<m>\d{2}):(?P<s>\d{2}),(?P<ms>\d{3})")
_VTT_TIME = re.compile(r"(?:(?P<h>\d{2,}):)?(?P<m>\d{2}):(?P<s>\d{2})\.(?P<ms>\d{3})")
_CLOCK_MAP = re.compile(r"^\s*X-TIMESTAMP-MAP\s*[=:]", re.IGNORECASE | re.MULTILINE)
_INLINE_TIME = re.compile(r"<((?:\d{2,}:)?\d{2}:\d{2}\.\d{3})>")
_VOICE = re.compile(r"<v(?:\.[\w.-]+)*(?:[ \t]+([^<>\r\n]+))?>", re.IGNORECASE)
_FORMAT_TAG = re.compile(
    r"</?(?:b|i|u|c(?:\.[\w.-]+)*|v|lang(?:[ \t]+[^<>\r\n]+)?|ruby|font(?:[ \t]+[^<>\r\n]+)?)[ \t]*>",
    re.IGNORECASE,
)
_VTT_SETTING = re.compile(r"[A-Za-z][A-Za-z0-9_-]*:[^\s:]+")


def _timestamp(value: str, *, webvtt: bool, cue_number: int) -> int:
    match = (_VTT_TIME if webvtt else _SRT_TIME).fullmatch(value)
    if not match:
        raise ValueError(f"Subtitle cue {cue_number}: invalid timestamp {value!r}")
    hours = int(match.group("h") or 0)
    minutes, seconds, milliseconds = (
        int(match.group(name)) for name in ("m", "s", "ms")
    )
    if minutes >= 60 or seconds >= 60:
        raise ValueError(f"Subtitle cue {cue_number}: timestamp out of range: {value!r}")
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + milliseconds


def _plain_text(value: str) -> str:
    # Keep speaker names and ruby readings rather than deleting their content.
    value = _VOICE.sub(lambda match: f"{match.group(1).strip()}: " if match.group(1) else "", value)
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"<rt>", "(", value, flags=re.IGNORECASE)
    value = re.sub(r"</rt>", ")", value, flags=re.IGNORECASE)
    value = _FORMAT_TAG.sub("", value)
    value = _INLINE_TIME.sub("", value)
    # Decode entities last so escaped literal '<b>' remains literal text.
    return html.unescape(value).strip()


def _read_subtitle(path: Path) -> str:
    try:
        if not path.is_file():
            raise ValueError(f"Subtitle is not a readable file: {path}")
        if path.stat().st_size > MAX_SUBTITLE_BYTES:
            raise ValueError("Subtitle exceeds the 10 MiB size limit")
        with path.open("rb") as source:
            data = source.read(MAX_SUBTITLE_BYTES + 1)
    except OSError as exc:
        raise ValueError(f"Cannot read subtitle file {path}: {exc}") from exc
    if len(data) > MAX_SUBTITLE_BYTES:
        raise ValueError("Subtitle exceeds the 10 MiB size limit")
    if not data:
        raise ValueError("Subtitle file is empty")
    try:
        encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        content = data.decode(encoding)
    except UnicodeError as exc:
        raise ValueError("Subtitle must use UTF-8 or UTF-16 with a byte-order mark") from exc
    if "\x00" in content:
        raise ValueError("Subtitle contains NUL characters; use UTF-8 or BOM-marked UTF-16")
    return content.replace("\r\n", "\n").replace("\r", "\n")


def parse_subtitles(path: Path, duration_ms: int) -> list[dict]:
    """Parse a complete SRT/VTT file, raising ``ValueError`` on invalid input.

    ``duration_ms`` is the probed media duration, as a positive integer. Cue
    starts must be nondecreasing; overlaps are valid, but every cue must have a
    nonempty body and satisfy ``0 <= start_ms < end_ms <= duration_ms``.

    SRT accepts numeric cue identifiers or omitted identifiers. WebVTT requires
    its WEBVTT header and accepts cue identifiers, presentation settings, and
    NOTE/STYLE/REGION blocks. STYLE/REGION are only accepted before the first cue.
    Layout settings are syntax-checked and discarded because this output is
    plain text, not a subtitle renderer. X-TIMESTAMP-MAP is unsupported.
    """
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms <= 0:
        raise ValueError("Media duration_ms must be a positive integer")
    path = Path(path)
    if path.suffix.lower() not in {".srt", ".vtt"}:
        raise ValueError("Subtitle file type must be .srt or .vtt")
    content = _read_subtitle(path)
    if _CLOCK_MAP.search(content):
        raise ValueError("X-TIMESTAMP-MAP is unsupported; provide media-relative subtitles")
    webvtt = path.suffix.lower() == ".vtt"
    blocks = re.split(r"\n[ \t]*\n", content.strip())
    if webvtt:
        header_lines = blocks.pop(0).splitlines()
        if not header_lines or not re.fullmatch(r"WEBVTT(?:[ \t]+[^\n]*)?", header_lines[0]):
            raise ValueError("WebVTT file must begin with a WEBVTT header")
        if any("-->" in line for line in header_lines):
            raise ValueError("WebVTT header must be followed by a blank line before cues")

    segments: list[dict] = []
    for block in blocks:
        if not block.strip():
            continue
        lines = block.splitlines()
        first = lines[0].strip()
        if webvtt and (first == "NOTE" or first.startswith(("NOTE ", "NOTE\t"))):
            continue
        if webvtt and first in {"STYLE", "REGION"}:
            if segments or len(lines) < 2 or any("-->" in line for line in lines):
                raise ValueError(f"Invalid WebVTT {first} block; place it before all cues")
            continue

        cue_number = len(segments) + 1
        timing_index = 0 if "-->" in first else 1
        if timing_index and (len(lines) < 2 or (not webvtt and not first.isdecimal())):
            raise ValueError(f"Subtitle cue {cue_number}: missing or invalid timing line")
        timing = lines[timing_index].strip()
        timing_match = re.fullmatch(r"(\S+)[ \t]+-->[ \t]+(\S+)(?:[ \t]+(.*))?", timing)
        if not timing_match:
            raise ValueError(f"Subtitle cue {cue_number}: invalid timing line {timing!r}")
        start_value, end_value, settings = timing_match.groups()
        if settings and (not webvtt or any(not _VTT_SETTING.fullmatch(item) for item in settings.split())):
            raise ValueError(f"Subtitle cue {cue_number}: invalid or unsupported cue settings")
        start_ms = _timestamp(start_value, webvtt=webvtt, cue_number=cue_number)
        end_ms = _timestamp(end_value, webvtt=webvtt, cue_number=cue_number)
        if not 0 <= start_ms < end_ms <= duration_ms:
            raise ValueError(
                f"Subtitle cue {cue_number}: require 0 <= start_ms < end_ms <= media duration "
                f"({duration_ms} ms), got {start_ms}..{end_ms}"
            )
        if segments and start_ms < segments[-1]["start_ms"]:
            raise ValueError(f"Subtitle cue {cue_number}: starts before the preceding cue")
        body = "\n".join(lines[timing_index + 1:])
        if "-->" in body:
            raise ValueError(f"Subtitle cue {cue_number}: unexpected timing arrow in body; check cue separators")
        text = _plain_text(body)
        if not text:
            raise ValueError(f"Subtitle cue {cue_number}: text is empty")
        segments.append({"start_ms": start_ms, "end_ms": end_ms, "text": text})
    if not segments:
        raise ValueError("Subtitle contains no transcription cues")
    return segments
