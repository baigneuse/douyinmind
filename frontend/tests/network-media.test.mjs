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
const icons = 'Archive ArrowDownToLine ArrowUpRight Check ChevronRight CircleHelp Download FileVideo FolderOpen Layers Link LoaderCircle Plug Plus RefreshCw Save Search Star Tag UserRound Video X'.split(' ');
const iconsURL = asURL(icons.map(name => `export const ${name}=()=>null;`).join(''));
const stubURL = asURL('export default function Stub(){return null;}');
// TikHub calls fall back to "provider not enabled" unless a test supplies them.
const bridged = ['capabilities', 'sources', 'collect', 'update', 'download', 'cancel', 'tikhubStatus', 'tikhubCheck', 'tikhubResolve', 'tikhubCreator', 'tikhubImport'];
const bridgeURL = asURL(`export * from '${apiURL}';const call=name=>(...a)=>{const f=globalThis.__networkHarness.api[name];return f?f(...a):Promise.reject(new Error(name+' unavailable'));};export const networkMediaApi={${bridged.map(name => `${name}:call('${name}')`).join(',')}};`);
const panelURL = await moduleURL('../src/pages/TikHubPanel.tsx', { react: reactURL, 'react/jsx-runtime': jsxURL, 'lucide-react': iconsURL, '../networkMediaApi': bridgeURL });
const { default: TikHubPanel } = await import(panelURL);
const { default: NetworkMediaLibrary } = await import(await moduleURL('../src/pages/NetworkMediaLibrary.tsx', { react: reactURL, 'react/jsx-runtime': jsxURL, 'lucide-react': iconsURL, '../networkMediaApi': bridgeURL, './TikHubPanel': panelURL }));
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

// ----- TikHub source: synthetic identifiers; no provider, token or signed address is involved. -----
const { tikhubLookupCost, tikhubState, usesTikHub, durationLabel, safeCoverUrl, costLabel } = await import(apiURL);
const bucket = (overrides = {}) => ({ requests: 0, succeeded: 0, failed: 0, paid_attempts: 0, billable: 0, estimated_cost_usd: 0, ...overrides });
const tikhubStatus = (overrides = {}) => ({
  available: true, configured: true, endpoint: 'api.tikhub.io', endpoint_valid: true, cookie_access: false, page_size: 20,
  prices: { per_paid_request_usd: 0.001, checked_on: '2026-11-16', free: ['demo_video', 'account'] },
  usage: { today: bucket(), total: bucket(), daily_limit: 20, remaining_today: 20 }, last_check: null,
  comparison: { methods: {}, sources: { resolved: 0, downloaded: 0, needs_permission: 0 } }, ...overrides });
const previewItem = (overrides = {}) => ({ item_id: '7000000000000000001', title: '合成演示作品', author: '合成作者', duration_ms: 29400, published_at: '2025-08-04T08:16:31+00:00', kind: 'video', downloadable: true, reason: null, reason_message: null, cover_url: 'https://p1.image.example.com/cover.jpeg', in_library: false, source_id: null, has_media: false, ...overrides });
const tikhubPreview = (overrides = {}) => ({ preview_id: 'a'.repeat(32), kind: 'video', creator: null, items: [previewItem()], has_more: false, next_cursor: null, requests_used: 1, estimated_cost_usd: 0.001, usage: tikhubStatus().usage, ...overrides });
const douyin = (overrides = {}) => sample({ id: 'douyin1', url: 'https://v.douyin.com/TestDouyinA/', platform: 'douyin', supported: true, provider: 'tikhub', platform_item_id: '7000000000000000001', title: '合成演示作品', ...overrides });
const confirmationRequired = () => new NetworkMediaError('tikhub_confirmation_required', '需要先确认', 409);
const text = node => JSON.stringify(node?.props?.children ?? '');

test('tikhub client sends confirmations explicitly and never carries a provider token or cookie', async () => {
  const calls = [];
  const client = createNetworkMediaClient(async (url, options) => { calls.push({ url, options }); return json(url.endsWith('/capabilities') ? capability : tikhubPreview()); });
  await assert.rejects(client.tikhubResolve('https://v.douyin.com/TestDouyinA/', true), /先连接/);
  await client.capabilities();
  await client.tikhubStatus();
  await client.tikhubCheck('demo');
  await client.tikhubResolve('  https://v.douyin.com/TestDouyinA/  ', false);
  await client.tikhubCreator('https://www.douyin.com/user/MS4wLjABAAAAsynthetic', 1754000000000, true);
  await client.tikhubImport('a'.repeat(32), ['7000000000000000001']);
  await client.download('douyin1', { confirm_paid: true });
  await client.download('douyin1', { method: 'anonymous' });
  assert.deepEqual(calls.slice(1).map(call => [call.url, call.options.method ?? 'GET']), [
    ['/api/network-media/tikhub/status', 'GET'], ['/api/network-media/tikhub/check', 'POST'], ['/api/network-media/tikhub/resolve', 'POST'],
    ['/api/network-media/tikhub/creator', 'POST'], ['/api/network-media/tikhub/import', 'POST'],
    ['/api/network-media/sources/douyin1/download', 'POST'], ['/api/network-media/sources/douyin1/download', 'POST']]);
  assert.deepEqual(calls.slice(2).map(call => JSON.parse(call.options.body)), [
    { mode: 'demo' }, { text: 'https://v.douyin.com/TestDouyinA/', confirm_paid: false },
    { text: 'https://www.douyin.com/user/MS4wLjABAAAAsynthetic', cursor: 1754000000000, confirm_paid: true },
    { preview_id: 'a'.repeat(32), item_ids: ['7000000000000000001'] }, { confirm_paid: true }, { method: 'anonymous' }]);
  for (const call of calls) {
    assert.equal(call.options.headers.get('Authorization'), null);
    assert.equal(call.options.headers.get('Cookie'), null);
    assert.ok(!/token|key|cookie/i.test(call.options.body ?? ''));
  }
  await assert.rejects(client.tikhubResolve('  ', true), /粘贴/);
  await assert.rejects(client.tikhubImport('a'.repeat(32), []), /1–20/);
  await assert.rejects(client.tikhubImport('a'.repeat(32), Array.from({ length: 21 }, (_, i) => String(7000000000000000000 + i))), /1–20/);
  assert.equal(calls.length, 8);
});

test('tikhub failures show fixed guidance instead of provider text', async () => {
  for (const [code, pattern] of [['tikhub_not_configured', /TIKHUB_API_KEY/], ['tikhub_budget_exhausted', /每日请求上限/], ['tikhub_unauthorized', /Token/], ['tikhub_payment_required', /余额/], ['private_content', /私密/], ['already_downloaded', /没有重复/], ['tikhub_preview_expired', /重新解析/]]) {
    const client = createNetworkMediaClient(async url => url.endsWith('/capabilities') ? json(capability) : json({ detail: { code, message: 'PROVIDER-PRIVATE-TEXT Bearer secret' } }, 409));
    await client.capabilities();
    await assert.rejects(client.tikhubResolve('https://v.douyin.com/TestDouyinA/', true), error => error.code === code && pattern.test(error.message) && !/PROVIDER|Bearer/.test(error.message));
  }
});

test('tikhub helpers describe cost, state and which sources use the provider', () => {
  assert.equal(tikhubLookupCost('video', 'https://v.douyin.com/TestDouyinA/'), 1);
  assert.equal(tikhubLookupCost('creator', 'https://www.douyin.com/user/MS4wLjABAAAAsynthetic'), 1);
  assert.equal(tikhubLookupCost('creator', 'https://v.douyin.com/TestUserA/'), 2);
  assert.equal(costLabel(2, 0.001), '$0.002');
  assert.equal(durationLabel(29400), '0:29');
  assert.equal(durationLabel(null), '');
  assert.equal(safeCoverUrl('https://p1.image.example.com/c.jpeg'), 'https://p1.image.example.com/c.jpeg');
  for (const unsafe of ['http://p1.image.example.com/c.jpeg', 'javascript:alert(1)', 'https://user:pw@example.com/c.jpeg', null]) assert.equal(safeCoverUrl(unsafe), undefined);
  assert.equal(tikhubState(null), 'unavailable');
  assert.equal(tikhubState(tikhubStatus({ configured: false })), 'unconfigured');
  assert.equal(tikhubState(tikhubStatus()), 'configured');
  assert.equal(tikhubState(tikhubStatus({ last_check: { mode: 'demo', ok: true } })), 'configured');
  assert.equal(tikhubState(tikhubStatus({ last_check: { mode: 'account', ok: true } })), 'ready');
  assert.equal(tikhubState(tikhubStatus({ last_check: { mode: 'account', ok: false } })), 'error');
  assert.equal(tikhubState(tikhubStatus({ endpoint_valid: false })), 'error');
  assert.equal(usesTikHub(douyin(), tikhubStatus()), true);
  assert.equal(usesTikHub(douyin(), tikhubStatus({ configured: false })), false);
  assert.equal(usesTikHub(douyin({ supported: false }), tikhubStatus()), false);
  assert.equal(usesTikHub(sample(), tikhubStatus()), false);
  assert.equal(usesTikHub(douyin(), null), false);
});

async function panelHarness(run, api, props = {}) {
  const events = { statuses: [], imports: [] };
  await harness(async ({ h }) => {
    const render = () => { h.cursor = 0; return TikHubPanel({ status: tikhubStatus(), connected: true, onStatus: value => events.statuses.push(value), onImported: (result, download) => { events.imports.push({ result, download }); }, ...props }); };
    await run({ render, events });
  }, api);
}
const panelForm = tree => findAll(tree, node => node.type === 'form')[0];
const dialog = tree => findAll(tree, node => node.props?.role === 'alertdialog')[0];
const button = (tree, label) => findAll(tree, node => node.type === 'button' && text(node).includes(label))[0];

test('tikhub panel shows connection state and hides lookups until a token is configured', async () => {
  await panelHarness(async ({ render }) => {
    const tree = render();
    assert.ok(findAll(tree, node => node.props?.role === 'status').some(node => text(node).includes('未配置 Token')));
    assert.equal(panelForm(tree), undefined);
    assert.ok(text(tree).includes('TIKHUB_API_KEY') && text(tree).includes('匿名下载方式'));
    assert.equal(button(tree, '免费 Demo 测试').props.disabled, false);
    assert.equal(button(tree, '检查 Token').props.disabled, true);
  }, {}, { status: tikhubStatus({ configured: false }) });
  await panelHarness(async ({ render }) => { assert.equal(render(), null); }, {}, { status: null });
});

test('a paid lookup is sent only after the user confirms that one request', async () => {
  const requests = [];
  await panelHarness(async ({ render }) => {
    let tree = render();
    findAll(tree, node => node.type === 'input' && node.props.id === 'tikhub-link')[0].props.onChange({ target: { value: 'https://v.douyin.com/TestDouyinA/' } }); tree = render();
    assert.equal(dialog(tree), undefined);
    panelForm(tree).props.onSubmit({ preventDefault() {} }); await tick(); tree = render();
    assert.deepEqual(requests, [['https://v.douyin.com/TestDouyinA/', false]]);
    assert.ok(text(dialog(tree)).includes('$0.001') && text(dialog(tree)).includes('TikHub 账单'));
    assert.equal(findAll(tree, node => node.props?.className === 'nv-tk-preview')[0], undefined);
    button(dialog(tree), '取消').props.onClick(); tree = render();
    assert.equal(dialog(tree), undefined);
    assert.equal(requests.length, 1);
    panelForm(tree).props.onSubmit({ preventDefault() {} }); await tick(); tree = render();
    button(dialog(tree), '确认并发送').props.onClick(); await tick(); tree = render();
    assert.deepEqual(requests.at(-1), ['https://v.douyin.com/TestDouyinA/', true]);
    assert.equal(dialog(tree), undefined);
    assert.ok(text(tree).includes('本次使用') && text(tree).includes('合成演示作品'));
  }, { tikhubResolve: async (value, confirmed) => { requests.push([value, confirmed]); if (!confirmed) throw confirmationRequired(); return tikhubPreview(); } });
});

test('a post already in the library is previewed without a prompt or a request', async () => {
  await panelHarness(async ({ render }) => {
    let tree = render();
    findAll(tree, node => node.type === 'input' && node.props.id === 'tikhub-link')[0].props.onChange({ target: { value: 'https://www.douyin.com/video/7000000000000000001' } }); tree = render();
    panelForm(tree).props.onSubmit({ preventDefault() {} }); await tick(); tree = render();
    assert.equal(dialog(tree), undefined);
    assert.ok(text(tree).includes('未发送请求') && text(tree).includes('已下载到本机'));
    const box = findAll(tree, node => node.type === 'input' && node.props.type === 'checkbox' && node.props['aria-label'])[0];
    assert.equal(box.props.disabled, true);
    assert.equal(button(tree, '加入并下载').props.disabled, true);
  }, { tikhubResolve: async () => tikhubPreview({ requests_used: 0, estimated_cost_usd: 0, items: [previewItem({ in_library: true, has_media: true, source_id: 'douyin1' })] }) });
});

test('creator preview lets the user tick posts and import only the selection', async () => {
  const imported = [];
  const items = [previewItem(), previewItem({ item_id: '7000000000000000002', title: '第二条' }), previewItem({ item_id: '7000000000000000003', title: '图文', kind: 'images', downloadable: false, reason: 'image_post', reason_message: '这是图文作品，没有可下载的视频文件；来源已保留。' })];
  await panelHarness(async ({ render, events }) => {
    let tree = render();
    button(tree, '导入博主公开视频').props.onClick(); tree = render();
    findAll(tree, node => node.type === 'input' && node.props.id === 'tikhub-link')[0].props.onChange({ target: { value: 'https://v.douyin.com/TestUserA/' } }); tree = render();
    panelForm(tree).props.onSubmit({ preventDefault() {} }); await tick(); tree = render();
    assert.ok(text(dialog(tree)).includes('2') && text(dialog(tree)).includes('$0.002'));
    button(dialog(tree), '确认并发送').props.onClick(); await tick(); tree = render();
    assert.ok(text(tree).includes('合成作者') && text(tree).includes('图文作品'));
    const boxes = () => findAll(tree, node => node.type === 'input' && node.props.type === 'checkbox' && node.props['aria-label']);
    assert.deepEqual(boxes().map(box => box.props.checked), [true, true, true]);
    boxes()[1].props.onChange(); tree = render();
    assert.ok(text(tree).includes('已选') && boxes()[1].props.checked === false);
    assert.equal(button(tree, '下一页').props.disabled, false);
    button(tree, '加入并下载').props.onClick(); await tick(); tree = render();
    assert.deepEqual(imported, [['b'.repeat(32), ['7000000000000000001', '7000000000000000003']]]);
    assert.equal(events.imports[0].download, true);
    assert.equal(findAll(tree, node => node.props?.className === 'nv-tk-preview')[0], undefined);
  }, {
    tikhubCreator: async (value, cursor, confirmed) => { if (!confirmed) throw confirmationRequired(); return tikhubPreview({ preview_id: 'b'.repeat(32), kind: 'creator', creator: { sec_user_id: 'MS4wLjABAAAAsynthetic', nickname: '合成作者', url: 'https://www.douyin.com/user/MS4wLjABAAAAsynthetic' }, items, has_more: true, next_cursor: 1754000000000, requests_used: 2, estimated_cost_usd: 0.002 }); },
    tikhubImport: async (id, ids) => { imported.push([id, ids]); return { items: [douyin()], created: 2, duplicates: 0 }; },
  });
});

test('free checks report their result and failed lookups keep the pasted link', async () => {
  await panelHarness(async ({ render, events }) => {
    let tree = render();
    button(tree, '免费 Demo 测试').props.onClick(); await tick(); tree = render();
    assert.equal(events.statuses.at(-1).last_check.ok, true);
    findAll(tree, node => node.type === 'input' && node.props.id === 'tikhub-link')[0].props.onChange({ target: { value: 'https://v.douyin.com/TestDouyinA/' } }); tree = render();
    panelForm(tree).props.onSubmit({ preventDefault() {} }); await tick(); tree = render();
    assert.ok(findAll(tree, node => node.props?.role === 'alert').some(node => text(node).includes('每日请求上限')));
    assert.equal(findAll(tree, node => node.type === 'input' && node.props.id === 'tikhub-link')[0].props.value, 'https://v.douyin.com/TestDouyinA/');
  }, {
    tikhubCheck: async mode => ({ mode, ok: true, status: tikhubStatus({ last_check: { mode, ok: true, sample: { title: '合成演示作品', candidates: 5, codec: 'h264' } } }) }),
    tikhubResolve: async () => { throw new NetworkMediaError('tikhub_budget_exhausted', '已达到本机设置的 TikHub 每日请求上限，没有发送请求。', 409); },
  });
});

test('downloading a douyin source through tikhub asks before any paid request', async () => {
  const downloads = [];
  await harness(async ({ render }) => {
    render(); await tick(); let tree = render();
    assert.equal(detail(tree).props.paidPrompt, false);
    detail(tree).props.onDownload(); await tick(); tree = render();
    assert.deepEqual(downloads, [{}]);
    assert.equal(detail(tree).props.paidPrompt, true);
    assert.equal(findAll(tree, node => node.props?.role === 'alert').length, 0);
    detail(tree).props.onCancelPaid(); tree = render();
    assert.equal(detail(tree).props.paidPrompt, false);
    detail(tree).props.onDownload({ confirm_paid: true }); await tick(); tree = render();
    assert.deepEqual(downloads, [{}, { confirm_paid: true }]);
    assert.equal(detail(tree).props.source.status, 'queued');
    assert.equal(detail(tree).props.paidPrompt, false);
  }, {
    capabilities: async () => capability, sources: async () => ({ items: [douyin()] }), tikhubStatus: async () => tikhubStatus(),
    download: async (id, options) => { downloads.push(options); if (!options.confirm_paid) throw confirmationRequired(); return douyin({ status: 'queued', download_id: 'attempt1' }); },
  });
});
