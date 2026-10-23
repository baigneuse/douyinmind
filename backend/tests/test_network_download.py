"""Offline source parsing, real transport boundaries and isolated process control."""
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
import urllib.request

import pytest

from app.services import network_download as service
from app.services import network_download_worker as worker


def test_share_text_is_offline_and_canonical_single_video(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: pytest.fail('collection must not resolve DNS'))
    # Synthetic identifiers retain platform syntax without retaining user history.
    sources = service.parse_sources('分享 01:23 https://v.douyin.com/TestDouyinA/ 复制此文案\n'
        'https://youtu.be/TestVideo01?si=tracking\n'
        'https://www.youtube.com/watch?v=TestVideo01&list=PL123&index=4\n'
        'B站 https://b23.tv/TestB01。 https://example.org/a.mp4?utm_source=share\n'
        'https://example.org/article')
    assert len(sources) == 5
    assert [item['platform'] for item in sources] == ['douyin', 'youtube', 'bilibili', 'direct', 'unknown']
    assert sources[1]['canonical_url'] == 'https://www.youtube.com/watch?v=TestVideo01'
    assert sources[3]['canonical_url'] == 'https://example.org/a.mp4'
    assert sources[-1]['supported'] is False


@pytest.mark.parametrize('url', [
    'http://127.0.0.1/a.mp4', 'http://[::1]/a.mp4', 'http://169.254.169.254/a.mp4',
    'http://10.1.2.3/a.mp4', 'http://localhost/a.mp4', 'http://host.local/a.mp4',
    'https://user:password@example.com/a.mp4', 'https://example.com:9000/a.mp4',
    'https://example.com/a.mp4?token=secret', 'https://example.com/a.mp4?X-Amz-Signature=secret',
    'http://[::ffff:127.0.0.1]/a.mp4', 'https://example.com/a.mp4?AUTHORIZATION=secret',
    'http://[64:ff9b::7f00:1]/a.mp4', 'http://[2002:7f00:1::]/a.mp4',
    'http://127.0.0.01/a.mp4',
    'https://example.com/a.mp4?api_key=secret', 'https://example.com/a.mp4#access_token=secret',
])
def test_sources_reject_private_destinations_and_credentials(url):
    with pytest.raises(service.NetworkDownloadError) as exc:
        service.parse_sources(url)
    assert exc.value.code == 'unsafe_url'
    assert 'secret' not in str(exc.value)


def test_collection_is_bounded_and_channels_are_bookmarks():
    with pytest.raises(service.NetworkDownloadError, match='20'):
        service.parse_sources('\n'.join(f'https://example.com/{i}.mp4' for i in range(21)))
    for url in ('https://youtube.com/playlist?list=PL123', 'https://www.youtube.com/@channel',
                'https://www.bilibili.com/', 'https://www.douyin.com/user/someone'):
        assert service.parse_sources(url)[0]['supported'] is False


def test_actual_connect_pins_dns_result_and_checks_peer(monkeypatch):
    connected, queries = [], []
    monkeypatch.setattr(socket.socket, 'connect', lambda sock, address: connected.append(address))
    def lookup(*args):
        queries.append(args)
        # A second DNS lookup would rebind to localhost. Guard must pin once.
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34' if len(queries) == 1 else '127.0.0.1', 443))]
    monkeypatch.setattr(socket, 'getaddrinfo', lookup)
    sock = SimpleNamespace(family=socket.AF_INET, type=socket.SOCK_STREAM, proto=6,
                           getpeername=lambda: ('93.184.216.34', 443))
    with worker.public_connections_only():
        socket.socket.connect(sock, ('example.com', 443))
    assert connected == [('93.184.216.34', 443)]
    assert len(queries) == 1


@pytest.mark.parametrize('address', [('127.0.0.1', 443), ('169.254.169.254', 80), ('93.184.216.34', 22)])
def test_actual_connect_blocks_private_and_non_http_ports(monkeypatch, address):
    monkeypatch.setattr(socket.socket, 'connect', lambda *args: pytest.fail('blocked address reached socket'))
    sock = SimpleNamespace(family=socket.AF_INET, type=socket.SOCK_STREAM, proto=6)
    with worker.public_connections_only(), pytest.raises(service.NetworkDownloadError):
        socket.socket.connect(sock, address)


def test_mixed_dns_answers_fail_closed(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.0.0.1', 443))])
    with pytest.raises(service.NetworkDownloadError):
        worker._checked_addresses('example.org', 443, socket.AF_INET, socket.SOCK_STREAM, 6)


@pytest.mark.parametrize('url', ['http://127.0.0.1/private', 'file:///etc/passwd', 'ftp://example.org/file'])
def test_redirects_and_internal_handler_requests_are_checked(url):
    opener = SimpleNamespace(open=lambda *a, **kw: pytest.fail('unsafe redirect opened'))
    guarded = worker._guard_opener(opener)
    with pytest.raises(service.NetworkDownloadError):
        guarded.open(urllib.request.Request(url))
    with pytest.raises(service.NetworkDownloadError):
        worker.SafeRedirect().redirect_request(urllib.request.Request('https://example.com'), None, 302, '', {}, url)


def test_worker_environment_does_not_inherit_credentials_or_proxies(tmp_path, monkeypatch):
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'OPENAI_API_KEY', 'DASHSCOPE_API_KEY',
                'COOKIE', 'PYTHONPATH', 'NODE_OPTIONS', 'DENO_AUTH_TOKENS'):
        monkeypatch.setenv(key, 'secret')
    env = service._worker_environment(tmp_path)
    assert 'secret' not in json.dumps(env)
    assert env['HOME'] == env['USERPROFILE'] == str(tmp_path)
    assert env['YTDLP_NO_PLUGINS'] == '1'


def test_solver_subprocess_allowlist_has_no_permissions_or_remote_imports(tmp_path):
    deno = str(tmp_path / 'deno.exe')
    allowed = [deno, 'run', *worker._DENO_OPTIONS, '-']
    assert worker._allowed_deno(allowed, deno)
    assert worker._allowed_deno([deno, '--version'], deno)
    for command in ([deno, 'run', '--allow-net', '-'], [deno, 'eval', 'fetch("https://example.com")'],
                    [deno, 'run', *worker._DENO_OPTIONS, '--allow-read', '-'],
                    ['node', '-'], [deno, 'run', *worker._DENO_OPTIONS, 'https://example.com/script.js']):
        assert not worker._allowed_deno(command, deno)
    with worker.public_connections_only(deno), pytest.raises(service.NetworkDownloadError):
        subprocess.Popen([sys.executable, '-c', 'print("forbidden")'])


def test_format_selection_never_delegates_network_or_selects_playlist():
    fmt = lambda **kw: dict(url='https://example.com/video.mp4', vcodec='avc1', acodec='mp4a', **kw)
    assert worker._choose_formats({'formats': [fmt(height=1080), fmt(height=720, ext='mp4'),
                                               fmt(height=360, ext='mp4')]})[0]['height'] == 720
    with pytest.raises(service.NetworkDownloadError):
        worker._choose_formats({'formats': [fmt(protocol='m3u8_native', height=720)]})
    selected = worker._choose_formats({'formats': [
        dict(url='https://example.com/v', vcodec='avc1', acodec='none', ext='mp4', height=720),
        dict(url='https://example.com/a', vcodec='none', acodec='mp4a', ext='m4a', abr=128)]})
    assert len(selected) == 2


class Response(io.BytesIO):
    status = 200
    url = 'https://example.com/a.mp4'
    def __init__(self, body, headers=None):
        super().__init__(body)
        self.headers = headers or {}


def test_stream_enforces_actual_bytes_without_content_length(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, 'MAX_BYTES', 10)
    monkeypatch.setattr(worker, '_opener', lambda: SimpleNamespace(open=lambda *a, **kw: Response(b'12345678901')))
    with pytest.raises(service.NetworkDownloadError) as exc:
        worker._download('https://example.com/a.mp4', tmp_path / 'part', {}, 0)
    assert exc.value.code == 'media_too_large'
    assert (tmp_path / 'part').stat().st_size == 0


def test_stream_rejects_truncated_or_html_media(tmp_path, monkeypatch):
    for index, response in enumerate([Response(b'123', {'Content-Length': '10'}),
                                       Response(b'<html/>', {'Content-Type': 'text/html'})]):
        monkeypatch.setattr(worker, '_opener', lambda: SimpleNamespace(open=lambda *a, **kw: response))
        with pytest.raises(service.NetworkDownloadError):
            worker._download('https://example.com/a.mp4', tmp_path / str(index), {}, 0)


def test_probe_and_full_decode_only_use_local_protocols(tmp_path, monkeypatch):
    calls = []
    def local(popen, command, **kwargs):
        calls.append(command)
        return json.dumps({'streams': [{'codec_type': 'video'}],
                           'format': {'duration': '120', 'format_name': 'mov,mp4'}}).encode()
    monkeypatch.setattr(worker, '_local_process', local)
    assert worker._probe(tmp_path / 'media', 'ffprobe', None) == (120000, '.mp4')
    assert calls[0][calls[0].index('-protocol_whitelist') + 1] == 'file,pipe'


def test_download_refuses_to_overwrite_existing_results(tmp_path):
    original = tmp_path / 'notes.md'
    original.write_text('my note', encoding='utf-8')
    with pytest.raises(service.NetworkDownloadError) as exc:
        service.download_media('https://example.com/a.mp4', tmp_path, progress=lambda _: None, cancelled=lambda: False)
    assert exc.value.code == 'target_not_empty'
    assert original.read_text(encoding='utf-8') == 'my note'


def test_cancel_kills_actual_worker_and_its_child(tmp_path, monkeypatch):
    helper = tmp_path / 'isolated_worker.py'
    helper.write_text('''import json,subprocess,sys,time
from pathlib import Path
request=json.loads(sys.stdin.buffer.readline())
marker=Path(request['target'])/'child_survived'
child=subprocess.Popen([sys.executable,'-I','-B','-c',"import time;from pathlib import Path;time.sleep(2);Path("+repr(str(marker))+").write_text('survived')"])
print(json.dumps({'event':'progress','stage':'downloading'}),flush=True)
time.sleep(30)
''', encoding='utf-8')
    original = subprocess.Popen
    children = []
    def start(args, **kwargs):
        process = original([sys.executable, '-I', '-B', str(helper)], **kwargs)
        children.append(process)
        return process
    monkeypatch.setattr(service.subprocess, 'Popen', start)
    monkeypatch.setattr(service, '_media_tool', lambda _: sys.executable)
    cancel = [False]
    def progress(data):
        cancel[0] = True
    target = tmp_path / 'download'
    with pytest.raises(service.NetworkDownloadError) as exc:
        service.download_media('https://example.com/a.mp4', target, progress=progress, cancelled=lambda: cancel[0])
    assert exc.value.code == 'cancelled'
    assert len(children) == 1 and children[0].poll() is not None
    time.sleep(2.2)
    assert not (target / 'child_survived').exists()


def test_errors_never_expose_provider_urls_or_secrets():
    assert worker._classify(RuntimeError('sign in cookie=TOPSECRET https://example.com?token=TOPSECRET')) == 'authentication_required'
    error = service.NetworkDownloadError('authentication_required', 'TOPSECRET')
    assert 'TOPSECRET' not in str(error)


def test_real_extractor_instance_only_has_guarded_python_transport(monkeypatch):
    import yt_dlp
    observed = []
    def extract(instance, url, **kwargs):
        observed.append(instance)
        assert kwargs == {'download': False, 'process': False, 'ie_key': 'Youtube'}
        assert instance.params['cookiefile'] is None
        assert instance.params['cookiesfrombrowser'] is None
        assert instance.params['usenetrc'] is False
        assert instance.params['cachedir'] is False
        assert instance.params['remote_components'] == set()
        handlers = list(instance._request_director.handlers.values())
        assert len(handlers) == 1
        assert handlers[0]._SUPPORTED_URL_SCHEMES == ('http', 'https')
        return {'id': 'TestVideo01', 'formats': []}
    monkeypatch.setattr(yt_dlp.YoutubeDL, 'extract_info', extract)
    monkeypatch.setattr(socket.socket, 'connect', lambda *a: pytest.fail('offline test connected'))
    with worker.public_connections_only():
        assert worker._extract('https://www.youtube.com/watch?v=TestVideo01')['id'] == 'TestVideo01'
    assert len(observed) == 1


@pytest.mark.parametrize(('message', 'code'), [
    ('HTTP Error 412: Precondition Failed', 'access_restricted'),
    ('HTTP Error 403: Forbidden', 'access_restricted'),
    ('HTTP Error 429: Too Many Requests', 'rate_limited'),
    ('Unable to extract initial state', 'extractor_error'),
    ('Fresh cookies are needed', 'authentication_required'),
])
def test_platform_failures_keep_distinct_actionable_categories(message, code):
    assert worker._classify(RuntimeError(message)) == code


def test_actual_deno_solver_accepts_large_anonymous_stdin(tmp_path, monkeypatch):
    deno = service._deno_tool()
    if not deno:
        pytest.skip('Optional local Deno runtime is not installed')
    from yt_dlp.extractor.youtube.jsc._builtin.deno import DenoJCP
    monkeypatch.setattr(DenoJCP, '_run_deno', DenoJCP._run_deno)
    monkeypatch.setattr(worker.tempfile, 'tempdir', str(tmp_path))
    for key, value in service._worker_environment(tmp_path).items():
        monkeypatch.setenv(key, value)
    worker._install_bounded_deno()
    instance = SimpleNamespace(runtime_info=SimpleNamespace(path=deno))
    with worker.public_connections_only(deno):
        output = DenoJCP._run_deno(instance, '/*' + 'a' * (2 * 1024 * 1024) + '*/console.log("complete")', worker._DENO_OPTIONS)
    assert output.strip() == 'complete'
    assert all(path.name == '.deno' for path in tmp_path.iterdir())
