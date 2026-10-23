"""Anonymous, bounded network media downloads in an isolated worker.

Source collection is deliberately offline. The worker owns network access and
never imports application settings, browser profiles or provider credentials.
"""
from __future__ import annotations

import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import sys
import sysconfig
import threading
import time
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MAX_BYTES = 500 * 1024 * 1024
MAX_DURATION_MS = 2 * 60 * 60 * 1000
MAX_SECONDS = 15 * 60
MAX_SOURCES = 20
_MEDIA_EXTENSIONS = {'.mp4', '.webm', '.mov', '.m4v'}
_TRACKING = {'si', 'spm_id_from', 'vd_source', 'share_source', 'share_medium',
             'share_plat', 'share_session_id', 'share_tag', 'timestamp', 'feature',
             'from', 'is_copy_url', 'is_from_webapp', 'sender_device', 'web_id'}
_SENSITIVE = re.compile(r'(?:token|cookie|auth|signature|credential|password|secret|session|^sig$|(?:^|[_-])(?:api[_-]?)?key$|^policy$|^x-amz-|^x-goog-)', re.I)
_URLS = re.compile(r'https?://[^\s<>"\u3000]+', re.I)
_CODES = {
    'invalid_source': '链接格式无效，请粘贴公开的视频链接。',
    'unsafe_url': '此链接包含登录信息、敏感参数或非公网地址，不能导入。',
    'too_many_sources': '每次最多添加 20 个链接。',
    'unsupported_source': '此链接可以收藏，但暂不支持下载这种视频。',
    'authentication_required': '平台要求登录或验证；来源已保留，请在原站查看。',
    'access_restricted': '平台当前限制此网络的访问；来源已保留，请稍后重试或在原站查看。',
    'rate_limited': '平台请求过于频繁；来源已保留，请稍后重试。',
    'extractor_error': '当前平台页面无法解析；来源已保留，请在原站查看或稍后重试。',
    'network_error': '无法连接视频平台，请稍后重试。',
    'download_failed': '未能取得完整视频；来源已保留。',
    'media_too_large': '视频超过 500 MiB 下载上限。',
    'duration_exceeded': '视频超过 2 小时时长上限。',
    'cancelled': '下载已取消。',
    'timeout': '下载超过 15 分钟时限。',
    'tool_unavailable': '本机下载或媒体校验工具尚未就绪。',
    'invalid_media': '下载结果不是可验证的完整视频。',
    'target_not_empty': '下载目录已包含文件，请新建一次下载任务。',
}


class NetworkDownloadError(Exception):
    def __init__(self, code: str, message: str | None = None):
        self.code = code if code in _CODES else 'download_failed'
        # Error messages never interpolate extractor errors, URLs or credentials.
        self.message = _CODES[self.code]
        super().__init__(self.message)


def _public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value.split('%', 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return _public_ip(str(ip.ipv4_mapped))
    if isinstance(ip, ipaddress.IPv6Address) and any(ip in network for network in (
            ipaddress.ip_network('64:ff9b::/96'), ipaddress.ip_network('64:ff9b:1::/48'),
            ipaddress.ip_network('2002::/16'), ipaddress.ip_network('2001::/32'))):
        return False  # Translation/tunnelling can conceal a private IPv4 target.
    return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def _validate_url(url: str, *, source: bool = False) -> str:
    if not isinstance(url, str) or len(url) > 4096 or any(ord(c) < 33 for c in url) or '\\' in url:
        raise NetworkDownloadError('invalid_source')
    try:
        parts = urlsplit(url)
        host = (parts.hostname or '').rstrip('.').encode('idna').decode('ascii').lower()
        port = parts.port
    except (ValueError, UnicodeError):
        raise NetworkDownloadError('invalid_source') from None
    if parts.scheme.lower() not in {'http', 'https'} or not host:
        raise NetworkDownloadError('invalid_source')
    if parts.username is not None or parts.password is not None or port not in {None, 80, 443}:
        raise NetworkDownloadError('unsafe_url')
    if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal', '.lan')) or '%' in host:
        raise NetworkDownloadError('unsafe_url')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if '.' not in host or not re.fullmatch(r'[a-z0-9.-]+', host) or re.fullmatch(r'[0-9.]+', host):
            raise NetworkDownloadError('unsafe_url')
    else:
        if not _public_ip(host):
            raise NetworkDownloadError('unsafe_url')
    if source:
        for name, _ in parse_qsl(parts.query, keep_blank_values=True) + parse_qsl(parts.fragment, keep_blank_values=True):
            if _SENSITIVE.search(name) and name.lower() not in _TRACKING:
                raise NetworkDownloadError('unsafe_url')
    return host


def _source(url: str) -> dict:
    host = _validate_url(url, source=True)
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in _TRACKING and not k.lower().startswith('utm_')]
    platform = 'unknown'
    if host in {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be', 'www.youtu.be'}:
        platform = 'youtube'
        video = dict(query).get('v') if parts.path == '/watch' else None
        if host.endswith('youtu.be'):
            video = parts.path.strip('/').split('/')[0]
        elif parts.path.startswith(('/shorts/', '/embed/')):
            video = parts.path.split('/')[2]
        if video and re.fullmatch(r'[A-Za-z0-9_-]{11}', video):
            canonical = 'https://www.youtube.com/watch?' + urlencode({'v': video})
            return dict(url=url, canonical_url=canonical, platform=platform, title='YouTube 视频', supported=True)
        return dict(url=url, canonical_url=urlunsplit(('https', host, parts.path or '/', urlencode(query), '')),
                    platform=platform, title='YouTube 来源', supported=False)
    if host in {'douyin.com', 'www.douyin.com', 'v.douyin.com'}:
        platform = 'douyin'
        supported = bool(re.fullmatch(r'/video/\d+/?', parts.path) or
                         (host == 'v.douyin.com' and re.fullmatch(r'/[A-Za-z0-9_-]+/?', parts.path)))
        query = []
    elif host in {'bilibili.com', 'www.bilibili.com', 'm.bilibili.com', 'b23.tv'}:
        platform = 'bilibili'
        supported = bool(re.fullmatch(r'/video/(?:BV[A-Za-z0-9]+|av\d+)/?', parts.path) or
                         (host == 'b23.tv' and re.fullmatch(r'/[A-Za-z0-9]+/?', parts.path)))
        query = [(k, v) for k, v in query if k == 'p' and v.isdigit() and 0 < int(v) <= 10000]
    elif Path(parts.path).suffix.lower() in _MEDIA_EXTENSIONS:
        platform, supported = 'direct', True
    else:
        supported = False
    netloc = f'[{host}]' if ':' in host else host
    if parts.port and parts.port != (443 if parts.scheme == 'https' else 80):
        netloc += f':{parts.port}'
    canonical = urlunsplit((parts.scheme.lower(), netloc, parts.path or '/', urlencode(query), ''))
    title = {'douyin': '抖音视频', 'bilibili': '哔哩哔哩视频', 'direct': '网络视频文件'}.get(platform, host)
    return dict(url=url, canonical_url=canonical, platform=platform, title=title, supported=supported)


def parse_sources(text: str) -> list[dict]:
    if not isinstance(text, str) or len(text) > 100_000:
        raise NetworkDownloadError('invalid_source')
    results, seen = [], set()
    for match in _URLS.finditer(text):
        url = match.group().rstrip('.,;:!?)]}，。；：！？”’')
        item = _source(url)
        if item['canonical_url'] in seen:
            continue
        if len(results) >= MAX_SOURCES:
            raise NetworkDownloadError('too_many_sources')
        seen.add(item['canonical_url'])
        results.append(item)
    if not results:
        raise NetworkDownloadError('invalid_source')
    return results


def _media_tool(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return str(Path(found).resolve())
    # Phase 0's existing local tool directory; no user profiles/config are read.
    repo = Path(__file__).resolve().parents[3]
    candidate = repo.parent / '.phase0-tools' / 'bin' / (name + ('.exe' if os.name == 'nt' else ''))
    return str(candidate) if candidate.is_file() else None


def downloader_capabilities() -> dict:
    yt = importlib.util.find_spec('yt_dlp') is not None
    probe, merge = bool(_media_tool('ffprobe')), bool(_media_tool('ffmpeg'))
    return {'downloader_ready': yt and merge, 'yt_dlp_available': yt,
            'ffprobe_available': probe, 'ffmpeg_available': merge,
            'youtube_js_available': bool(_deno_tool()) and importlib.util.find_spec('yt_dlp_ejs') is not None}


def _deno_tool() -> str | None:
    # Use an explicitly installed native binary; never invoke a shell launcher.
    spec = importlib.util.find_spec('deno')
    if spec and spec.submodule_search_locations:
        for directory in spec.submodule_search_locations:
            candidate = Path(directory) / ('deno.exe' if os.name == 'nt' else 'deno')
            if candidate.is_file():
                return str(candidate.resolve())
        candidate = Path(sysconfig.get_path('scripts')) / ('deno.exe' if os.name == 'nt' else 'deno')
        if candidate.is_file():
            return str(candidate.resolve())
    return None


def _worker_environment(target: Path) -> dict[str, str]:
    env = {key: os.environ[key] for key in ('SystemRoot', 'WINDIR', 'SYSTEMDRIVE') if key in os.environ}
    env.update(TEMP=str(target), TMP=str(target), HOME=str(target), USERPROFILE=str(target),
               YTDLP_NO_PLUGINS='1', PYTHONUTF8='1', PYTHONIOENCODING='utf-8',
               DENO_DIR=str(target / '.deno'), DENO_NO_UPDATE_CHECK='1', DENO_NO_PACKAGE_JSON='1')
    return env


class _ProcessTree:
    """Kill-on-close Windows job; POSIX worker owns a new process group."""
    def __init__(self, process: subprocess.Popen):
        self.process, self.handle = process, None
        if os.name != 'nt':
            return
        import ctypes
        from ctypes import wintypes
        class Basic(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong), ('PerJobUserTimeLimit', ctypes.c_longlong),
                        ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                        ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                        ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD), ('SchedulingClass', wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ('ReadOperationCount', 'WriteOperationCount',
                        'OtherOperationCount', 'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]
        class Extended(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', Basic), ('IoInfo', IO), ('ProcessMemoryLimit', ctypes.c_size_t),
                        ('JobMemoryLimit', ctypes.c_size_t), ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateJobObjectW(None, None)
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not handle or not kernel.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)) or not kernel.AssignProcessToJobObject(handle, int(process._handle)):
            if handle:
                kernel.CloseHandle(handle)
            process.kill()
            raise NetworkDownloadError('tool_unavailable')
        self.kernel, self.handle = kernel, handle

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
        elif os.name != 'nt':
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif self.process.poll() is None:
            self.process.kill()


def download_media(url: str, target_dir: Path, *, progress: Callable[[dict], None], cancelled: Callable[[], bool]) -> dict:
    source = _source(url)
    if not source['supported']:
        raise NetworkDownloadError('unsupported_source')
    if cancelled():
        raise NetworkDownloadError('cancelled')
    target = Path(target_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    if any(target.iterdir()):
        raise NetworkDownloadError('target_not_empty')
    probe, merge = _media_tool('ffprobe'), _media_tool('ffmpeg')
    if not merge or importlib.util.find_spec('yt_dlp') is None:
        raise NetworkDownloadError('tool_unavailable')
    worker = Path(__file__).with_name('network_download_worker.py')
    process = subprocess.Popen([sys.executable, '-I', '-B', str(worker)], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=target,
                               env=_worker_environment(target), creationflags=(subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0),
                               start_new_session=os.name != 'nt')
    tree = None
    events: queue.Queue = queue.Queue(maxsize=256)
    def read_events():
        try:
            while line := process.stdout.readline(65537):
                if len(line) > 65536:
                    break
                try:
                    value = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(value, dict):
                    events.put(value, timeout=1)
        except (OSError, queue.Full):
            pass
        finally:
            try:
                events.put({'event': 'eof'}, timeout=1)
            except queue.Full:
                pass
    try:
        tree = _ProcessTree(process)
        threading.Thread(target=read_events, daemon=True).start()
        process.stdin.write(json.dumps({'url': source['canonical_url'], 'target': str(target),
                                        'ffprobe': probe, 'ffmpeg': merge, 'deno': _deno_tool()}).encode('utf-8') + b'\n')
        process.stdin.close()
        started = time.monotonic()
        while True:
            if cancelled():
                raise NetworkDownloadError('cancelled')
            if time.monotonic() - started > MAX_SECONDS:
                raise NetworkDownloadError('timeout')
            try:
                event = events.get(timeout=.1)
            except queue.Empty:
                # The reader drains buffered stdout before its EOF marker. A
                # process exit alone must not discard a final result still queued.
                continue
            kind = event.get('event')
            if kind == 'progress':
                stage = event.get('stage')
                if stage not in {'resolving', 'downloading', 'verifying', 'merging'}:
                    continue
                data = {'stage': stage}
                for key in ('downloaded_bytes', 'total_bytes'):
                    value = event.get(key)
                    if type(value) is int and 0 <= value <= MAX_BYTES:
                        data[key] = value
                progress(data)
            elif kind == 'error':
                raise NetworkDownloadError(event.get('code', 'download_failed'))
            elif kind == 'result':
                if cancelled():
                    raise NetworkDownloadError('cancelled')
                name = event.get('filename')
                if name not in {'media.mp4', 'media.webm', 'media.mov', 'media.m4v'}:
                    raise NetworkDownloadError('invalid_media')
                path = target / name
                if not path.is_file() or path.is_symlink() or path.resolve().parent != target:
                    raise NetworkDownloadError('invalid_media')
                size = path.stat().st_size
                duration = event.get('duration_ms')
                if not 0 < size <= MAX_BYTES or type(duration) is not int or not 0 < duration <= MAX_DURATION_MS:
                    raise NetworkDownloadError('invalid_media')
                with path.open('rb') as media:
                    digest = hashlib.file_digest(media, 'sha256').hexdigest()
                return {'path': path, 'title': _safe_text(event.get('title'), source['title']),
                        'author': _safe_text(event.get('author'), ''), 'duration_ms': duration,
                        'size_bytes': size, 'sha256': digest,
                        'content_type': 'video/webm' if path.suffix == '.webm' else 'video/mp4'}
            elif kind == 'eof':
                raise NetworkDownloadError('download_failed')
    finally:
        if tree:
            tree.close()
        elif process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        if process.stdout:
            process.stdout.close()
        # Only worker-created cache directories inside this new attempt are
        # eligible. This also cleans caches after cancelling a solver process.
        for candidate in target.iterdir():
            if (candidate.name == '.deno' or candidate.name.startswith('solver-')) and candidate.is_dir() and not candidate.is_symlink():
                resolved = candidate.resolve()
                if resolved.parent == target:
                    shutil.rmtree(resolved, ignore_errors=True)


def _safe_text(value, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    return ''.join(c for c in value if ord(c) >= 32 and ord(c) != 127)[:300].strip() or fallback
