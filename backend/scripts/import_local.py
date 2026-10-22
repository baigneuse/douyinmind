"""Import a local media file; all cloud access is explicitly opt-in."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="本地音视频导入。默认离线字幕；不会自动上传或下载 ASR 模型。",
        epilog="云端凭证仅从显式允许该服务后的进程环境读取，不自动加载 backend/.env。")
    parser.add_argument("--input", type=Path, required=True, help="有权处理的 MP4/MP3/M4A/WAV（最大2 GiB、三小时）")
    parser.add_argument("--subtitles", type=Path, help="对应 SRT/VTT；离线模式必填")
    parser.add_argument("--mode", choices=("subtitles", "qwen"), default="subtitles")
    parser.add_argument("--summary", choices=("none", "deepseek"), default="none")
    parser.add_argument("--output-dir", type=Path, help="输出根目录；默认 backend/app/storage/local_imports")
    parser.add_argument("--title", default="")
    parser.add_argument("--allow-cloud-asr", action="store_true",
                        help="明确同意将音频发送到配置的 Qwen 服务并承担可能费用")
    parser.add_argument("--allow-cloud-summary", action="store_true",
                        help="明确同意将逐字稿发送到配置的总结服务并承担可能费用")
    parser.add_argument("--asr-model", default="qwen3.8-omni-flash")
    parser.add_argument("--asr-base-url", default="https://dashscope.aliyuncs.com/compatible-mode/v1")
    parser.add_argument("--summary-model", default="deepseek-chat")
    parser.add_argument("--summary-base-url", default="https://api.deepseek.com")
    parser.add_argument("--segment-seconds", type=int, default=60)
    parser.add_argument("--concurrency", type=int, default=3)
    args = parser.parse_args(argv)
    # Set this before any app import. It affects only this CLI process.
    os.environ["DOUYINMIND_NO_DOTENV"] = "1"
    if not args.allow_cloud_asr:
        os.environ.pop("QWEN_OMNI_API_KEY", None)
        os.environ.pop("DASHSCOPE_API_KEY", None)
    if not args.allow_cloud_summary:
        os.environ.pop("DEEPSEEK_API_KEY", None)
    os.environ.pop("OPENAI_API_KEY", None)
    os.environ["ANONYMIZED_TELEMETRY"] = "False"
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.services.local_media_import import DEFAULT_OUTPUT, ImportOptions, LocalImportError, run_import
    options = ImportOptions(
        input=args.input, subtitles=args.subtitles, output_dir=args.output_dir or DEFAULT_OUTPUT,
        mode=args.mode, summary=args.summary, title=args.title,
        allow_cloud_asr=args.allow_cloud_asr, allow_cloud_summary=args.allow_cloud_summary,
        asr_model=args.asr_model, asr_base_url=args.asr_base_url,
        summary_model=args.summary_model, summary_base_url=args.summary_base_url,
        segment_seconds=args.segment_seconds, concurrency=args.concurrency)
    try:
        result = run_import(options, progress=lambda message: print(message, file=sys.stderr, flush=True))
    except LocalImportError as error:
        print(json.dumps({"status": "BLOCKED" if error.exit_code == 3 else "FAILED",
                          "error_code": error.code, "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        return error.exit_code
    except KeyboardInterrupt:
        print("已取消；原文件未修改，已有逐字稿和 checkpoint 保留。", file=sys.stderr)
        return 130
    except Exception:
        print("导入失败：请检查输入与输出权限；不会删除原文件。", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
