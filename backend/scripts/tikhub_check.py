"""Free TikHub checks and a local results report. Never sends a paid request.

    uv run python scripts/tikhub_check.py demo      # free fixed demo post; no token is sent
    uv run python scripts/tikhub_check.py account   # free; verifies the token in backend/.env
    uv run python scripts/tikhub_check.py report    # offline; summarises local requests and downloads

Paid lookups (post details, creator lists) are only available in the web page,
where each one is confirmed individually. Signed media addresses and the token
are never printed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


def demo() -> int:
    from app.services.tikhub_adapter import ENDPOINTS, TikHubClient, TikHubError, describe_aweme
    records = []
    try:
        aweme = TikHubClient("", on_request=records.append).demo_video()
        info = describe_aweme(aweme)
    except TikHubError as error:
        print(f"FAILED {error.code}: {error}")
        return 1
    video = aweme.get("video") or {}
    print("TikHub 免费 Demo（固定作品，未发送 Token）")
    print(f"  接口            {ENDPOINTS['demo_video'].path}")
    print(f"  HTTP / 计费估算  {records[0]['http_status']} / ${records[0]['cost_usd']:.3f}")
    print(f"  作品 ID          {info['item_id']}")
    print(f"  规范链接         {info['canonical_url']}")
    print(f"  标题             {info['title']}")
    print(f"  作者             {info['author']}")
    print(f"  时长             {info['duration_ms']} ms    发布时间 {info['published_at']}")
    print(f"  类型 / 可下载    {info['kind']} / {info['downloadable']}（原因：{info['reason']}）")
    print(f"  响应字段数       aweme_detail={len(aweme)}  video={len(video)}  bit_rate={len(video.get('bit_rate') or [])}")
    print(f"  地址过期时间     cdn_url_expired={info['media_expires_at']}")
    print(f"  选出的媒体地址   {len(info['candidates'])} 个（仅显示主机，不显示签名地址）")
    for index, candidate in enumerate(info["candidates"], 1):
        print(f"    {index}. {candidate['codec']}  短边 {candidate['short_side']}  {candidate['size']} bytes  "
              f"{urlsplit(candidate['url']).hostname}")
    return 0 if info["downloadable"] else 2


def account() -> int:
    from app.core.config import settings
    from app.services.tikhub_adapter import TikHubClient, TikHubError
    try:
        result = TikHubClient(settings.tikhub_api_key.get_secret_value(), base_url=settings.tikhub_base_url,
                              timeout=settings.tikhub_timeout_seconds).account()
    except TikHubError as error:
        print(f"FAILED {error.code}: {error}")
        return 1
    print("TikHub 账户检查（免费接口；不显示 Token、邮箱或密钥名称）")
    for key, value in result.items():
        print(f"  {key:18} {value}")
    return 0 if result["key_active"] and result["account_active"] and not result["missing_scopes"] else 2


def _records(folder: Path, pattern: str) -> list[dict]:
    values = []
    for path in folder.glob(pattern):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(value, dict):
            values.append(value)
    return values


def report() -> int:
    # Read-only: the running backend owns these files and its download states.
    root = Path(os.environ.get("DOUYINMIND_LOCAL_MEDIA_ROOT", str(BACKEND / "app/storage/local_media_workspace"))) / "network"
    sources = {source["id"]: source for source in _records(root / "sources", "*.json") if source.get("platform") == "douyin"}
    attempts = [attempt for attempt in _records(root / "attempts", "*/attempt.json") if attempt.get("source_id") in sources]
    ledger = (_records(root / "tikhub", "usage.json") or [{}])[0]
    total = ledger.get("total") or {}
    resolved = [source for source in sources.values() if source.get("provider") == "tikhub"]
    methods = {attempt.get("id"): attempt.get("method") or "anonymous" for attempt in attempts}
    restricted = [source for source in sources.values()
                  if source.get("status") == "needs_login" and methods.get(source.get("download_id")) == "tikhub"]
    print("## TikHub 本机结果汇总\n")
    print(f"- TikHub 成功解析并加入资料库的作品：{len(resolved)}")
    print(f"- 已下载到本机（通过完整解码与 SHA-256 校验）：{sum(bool(source.get('has_media')) for source in resolved)}")
    print(f"- TikHub 已应答但需要登录/权限：{len(restricted)}")
    print(f"- TikHub 请求：{total.get('requests', 0)} 次（成功 {total.get('succeeded', 0)}，失败 {total.get('failed', 0)}，"
          f"付费接口 {total.get('paid_attempts', 0)}，估算计费 {total.get('billable', 0)} 次）")
    print(f"- 按公开单价估算费用：${total.get('estimated_cost_usd', 0):.3f}（以 TikHub 后台账单为准）\n")
    print("| 作品 ID | 标题 | 状态 | 本地文件 | 失败原因 |\n| --- | --- | --- | --- | --- |")
    listed = {source["id"]: source for source in [*resolved, *restricted]}
    for source in sorted(listed.values(), key=lambda value: value.get("created_at", "")):
        error = (source.get("error") or {}).get("code") or ""
        size = f"{source['size_bytes']} bytes" if source.get("has_media") else "无"
        title = str(source.get("title"))[:40].replace("|", "/")
        print(f"| {source.get('platform_item_id')} | {title} | {source.get('status')} | {size} | {error} |")
    print("\n| 下载方式 | 尝试 | 成功 | 需登录/权限 | 不支持 | 失败 | 其他 |\n| --- | --- | --- | --- | --- | --- | --- |")
    for label, method in (("TikHub", "tikhub"), ("原匿名解析", "anonymous")):
        rows = [attempt for attempt in attempts if (attempt.get("method") or "anonymous") == method]
        count = lambda status: sum(attempt.get("status") == status for attempt in rows)
        known = sum(count(status) for status in ("ready", "needs_login", "unsupported", "failed"))
        print(f"| {label} | {len(rows)} | {count('ready')} | {count('needs_login')} | {count('unsupported')} | "
              f"{count('failed')} | {len(rows) - known} |")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="TikHub 免费检查与本机结果汇总；不会发送付费请求。")
    parser.add_argument("command", choices=("demo", "account", "report"))
    return {"demo": demo, "account": account, "report": report}[parser.parse_args(argv).command]()


if __name__ == "__main__":
    raise SystemExit(main())
