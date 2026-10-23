"""Private worker entry point. Never imports application configuration.

Only Python HTTP(S) transports may access the network. Extractor subprocesses,
plugins, credential sources, proxies and remote JS components are disabled.
Local ffmpeg/probe commands run with the file protocol only.
"""
from __future__ import annotations

import contextlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

if __package__:
    from .network_download import (MAX_BYTES, MAX_DURATION_MS, NetworkDownloadError,
                                   _public_ip, _safe_text, _source, _validate_url)
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from network_download import (MAX_BYTES, MAX_DURATION_MS, NetworkDownloadError,
                                  _public_ip, _safe_text, _source, _validate_url)


def _emit(event: str, **data):
    print(json.dumps({'event': event, **data}, ensure_ascii=True), flush=True)


def _checked_addresses(host, port, family, socktype, proto):
    if port not in (80, 443) or family not in (socket.AF_INET, socket.AF_INET6):
        raise NetworkDownloadError('unsafe_url')
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        addresses = socket.getaddrinfo(host, port, family, socktype, proto)
        if not addresses or any(not _public_ip(item[4][0]) for item in addresses):
            raise NetworkDownloadError('unsafe_url')
        return addresses[0][4]
    if not _public_ip(str(literal)):
        raise NetworkDownloadError('unsafe_url')
    return (str(literal), port, 0, 0) if family == socket.AF_INET6 else (str(literal), port)


_DENO_OPTIONS = ['--ext=js', '--no-code-cache', '--no-prompt', '--no-remote',
                 '--no-lock', '--node-modules-dir=none', '--no-config', '--no-npm', '--cached-only']


def _allowed_deno(command, deno: str | None) -> bool:
    if not deno or not isinstance(command, (list, tuple)) or not command:
        return False
    try:
        if Path(command[0]).resolve() != Path(deno).resolve():
            return False
    except (TypeError, ValueError):
        return False
    return list(command[1:]) in [['--version'], ['run', *_DENO_OPTIONS, '-']]


@contextlib.contextmanager
def public_connections_only(deno: str | None = None):
    """Validate/pin the actual address passed to connect, not a prior DNS check."""
    original, original_ex = socket.socket.connect, socket.socket.connect_ex
    original_sendto, popen_class, original_init = socket.socket.sendto, subprocess.Popen, subprocess.Popen.__init__
    def connect(sock, address):
        if not isinstance(address, tuple) or len(address) < 2:
            raise NetworkDownloadError('unsafe_url')
        pinned = _checked_addresses(address[0], address[1], sock.family, sock.type, sock.proto)
        result = original(sock, pinned)
        if not _public_ip(sock.getpeername()[0]):
            sock.close()
            raise NetworkDownloadError('unsafe_url')
        return result
    def connect_ex(sock, address):
        connect(sock, address)
        return 0
    def denied(*args, **kwargs):
        raise NetworkDownloadError('unsupported_source')
    def guarded_init(instance, args, *positional, **kwargs):
        if positional or not _allowed_deno(args, deno) or kwargs.get('shell') or kwargs.get('executable'):
            raise NetworkDownloadError('unsupported_source')
        # Ignore any runtime-supplied environment. Parent provided only a clean
        # environment; no --allow-net/read/env/run or remote imports are allowed.
        kwargs['env'] = os.environ.copy()
        if os.name == 'nt':
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
        original_init(instance, args, **kwargs)
    def local_popen(args, **kwargs):
        instance = popen_class.__new__(popen_class)
        original_init(instance, args, **kwargs)
        return instance
    socket.socket.connect, socket.socket.connect_ex = connect, connect_ex
    socket.socket.sendto, subprocess.Popen.__init__ = denied, guarded_init
    try:
        yield local_popen
    finally:
        socket.socket.connect, socket.socket.connect_ex = original, original_ex
        socket.socket.sendto, subprocess.Popen.__init__ = original_sendto, original_init


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 5
    max_repeats = 2
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _guard_opener(opener):
    original_open = opener.open
    def safe_open(request, *args, **kwargs):
        _validate_url(request.full_url if isinstance(request, urllib.request.Request) else request)
        return original_open(request, *args, **kwargs)
    opener.open = safe_open
    return opener


def _opener():
    # Empty proxies prohibit inheritance from environment or Windows registry.
    return _guard_opener(urllib.request.build_opener(urllib.request.ProxyHandler({}), SafeRedirect()))


def _headers(values=None):
    allowed = {'user-agent', 'referer', 'accept', 'accept-language', 'origin'}
    result = {'User-Agent': 'Mozilla/5.0'}
    for key, value in (values or {}).items():
        if key.lower() in allowed and isinstance(value, str) and len(value) < 2048 and not any(ord(c) < 32 for c in value):
            if key.lower() in {'referer', 'origin'}:
                _validate_url(value)
            result[key] = value
    return result


def _install_bounded_deno():
    """Avoid Windows large-pipe writes and keep solver I/O/time bounded.

The anonymous file is an inherited stdin descriptor, not a filesystem grant to
Deno. Windows deletes it when the worker/child handles close; POSIX unlinks it
immediately. No challenge code or signed parameters become durable artifacts.
"""
    from yt_dlp.extractor.youtube.jsc._builtin.deno import DenoJCP
    from yt_dlp.extractor.youtube.jsc.provider import JsChallengeProviderError
    def run_deno(instance, stdin, options):
        command = [instance.runtime_info.path, 'run', *options, '-']
        if not _allowed_deno(command, instance.runtime_info.path):
            raise JsChallengeProviderError('Unsupported local solver invocation')
        payload = stdin.encode('utf-8')
        if len(payload) > 32 * 1024 * 1024:
            raise JsChallengeProviderError('Local solver input exceeds limit')
        with tempfile.TemporaryDirectory(prefix='solver-') as cache_dir, tempfile.TemporaryFile() as input_file:
            input_file.write(payload)
            input_file.seek(0)
            previous_cache = os.environ.get('DENO_DIR')
            os.environ['DENO_DIR'] = cache_dir
            try:
                process = subprocess.Popen(command, stdin=input_file, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            finally:
                if previous_cache is None:
                    os.environ.pop('DENO_DIR', None)
                else:
                    os.environ['DENO_DIR'] = previous_cache
            output, errors, overflow = [], [], threading.Event()
            def drain(pipe, parts, maximum):
                size = 0
                while chunk := pipe.read(16384):
                    size += len(chunk)
                    if size > maximum:
                        overflow.set()
                        process.kill()
                        break
                    parts.append(chunk)
            readers = [threading.Thread(target=drain, args=(process.stdout, output, 4 * 1024 * 1024), daemon=True),
                       threading.Thread(target=drain, args=(process.stderr, errors, 65536), daemon=True)]
            for thread in readers:
                thread.start()
            try:
                process.wait(timeout=90)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise JsChallengeProviderError('Local solver timed out') from None
            finally:
                for thread in readers:
                    thread.join(timeout=2)
                process.stdout.close()
                process.stderr.close()
            if process.returncode or errors or overflow.is_set() or any(thread.is_alive() for thread in readers):
                raise JsChallengeProviderError('Local solver failed')
            return b''.join(output).decode('utf-8')
    DenoJCP._run_deno = run_deno


def _extract(url: str, deno: str | None = None) -> dict:
    source = _source(url)
    if source['platform'] == 'direct':
        return {'title': source['title'], 'formats': [{'url': url, 'ext': Path(url.split('?', 1)[0]).suffix[1:],
                    'vcodec': 'unknown', 'acodec': 'unknown', 'protocol': 'https'}]}
    if source['platform'] in {'douyin', 'bilibili'} and urllib.parse.urlsplit(url).hostname in {'v.douyin.com', 'b23.tv'}:
        with _opener().open(urllib.request.Request(url, headers=_headers()), timeout=30) as response:
            resolved = response.url
        destination = _source(resolved)
        if destination['platform'] != source['platform'] or not destination['supported']:
            raise NetworkDownloadError('unsupported_source')
        url = destination['canonical_url']
    import yt_dlp
    if deno:
        _install_bounded_deno()
    from yt_dlp.networking._urllib import UrllibRH
    from yt_dlp.globals import plugin_dirs
    plugin_dirs.value = []
    class PublicUrllibRH(UrllibRH):
        _SUPPORTED_URL_SCHEMES = ('http', 'https')
        def _create_instance(self, *args, **kwargs):
            return _guard_opener(super()._create_instance(*args, **kwargs))
        def _send(self, request):
            _validate_url(request.url)
            return super()._send(request)
    class PublicYoutubeDL(yt_dlp.YoutubeDL):
        def build_request_director(self, handlers, preferences=None):
            return super().build_request_director([PublicUrllibRH], [])
    class SilentLogger:
        def debug(self, message): pass
        def warning(self, message): pass
        def error(self, message): pass
    opts = {'quiet': True, 'no_warnings': True, 'logger': SilentLogger(), 'noplaylist': True,
            'extract_flat': False, 'skip_download': True, 'cachedir': False, 'proxy': '',
            'socket_timeout': 30, 'retries': 0, 'extractor_retries': 0,
            'cookiefile': None, 'cookiesfrombrowser': None, 'usenetrc': False,
            'js_runtimes': {'deno': {'path': deno}} if deno else {}, 'remote_components': [], 'enable_file_urls': False,
            'external_downloader': {}, 'postprocessors': [], 'color': {'stdout': 'never', 'stderr': 'never'}}
    key = {'youtube': 'Youtube', 'bilibili': 'BiliBili', 'douyin': 'Douyin'}.get(source['platform'])
    if not key:
        raise NetworkDownloadError('unsupported_source')
    with PublicYoutubeDL(opts) as ydl:
        # We select and download formats ourselves. Avoid yt-dlp's processing
        # stage, which can probe external merger tools even with download=False.
        result = ydl.extract_info(url, download=False, process=False, ie_key=key)
    if not isinstance(result, dict) or result.get('_type', 'video') != 'video' or result.get('entries') is not None:
        raise NetworkDownloadError('unsupported_source')
    if result.get('is_live') or result.get('live_status') in {'is_live', 'is_upcoming'}:
        raise NetworkDownloadError('unsupported_source')
    if isinstance(result.get('duration'), (int, float)) and result['duration'] * 1000 > MAX_DURATION_MS:
        raise NetworkDownloadError('duration_exceeded')
    return result


def _choose_formats(info: dict) -> list[dict]:
    candidates = []
    for f in info.get('formats', []):
        if not isinstance(f, dict) or not isinstance(f.get('url'), str):
            continue
        if f.get('protocol', urllib.parse.urlsplit(f['url']).scheme) not in {'http', 'https'}:
            continue  # No HLS/DASH/ffmpeg network delegation.
        _validate_url(f['url'])
        if f.get('has_drm') or (f.get('height') or 0) > 720:
            continue
        candidates.append(f)
    def rank(f):
        return ((f.get('ext') == 'mp4'), min(f.get('height') or 0, 720), f.get('tbr') or 0)
    progressive = [f for f in candidates if f.get('vcodec') != 'none' and f.get('acodec') != 'none']
    if progressive:
        return [max(progressive, key=rank)]
    videos = [f for f in candidates if f.get('vcodec') != 'none' and f.get('acodec') == 'none'
              and f.get('ext') == 'mp4' and str(f.get('vcodec', '')).startswith(('avc', 'h264'))]
    audios = [f for f in candidates if f.get('vcodec') == 'none' and f.get('acodec') != 'none'
              and f.get('ext') in {'m4a', 'mp4'} and str(f.get('acodec', '')).startswith(('mp4a', 'aac'))]
    if videos and audios:
        return [max(videos, key=rank), max(audios, key=lambda f: f.get('abr') or f.get('tbr') or f.get('filesize') or 0)]
    raise NetworkDownloadError('unsupported_source')


def _download(url: str, path: Path, headers: dict, consumed: int) -> int:
    _validate_url(url)
    last_emit = 0.0
    with _opener().open(urllib.request.Request(url, headers=_headers(headers)), timeout=30) as response:
        _validate_url(response.url)
        if response.status != 200:
            raise NetworkDownloadError('download_failed')
        length = response.headers.get('Content-Length')
        expected = int(length) if length and length.isdigit() else None
        if expected is not None and consumed + expected > MAX_BYTES:
            raise NetworkDownloadError('media_too_large')
        media_type = response.headers.get('Content-Type', '').split(';')[0].lower()
        if media_type.startswith('text/') or media_type in {'application/json', 'application/xml'}:
            raise NetworkDownloadError('invalid_media')
        written = 0
        with path.open('xb') as output:
            while chunk := response.read(256 * 1024):
                written += len(chunk)
                if consumed + written > MAX_BYTES:
                    raise NetworkDownloadError('media_too_large')
                output.write(chunk)
                if time.monotonic() - last_emit >= .3:
                    _emit('progress', stage='downloading', downloaded_bytes=consumed + written,
                          total_bytes=consumed + expected if expected is not None else None)
                    last_emit = time.monotonic()
            output.flush()
            os.fsync(output.fileno())
        if written == 0 or (expected is not None and expected != written):
            raise NetworkDownloadError('download_failed')
    return consumed + written


def _local_process(popen, command: list[str], *, timeout: int = 90, metadata: bool = False) -> bytes:
    # Call the saved constructor only for fixed local ffmpeg/ffprobe commands.
    process = popen(command, stdout=subprocess.DEVNULL if metadata else subprocess.PIPE,
                    stderr=subprocess.PIPE if metadata else subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL, creationflags=(subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0))
    chunks, overflow = [], threading.Event()
    pipe = process.stderr if metadata else process.stdout
    def read_bounded():
        size = 0
        while chunk := pipe.read(16384):
            size += len(chunk)
            if size > 1024 * 1024:
                overflow.set()
                process.kill()
                break
            chunks.append(chunk)
    reader = threading.Thread(target=read_bounded, daemon=True)
    reader.start()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise NetworkDownloadError('invalid_media') from None
    finally:
        reader.join(timeout=2)
        pipe.close()
    if process.returncode != 0 or overflow.is_set() or reader.is_alive():
        raise NetworkDownloadError('invalid_media')
    return b''.join(chunks)


def _probe(path: Path, ffprobe: str | None, popen, *, ffmpeg: str | None = None) -> tuple[int, str]:
    if ffprobe:
        result = _local_process(popen, [ffprobe, '-v', 'error', '-protocol_whitelist', 'file,pipe',
                              '-show_entries', 'format=duration,format_name:stream=codec_type,codec_name',
                              '-of', 'json', str(path)])
        try:
            data = json.loads(result)
            streams = data['streams']
            duration = int(float(data['format']['duration']) * 1000)
            name = data['format']['format_name']
        except (KeyError, ValueError, TypeError, OverflowError):
            raise NetworkDownloadError('invalid_media') from None
    elif ffmpeg:
        # Existing installations bundle ffmpeg alone. Inspect only a local file;
        # stderr stays in memory and is never returned as user-facing diagnostics.
        result = _local_process(popen, [ffmpeg, '-nostdin', '-hide_banner', '-v', 'info',
                                '-protocol_whitelist', 'file,pipe', '-i', str(path),
                                '-map', '0:v:0', '-t', '0', '-f', 'null', '-'], metadata=True)
        text = result.decode('utf-8', errors='replace')
        match = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', text)
        container = re.search(r'Input #0, ([a-zA-Z0-9_,]+), from ', text)
        if not match or not container:
            raise NetworkDownloadError('invalid_media')
        duration = int((int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3])) * 1000)
        name = container[1]
        streams = [{'codec_type': 'video'}] if re.search(r'Stream #0:.*Video:', text) else []
    else:
        raise NetworkDownloadError('tool_unavailable')
    if not any(s.get('codec_type') == 'video' for s in streams) or duration <= 0:
        raise NetworkDownloadError('invalid_media')
    if duration > MAX_DURATION_MS:
        raise NetworkDownloadError('duration_exceeded')
    if not any(token in name.split(',') for token in ('mp4', 'mov', 'webm', 'matroska')):
        raise NetworkDownloadError('unsupported_source')
    return duration, '.webm' if 'webm' in name or 'matroska' in name else '.mp4'


def run(request: dict):
    url, target = request['url'], Path(request['target']).resolve()
    if not target.is_dir() or any(target.iterdir()):
        raise NetworkDownloadError('target_not_empty')
    _validate_url(url, source=True)
    _emit('progress', stage='resolving')
    with public_connections_only(request.get('deno')) as popen:
        info = _extract(url, request.get('deno'))
        formats = _choose_formats(info)
        consumed, inputs = 0, []
        for index, f in enumerate(formats):
            path = target / f'track-{index}.part'
            headers = dict(info.get('http_headers') or {})
            headers.update(f.get('http_headers') or {})
            consumed = _download(f['url'], path, headers, consumed)
            inputs.append(path)
        candidate = inputs[0]
        if len(inputs) == 2:
            _emit('progress', stage='merging')
            candidate = target / 'merged.mp4'
            _local_process(popen, [request['ffmpeg'], '-nostdin', '-v', 'error', '-n',
                           '-protocol_whitelist', 'file,pipe', '-i', str(inputs[0]),
                           '-protocol_whitelist', 'file,pipe', '-i', str(inputs[1]),
                           '-map', '0:v:0', '-map', '1:a:0', '-c', 'copy', '-movflags', '+faststart', str(candidate)])
        _emit('progress', stage='verifying')
        if candidate.stat().st_size > MAX_BYTES:
            raise NetworkDownloadError('media_too_large')
        duration, extension = _probe(candidate, request['ffprobe'], popen, ffmpeg=request['ffmpeg'])
        # Decode the complete local file. Protocol whitelist prevents nested URL
        # references in malicious containers from causing ffmpeg network access.
        _local_process(popen, [request['ffmpeg'], '-nostdin', '-v', 'error', '-xerror',
                       '-protocol_whitelist', 'file,pipe', '-i', str(candidate),
                       '-map', '0:v:0', '-map', '0:a:0?', '-f', 'null', '-'], timeout=180)
        final = target / ('media' + extension)
        candidate.rename(final)
        for path in inputs:
            if path.exists():
                path.unlink()
        _emit('result', filename=final.name, duration_ms=duration,
              title=_safe_text(info.get('title'), '网络视频'),
              author=_safe_text(info.get('uploader') or info.get('creator'), ''))


def _classify(error: Exception) -> str:
    if isinstance(error, NetworkDownloadError):
        return error.code
    message = str(error).lower()  # Examined only in memory; never logged/persisted.
    if re.search(r'(?<!\d)429(?!\d)', message) or 'too many requests' in message:
        return 'rate_limited'
    if re.search(r'(?<!\d)(403|412|451)(?!\d)', message):
        return 'access_restricted'
    if any(s in message for s in ('login', 'sign in', 'cookies', 'captcha', 'verification', 'not a bot', '401')):
        return 'authentication_required'
    if any(s in message for s in ('timed out', 'timeout', 'connection', 'resolve', 'network', 'ssl')):
        return 'network_error'
    if any(s in message for s in ('unsupported', 'no video formats', 'requested format', 'javascript', 'challenge', 'ejs')):
        return 'unsupported_source'
    if 'unable to extract' in message:
        return 'extractor_error'
    return 'download_failed'


if __name__ == '__main__':
    try:
        # Parent establishes the process-tree kill boundary before sending input.
        raw = sys.stdin.buffer.readline(16385)
        if len(raw) > 16384:
            raise NetworkDownloadError('invalid_source')
        run(json.loads(raw))
    except Exception as exc:
        _emit('error', code=_classify(exc))
        sys.exit(1)
