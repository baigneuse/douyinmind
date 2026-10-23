import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import ts from 'typescript';

const source = await readFile(new URL('../src/localMediaApi.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ESNext } });
const { createLocalMediaClient, inspectLocalImport, formatTimestamp, mediaUrl, exportUrl, EXPORT_FILES, LocalMediaError } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`);
const json = (value, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } });
const capabilities = {
  ffmpeg_ready: true, asr_configured: true, asr_model: 'test-model', asr_endpoint: 'https://example.invalid',
  cloud_verified: false, session_token: 'test-session-token',
  limits: { media_bytes: 20_000, subtitle_bytes: 2000, duration_seconds: 180 },
};
const media = new File(['video fixture'], '中文_日本語.mp4');
const subtitles = new File(['WEBVTT\n'], '字幕_日本語.vtt');

test('writes are blocked before connecting and tokens never enter read URLs', async () => {
  const calls = [];
  const client = createLocalMediaClient(async (url, options) => {
    calls.push({ url, options });
    return json(url.endsWith('/capabilities') ? capabilities : { id: 'job1' });
  });
  await assert.rejects(client.upload(media, 'media'), /先连接/);
  assert.equal(calls.length, 0);
  await client.capabilities();
  await client.cancel('job1');
  await client.assets();
  assert.equal(calls[1].options.headers.get('X-Local-Media-Token'), capabilities.session_token);
  assert.equal(calls[2].options.headers.get('X-Local-Media-Token'), null);
  assert.ok(calls.every(call => !call.url.includes(capabilities.session_token)));
  assert.ok(calls.every(call => call.options.credentials === 'same-origin'));
});

test('raw upload preserves bytes and safely encodes multilingual reserved filenames', async () => {
  const file = new File(['binary\u0000content'], '中文_日本語 & #?.mp4');
  let upload;
  const client = createLocalMediaClient(async (url, options) => {
    if (url.endsWith('/capabilities')) return json(capabilities);
    upload = { url, options };
    return json({ upload_id: 'u1', filename: file.name, size_bytes: file.size });
  });
  await client.capabilities();
  const controller = new AbortController();
  assert.equal((await client.upload(file, 'media', controller.signal)).upload_id, 'u1');
  assert.equal(new URL(upload.url, 'http://localhost').searchParams.get('filename'), file.name);
  assert.equal(upload.options.body, file);
  assert.equal(upload.options.signal, controller.signal);
  assert.equal(upload.options.headers.get('Content-Type'), 'application/octet-stream');
});

test('cloud work cannot be submitted without explicit consent or a finite request budget', async () => {
  let writes = 0;
  const client = createLocalMediaClient(async (url) => {
    if (url.endsWith('/capabilities')) return json(capabilities);
    writes++;
    return json({ id: 'should-not-run' });
  });
  await client.capabilities();
  const input = { media_upload_id: 'u1', mode: 'qwen', allow_cloud_asr: false, max_requests: 8 };
  await assert.rejects(client.createJob(input), /确认/);
  for (const max_requests of [0, 101, 1.5, NaN, Infinity]) {
    await assert.rejects(client.createJob({ ...input, allow_cloud_asr: true, max_requests }), /1–100/);
    await assert.rejects(client.retry('job1', true, max_requests), /1–100/);
  }
  assert.equal(writes, 0);
});

test('retry sends only the newly granted budget and consent, never changed source paths', async () => {
  let sent;
  const client = createLocalMediaClient(async (url, options) => {
    if (url.endsWith('/capabilities')) return json(capabilities);
    sent = { url, body: JSON.parse(options.body) };
    return json({ id: 'job2' });
  });
  await client.capabilities();
  assert.equal((await client.retry('job/1', true, 3)).id, 'job2');
  assert.equal(sent.url, '/api/local-media/jobs/job%2F1/retry');
  assert.deepEqual(sent.body, { allow_cloud_asr: true, max_requests: 3 });
});

test('notes save keeps the supplied revision and exposes conflict without retrying or overwriting', async () => {
  let writes = 0;
  const draft = { text: '保留我的人工笔记', revision: 'revision-old' };
  const client = createLocalMediaClient(async (url, options) => {
    if (url.endsWith('/capabilities')) return json(capabilities);
    writes++;
    assert.equal(options.method, 'PUT');
    assert.deepEqual(JSON.parse(options.body), draft);
    return json({ detail: { code: 'notes_conflict', message: 'version changed' } }, 409);
  });
  await client.capabilities();
  await assert.rejects(client.saveNotes('asset1', draft), error => error instanceof LocalMediaError && error.status === 409 && /草稿/.test(error.message));
  assert.deepEqual(draft, { text: '保留我的人工笔记', revision: 'revision-old' });
  assert.equal(writes, 1);
});

test('untrusted server error bodies never expose provider credentials, HTML or private paths', async () => {
  for (const response of [json({ detail: { code: 'unknown', message: 'PRIVATE_SECRET C:/private/transcript.txt' } }, 500), new Response('<html>PRIVATE_SECRET</html>', { status: 502 }), json({ detail: [{ input: 'PRIVATE_SECRET' }] }, 422)]) {
    const client = createLocalMediaClient(async () => response);
    await assert.rejects(client.assets(), error => !/PRIVATE_SECRET|C:\/private|<html>/.test(error.message) && error instanceof LocalMediaError);
  }
  for (const code of ['__proto__', 'constructor', 'toString']) {
    const client = createLocalMediaClient(async () => json({ detail: { code } }, 500));
    await assert.rejects(client.assets(), error => error instanceof LocalMediaError && error.message === '操作未完成，请稍后重试。已保存的资料不会因此丢失。');
  }
});

test('offline mode requires valid subtitles but no cloud credentials or grant', () => {
  const offline = { ...capabilities, asr_configured: false };
  assert.equal(inspectLocalImport(media, subtitles, 'subtitles', false, 8, offline), '');
  assert.match(inspectLocalImport(media, null, 'subtitles', false, 8, offline), /字幕/);
  assert.match(inspectLocalImport(media, new File(['x'], 'notes.txt'), 'subtitles', false, 8, offline), /SRT\/VTT/);
});

test('preflight rejects unsafe inputs and cloud consent never carries into a missing configuration', () => {
  assert.match(inspectLocalImport(null, subtitles, 'subtitles', false, 8, capabilities), /选择/);
  assert.match(inspectLocalImport(new File(['x'], 'playlist.m3u8'), subtitles, 'subtitles', false, 8, capabilities), /支持/);
  assert.match(inspectLocalImport(new File([], 'empty.mp4'), subtitles, 'subtitles', false, 8, capabilities), /非空/);
  assert.match(inspectLocalImport(new File(['x'.repeat(20001)], 'large.mp4'), subtitles, 'subtitles', false, 8, capabilities), /不超过/);
  assert.match(inspectLocalImport(media, null, 'qwen', false, 8, capabilities), /授权/);
  assert.match(inspectLocalImport(media, null, 'qwen', true, 8, { ...capabilities, asr_configured: false }), /尚未配置/);
  assert.equal(inspectLocalImport(media, null, 'qwen', true, 8, capabilities), '');
});

test('playback timestamps retain milliseconds across minutes and hour boundaries', () => {
  assert.equal(formatTimestamp(1234), '00:01.234');
  assert.equal(formatTimestamp(59999), '00:59.999');
  assert.equal(formatTimestamp(3600001), '1:00:00.001');
});

test('download and media URLs contain opaque ids only and exports use a fixed allowlist', () => {
  assert.equal(mediaUrl('asset/1'), '/api/local-media/assets/asset%2F1/media');
  assert.equal(EXPORT_FILES.length, 5);
  for (const file of EXPORT_FILES) assert.ok(exportUrl('a1', file).endsWith(`/export/${file}`));
  assert.throws(() => exportUrl('a1', '../../.env'), /不支持/);
});
