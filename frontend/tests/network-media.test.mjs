import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import ts from 'typescript';

const asURL = value => `data:text/javascript;base64,${Buffer.from(value).toString('base64')}`;
async function moduleURL(path, replacements = {}) {
  let source = await readFile(new URL(path, import.meta.url), 'utf8');
  source = source.replace(/^import ['"].+\.css['"];?$/gm, '');
  let { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ESNext, jsx: ts.JsxEmit.ReactJSX } });
  for (const [key, value] of Object.entries(replacements)) outputText = outputText.replaceAll(`'${key}'`, `'${value}'`).replaceAll(`"${key}"`, `"${value}"`);
  return asURL(outputText);
}
const apiURL = await moduleURL('../src/networkMediaApi.ts');
const { createNetworkMediaClient, NetworkMediaError, filterSources, inspectSourcePatch, parseTags, safeSourceUrl, networkMediaUrl, downloadFileUrl } = await import(apiURL);
const json = (value, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } });
const capability = { session_token: 'local-network-token', max_batch: 20, downloader_ready: true, platforms: [], limits: { media_bytes: 524288000, duration_seconds: 7200, download_seconds: 900, max_height: 720 } };
// Synthetic source identifiers and tracking values; no user share history.
const share = '一段完整抖音分享文字\nhttps://v.douyin.com/TestDouyinA/ 复制打开\n另一段 https://v.douyin.com/TestDouyinB/\nhttps://youtu.be/TestVideo01?si=tracking\n【合成演示视频标题-哔哩哔哩】 https://b23.tv/TestB01';
const sample = (overrides = {}) => ({ id: 'source1', url: 'https://youtu.be/example', platform: 'youtube', title: '待看视频', author: '', tags: [], note: '', starred: false, archived: false, status: 'saved', progress: null, error: null, has_media: false, download_id: null, size_bytes: null, duration_ms: null, filename: null, content_type: null, revision: 0, created_at: '2026-10-23T01:00:00Z', updated_at: '2026-10-23T01:00:00Z', ...overrides });

test('collecting multiline share text preserves it and never starts a download', async () => {
  const calls = [];
  const client = createNetworkMediaClient(async (url, options) => {
    calls.push({ url, options });
    return json(url.endsWith('/capabilities') ? capability : { items: [], created: 4, duplicates: 0 });
  });
  await client.capabilities();
  const result = await client.collect(share);
  assert.equal(result.created, 4);
  assert.deepEqual(calls.map(call => call.url), ['/api/network-media/capabilities', '/api/network-media/sources']);
  assert.deepEqual(JSON.parse(calls[1].options.body), { text: share });
  assert.equal(calls[1].options.headers.get('X-Local-Media-Token'), capability.session_token);
  assert.equal(calls[0].options.headers.get('X-Local-Media-Token'), null);
});

test('unconnected, empty and overlong submissions cannot write', async () => {
  let calls = 0;
  const client = createNetworkMediaClient(async () => { calls++; return json({}); });
  await assert.rejects(client.collect(share), /先连接/);
  await assert.rejects(client.collect(' \n '), /粘贴/);
  await assert.rejects(client.collect('字'.repeat(20001)), /20,000/);
  assert.equal(calls, 0);
});

test('only explicit download and cancel commands touch their endpoints', async () => {
  const calls = [];
  const client = createNetworkMediaClient(async (url, options) => { calls.push({ url, options }); return json(url.endsWith('/capabilities') ? capability : sample()); });
  await client.capabilities();
  await client.download('source/1');
  await client.cancel('source/1');
  assert.deepEqual(calls.slice(1).map(call => [call.url, call.options.method]), [['/api/network-media/sources/source%2F1/download', 'POST'], ['/api/network-media/sources/source%2F1/cancel', 'POST']]);
  assert.ok(calls.every(call => call.options.credentials === 'same-origin'));
});

test('metadata saves use the original revision and never retry a conflict', async () => {
  const patch = { title: '我的标题', tags: ['研究'], note: '保留这份草稿', expected_revision: 7 };
  let writes = 0;
  const client = createNetworkMediaClient(async (url, options) => {
    if (url.endsWith('/capabilities')) return json(capability);
    writes++;
    assert.equal(options.method, 'PATCH');
    assert.deepEqual(JSON.parse(options.body), patch);
    return json({ detail: { code: 'source_conflict', message: 'PRIVATE_RAW_DETAIL' } }, 409);
  });
  await client.capabilities();
  await assert.rejects(client.update('source1', patch), error => error instanceof NetworkMediaError && error.status === 409 && /草稿/.test(error.message));
  assert.equal(writes, 1);
  assert.equal(patch.note, '保留这份草稿');
  assert.equal(patch.expected_revision, 7);
});

test('archiving and restoring patch only the requested state with CAS', async () => {
  const bodies = [];
  const client = createNetworkMediaClient(async (url, options) => {
    if (url.endsWith('/capabilities')) return json(capability);
    bodies.push(JSON.parse(options.body)); return json(sample());
  });
  await client.capabilities();
  await client.update('source1', { archived: true, expected_revision: 0 });
  await client.update('source1', { archived: false, expected_revision: 1 });
  assert.deepEqual(bodies, [{ archived: true, expected_revision: 0 }, { archived: false, expected_revision: 1 }]);
});

test('metadata validation protects missing revisions, unicode titles and tag limits', () => {
  assert.match(inspectSourcePatch({ title: '标题' }), /版本/);
  assert.match(inspectSourcePatch({ title: '  ', expected_revision: 0 }), /标题/);
  assert.equal(inspectSourcePatch({ title: '😀'.repeat(256), expected_revision: 0 }), '');
  assert.match(inspectSourcePatch({ title: '😀'.repeat(257), expected_revision: 0 }), /256/);
  assert.match(inspectSourcePatch({ tags: Array.from({ length: 21 }, (_, i) => `标签${i}`), expected_revision: 0 }), /20/);
  assert.match(inspectSourcePatch({ tags: ['字'.repeat(41)], expected_revision: 0 }), /40/);
  assert.deepEqual(parseTags(' 研究，学习,研究\n待看 '), ['研究', '学习', '待看']);
});

test('search includes tags and notes while archive and platform filters remain independent', () => {
  const a = sample({ id: 'a', tags: ['设备'], starred: true });
  const b = sample({ id: 'b', platform: 'bilibili', archived: true, note: '医疗设备' });
  const c = sample({ id: 'c', platform: 'direct', status: 'interrupted', download_id: 'attempt-c', archived: true });
  assert.deepEqual(filterSources([a, b, c], 'all', 'all', '设备').map(item => item.id), ['a']);
  assert.deepEqual(filterSources([a, b, c], 'archived', 'bilibili', '医疗').map(item => item.id), ['b']);
  assert.deepEqual(filterSources([a, b, c], 'starred', 'all', '').map(item => item.id), ['a']);
  assert.deepEqual(filterSources([a, b, c], 'downloads', 'all', '').map(item => item.id), ['c']);
  const attempts = ['failed', 'cancelled', 'interrupted', 'downloading', 'ready'].map((status, i) => sample({ id: `record-${i}`, download_id: `attempt-${i}`, status }));
  assert.equal(filterSources([a, ...attempts], 'downloads', 'all', '').length, 5);
});

test('untrusted server bodies and inherited object keys never become displayed errors', async () => {
  for (const code of ['unknown', '__proto__', 'constructor']) {
    const client = createNetworkMediaClient(async () => json({ detail: { code, message: 'PRIVATE_SECRET C:/private/file' } }, 500));
    await assert.rejects(client.sources(), error => error instanceof NetworkMediaError && !/PRIVATE_SECRET|C:\/private|object Object|function Object/.test(error.message));
  }
});

test('external source links reject executable schemes and embedded credentials', () => {
  assert.equal(safeSourceUrl('javascript:alert(1)'), undefined);
  assert.equal(safeSourceUrl('https://user:secret@example.com/video'), undefined);
  assert.equal(safeSourceUrl('file:///private/video.mp4'), undefined);
  assert.equal(safeSourceUrl('https://youtu.be/example'), 'https://youtu.be/example');
  assert.equal(networkMediaUrl('source/1'), '/api/network-media/sources/source%2F1/media');
  assert.equal(downloadFileUrl('source/1'), '/api/network-media/sources/source%2F1/download-file');
});

// Run actual form handlers with a small React hook harness, without browser or network.
const reactURL = asURL(`
function slot(initial) { const h=globalThis.__networkHarness; const i=h.cursor++; if(!(i in h.slots)) h.slots[i]=typeof initial==='function'?initial():initial; return [h,i]; }
export function useState(initial){const[h,i]=slot(initial);return[h.slots[i],value=>{h.slots[i]=typeof value==='function'?value(h.slots[i]):value}];}
export function useRef(initial){const[h,i]=slot({current:initial});return h.slots[i];}
export function useEffect(effect,deps){const[h,i]=slot(undefined);const old=h.slots[i];if(!old||deps.some((v,j)=>v!==old[j]))h.effects.push(effect);h.slots[i]=deps;}
export function useCallback(fn){return fn;}
export function useDeferredValue(value){return value;}
export function lazy(){return function LazyComponent(){};}
export function Suspense(){return null;}
`);
const jsxURL = asURL('export const jsx=(type,props)=>({type,props});export const jsxs=jsx;export const Fragment="fragment";');
const icons = 'Archive ArrowDownToLine ArrowUpRight Check ChevronRight CircleHelp Download FileVideo FolderOpen Layers Link LoaderCircle Plus RefreshCw Save Search Star Tag Video X'.split(' ');
const iconsURL = asURL(icons.map(name => `export const ${name}=()=>null;`).join(''));
const stubURL = asURL('export default function Stub(){return null;}');
const bridgeURL = asURL(`export * from '${apiURL}';export const networkMediaApi={capabilities:(...a)=>globalThis.__networkHarness.api.capabilities(...a),sources:(...a)=>globalThis.__networkHarness.api.sources(...a),collect:(...a)=>globalThis.__networkHarness.api.collect(...a),update:(...a)=>globalThis.__networkHarness.api.update(...a)};`);
const { default: NetworkMediaLibrary } = await import(await moduleURL('../src/pages/NetworkMediaLibrary.tsx', { react: reactURL, 'react/jsx-runtime': jsxURL, 'lucide-react': iconsURL, '../networkMediaApi': bridgeURL }));
const oldApiURL = asURL('export async function loginStatus(){globalThis.__networkHarness.loginCalls++;return{status:"not_logged_in"};}');
const { default: App } = await import(await moduleURL('../src/App.tsx', { react: reactURL, 'react/jsx-runtime': jsxURL, './pages/LandingPage': stubURL, './pages/Workspace': stubURL, './components/LoginModal': stubURL, './pages/LocalMediaWorkspace': stubURL, './pages/NetworkMediaLibrary': stubURL, './api': oldApiURL }));
function findAll(node, predicate) {
  if (node == null || typeof node !== 'object') return [];
  if (Array.isArray(node)) return node.flatMap(value => findAll(value, predicate));
  return [...(predicate(node) ? [node] : []), ...findAll(node.props?.children, predicate)];
}
const tick = () => new Promise(resolve => setImmediate(resolve));
async function harness(run, api, hash = '#videos') {
  const oldWindow = globalThis.window;
  globalThis.window = { location: { hash, pathname: '/', search: '' }, addEventListener() {}, removeEventListener() {}, history: { replaceState() {} }, confirm: () => true };
  const h = { slots: [], cursor: 0, effects: [], cleanups: [], api, loginCalls: 0 };
  globalThis.__networkHarness = h;
  const render = (Component = NetworkMediaLibrary) => {
    h.cursor = 0;
    const tree = Component({ active: true, onOpenLocal() {}, onOpenDouyin() {} });
    h.effects.splice(0).forEach(effect => { const cleanup = effect(); if (cleanup) h.cleanups.push(cleanup); });
    return tree;
  };
  try { await run({ h, render }); }
  finally { h.cleanups.forEach(cleanup => cleanup()); globalThis.window = oldWindow; delete globalThis.__networkHarness; }
}

test('share form keeps the whole input pending and prevents double submits, then reports four sources', async () => {
  let resolve;
  let submissions = 0;
  await harness(async ({ render }) => {
    let tree = render(); await tick(); tree = render();
    findAll(tree, node => node.type === 'textarea')[0].props.onChange({ target: { value: share } }); tree = render();
    const submit = findAll(tree, node => node.type === 'form')[0].props.onSubmit;
    submit({ preventDefault() {} }); submit({ preventDefault() {} }); tree = render();
    assert.equal(findAll(tree, node => node.type === 'textarea')[0].props.value, share);
    assert.equal(submissions, 1);
    resolve({ items: [sample()], created: 4, duplicates: 0 }); await tick(); tree = render();
    const status = findAll(tree, node => node.props?.role === 'status');
    assert.ok(status.some(node => JSON.stringify(node.props.children).includes('已新增 4 条链接')));
  }, { capabilities: async () => capability, sources: async () => ({ items: [] }), collect: async text => { submissions++; assert.equal(text, share); return new Promise(done => { resolve = done; }); } });
});

test('a failed save preserves the original multiline share text for correction', async () => {
  await harness(async ({ render }) => {
    let tree = render(); await tick(); tree = render();
    findAll(tree, node => node.type === 'textarea')[0].props.onChange({ target: { value: share } }); tree = render();
    findAll(tree, node => node.type === 'form')[0].props.onSubmit({ preventDefault() {} }); await tick(); tree = render();
    assert.equal(findAll(tree, node => node.type === 'textarea')[0].props.value, share);
    assert.ok(findAll(tree, node => node.props?.role === 'alert').length > 0);
  }, { capabilities: async () => capability, sources: async () => ({ items: [] }), collect: async () => { throw new NetworkMediaError('invalid_url', '链接格式不正确', 400); } });
});

const detail = tree => findAll(tree, node => typeof node.type === 'function' && node.props?.source && node.props?.onSave)[0];
test('metadata saves prevent double submits and preserve edits made while the request is pending', async () => {
  let resolve;
  let submissions = 0;
  const original = sample();
  await harness(async ({ render }) => {
    render(); await tick(); let tree = render();
    detail(tree).props.onEdit({ title: '  我整理的标题  ' }); tree = render();
    const save = detail(tree).props.onSave;
    save(); save(); tree = render();
    assert.equal(submissions, 1);
    assert.equal(detail(tree).props.busy, true);
    detail(tree).props.onEdit({ note: '保存期间新写的备注' });
    resolve({ ...original, title: '我整理的标题', revision: 1 }); await tick(); tree = render();
    assert.equal(detail(tree).props.draft.note, '保存期间新写的备注');
    assert.equal(detail(tree).props.draft.revision, 1);
    assert.equal(detail(tree).props.draft.saving, false);
  }, { capabilities: async () => capability, sources: async () => ({ items: [original] }), update: async () => { submissions++; return new Promise(done => { resolve = done; }); } });
});

test('metadata conflicts retain the draft and require explicit merge instead of automatic retry', async () => {
  let submissions = 0;
  let latest = sample();
  await harness(async ({ render }) => {
    render(); await tick(); let tree = render();
    detail(tree).props.onEdit({ note: '我的草稿' }); tree = render();
    detail(tree).props.onSave(); await tick(); tree = render(); await tick(); tree = render();
    assert.equal(submissions, 1);
    assert.equal(detail(tree).props.draft.note, '我的草稿');
    assert.equal(detail(tree).props.draft.conflict, true);
    assert.equal(detail(tree).props.draft.revision, 0);
    assert.equal(detail(tree).props.source.note, '另一页保存的备注');
  }, { capabilities: async () => capability, sources: async () => ({ items: [latest] }), update: async () => { submissions++; latest = sample({ note: '另一页保存的备注', revision: 1 }); throw new NetworkMediaError('source_conflict', '草稿已保留', 409); } });
});

test('a failed connection capability remains visible when reading the source list succeeds', async () => {
  await harness(async ({ render }) => {
    render(); await tick(); const tree = render();
    assert.ok(findAll(tree, node => node.props?.role === 'alert').some(node => JSON.stringify(node.props.children).includes('无法连接')));
  }, { capabilities: async () => { throw new Error('unavailable'); }, sources: async () => ({ items: [] }) });
});

test('default network route never polls Douyin login; the explicitly opened old route still can', async () => {
  for (const hash of ['', '#videos', '#local', '#douyin']) {
    await harness(async ({ render, h }) => { render(App); await tick(); assert.equal(h.loginCalls, hash === '#douyin' ? 1 : 0); }, {}, hash);
  }
});
