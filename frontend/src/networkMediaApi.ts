/** Saved links and explicit downloads are separate operations. No provider keys. */
const BASE = '/api/network-media';

export interface NetworkSource {
  id: string; url: string; platform: string; title: string; author: string;
  tags: string[]; note: string; starred: boolean; archived: boolean;
  status: string; progress: { downloaded_bytes: number; total_bytes?: number | null } | null;
  error: { code: string; message: string } | null; has_media: boolean;
  download_id: string | null;
  size_bytes: number | null; duration_ms: number | null; filename: string | null; content_type: string | null;
  media_version?: string | null;
  created_at: string; updated_at: string; revision: number;
}
export interface NetworkCapabilities {
  max_batch: number; downloader_ready: boolean; session_token: string;
  platforms: { id: string; label: string; download_verified: boolean }[];
  limits: { media_bytes: number; duration_seconds: number; download_seconds: number; max_height: number };
}
export type SourcePatch = Partial<Pick<NetworkSource, 'title' | 'tags' | 'note' | 'starred' | 'archived'>> & { expected_revision: number };
export type LibraryView = 'all' | 'starred' | 'archived' | 'downloads';
const MESSAGES: Record<string, string> = {
  invalid_token: '本机服务已重新启动，请点击“重新连接”后再试。',
  local_only: '请在这台电脑的本地页面使用资料库。',
  no_urls: '没有找到可保存的链接，请粘贴视频分享文字或完整网址。',
  invalid_batch: '分享文字过长或没有找到有效链接，请检查后分批保存。',
  too_many_urls: '每次最多保存 20 条链接，请分批添加。',
  too_many_sources: '每次最多保存 20 条链接，请分批添加。',
  invalid_url: '链接格式不正确，请使用完整的 http 或 https 网址。',
  unsupported_url: '这个链接暂时无法识别，请检查视频原始地址。',
  invalid_patch: '资料内容不符合要求，请检查标题、标签和备注。',
  empty_title: '请输入资料标题，或保留原来的链接标题。',
  source_conflict: '这条资料已在其他页面更新。你的草稿已保留，请比较最新内容后合并保存。',
  source_not_found: '这条资料已不存在，请刷新列表。',
  not_found: '这条资料已不存在，请刷新列表。',
  download_busy: '这条资料正在下载或排队，请等待或取消后再试。',
  already_downloading: '这条资料正在下载或排队，请等待或取消后再试。',
  downloader_unavailable: '本机下载工具尚未准备好，请检查启动环境后重新连接。',
  downloader_not_ready: '本机下载工具尚未准备好，请检查启动环境后重新连接。',
  needs_login: '原站要求登录或验证。已保留链接，本工具不会获取账号或 Cookie。',
  no_media: '这条资料还没有可用的本地文件。',
  unsafe_url: '该地址不适合通过本机下载服务访问，请使用公开的视频地址。',
};
export class NetworkMediaError extends Error {
  constructor(public code: string, message: string, public status: number) { super(message); }
}
export function networkErrorMessage(error: unknown) {
  return error instanceof NetworkMediaError ? error.message : '无法连接本机服务，请检查启动窗口后重新连接。';
}

export function createNetworkMediaClient(fetcher: typeof fetch = (...args) => fetch(...args)) {
  let sessionToken = '';
  async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
    const writes = Boolean(options.method && options.method !== 'GET');
    if (writes && !sessionToken) throw new NetworkMediaError('not_connected', '请先连接本机资料库。', 0);
    const headers = new Headers(options.headers);
    if (writes) headers.set('X-Local-Media-Token', sessionToken);
    const response = await fetcher(`${BASE}${path}`, { ...options, headers, credentials: 'same-origin' });
    if (!response.ok) {
      let code = '';
      try { const body = await response.json(); code = typeof body?.detail?.code === 'string' ? body.detail.code : ''; }
      catch { /* Do not expose raw downloader, webpage or server output. */ }
      const fallback = response.status === 403 ? '本机连接验证失败，请重新连接后重试。'
        : response.status === 409 ? '当前操作发生冲突，请刷新后再试；你的编辑仍保留。'
          : response.status === 422 || response.status === 400 ? '无法保存这些内容，请检查链接数量、标题、标签和备注。'
            : '操作未完成，请稍后再试。已保存的链接不会丢失。';
      throw new NetworkMediaError(code, Object.prototype.hasOwnProperty.call(MESSAGES, code) ? MESSAGES[code] : fallback, response.status);
    }
    return response.json() as Promise<T>;
  }
  const json = (value: unknown, method = 'POST'): RequestInit => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value) });
  return {
    async capabilities(signal?: AbortSignal) {
      const result = await request<NetworkCapabilities>('/capabilities', { signal });
      sessionToken = result.session_token;
      return result;
    },
    sources(signal?: AbortSignal) { return request<{ items: NetworkSource[] }>('/sources', { signal }); },
    async collect(text: string) {
      const error = inspectShareText(text);
      if (error) throw new NetworkMediaError('invalid_text', error, 0);
      return request<{ items: NetworkSource[]; created: number; duplicates: number }>('/sources', json({ text }));
    },
    async update(id: string, patch: SourcePatch) {
      const error = inspectSourcePatch(patch);
      if (error) throw new NetworkMediaError('invalid_patch', error, 0);
      return request<NetworkSource>(`/sources/${encodeURIComponent(id)}`, json(patch, 'PATCH'));
    },
    download(id: string) { return request<NetworkSource>(`/sources/${encodeURIComponent(id)}/download`, json({})); },
    cancel(id: string) { return request<NetworkSource>(`/sources/${encodeURIComponent(id)}/cancel`, json({})); },
  };
}
export const networkMediaApi = createNetworkMediaClient();
export function inspectShareText(text: string) {
  if (!text.trim()) return '请先粘贴一段分享文字或视频链接。';
  if ([...text].length > 20000) return '分享文字过长，请分批保存，每批不超过 20,000 字。';
  return '';
}
export function parseTags(text: string) {
  return [...new Set(text.split(/[,，\n]/).map(tag => tag.trim()).filter(Boolean))];
}
export function inspectSourcePatch(patch: SourcePatch) {
  if (!Number.isInteger(patch.expected_revision) || patch.expected_revision < 0) return '资料版本缺失，请重新读取后保存。';
  if (patch.title != null && !patch.title.trim()) return '请输入资料标题，或保留原来的链接标题。';
  if (patch.title != null && [...patch.title].length > 256) return '标题请控制在 256 字以内。';
  if (patch.note != null && [...patch.note].length > 5000) return '备注请控制在 5,000 字以内。';
  if (patch.tags != null && (patch.tags.length > 20 || patch.tags.some(tag => !tag.trim() || [...tag].length > 40))) return '最多 20 个标签，每个标签不超过 40 字。';
  return '';
}
export function filterSources(sources: NetworkSource[], view: LibraryView, platform: string, query: string) {
  const search = query.trim().toLocaleLowerCase();
  return sources.filter(source => {
    if (view === 'archived' ? !source.archived : view !== 'downloads' && source.archived) return false;
    if (view === 'starred' && !source.starred) return false;
    if (view === 'downloads' && !hasDownloadRecord(source)) return false;
    if (platform !== 'all' && source.platform !== platform) return false;
    return !search || [source.title, source.url, source.note, source.author, ...source.tags].join('\n').toLocaleLowerCase().includes(search);
  });
}
export function downloadActive(source: NetworkSource) { return ['queued', 'downloading', 'cancel_requested'].includes(source.status); }
export function hasDownloadRecord(source: NetworkSource) { return Boolean(source.download_id); }
export function networkMediaUrl(id: string) { return `${BASE}/sources/${encodeURIComponent(id)}/media`; }
export function downloadFileUrl(id: string) { return `${BASE}/sources/${encodeURIComponent(id)}/download-file`; }
export function safeSourceUrl(value: string): string | undefined {
  try { const url = new URL(value); return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : undefined; }
  catch { return undefined; }
}
export const PLATFORM_LABELS: Record<string, string> = { douyin: '抖音', bilibili: 'Bilibili', youtube: 'YouTube', direct: '媒体直链', unknown: '其他链接' };
export const DOWNLOAD_LABELS: Record<string, string> = {
  saved: '仅保存链接', queued: '排队中', downloading: '下载中', cancel_requested: '正在取消', ready: '已下载到本机',
  failed: '下载失败', needs_login: '原站要求验证', unsupported: '暂不支持下载', cancelled: '已取消下载', interrupted: '下载已中断',
};
export function sizeLabel(size: number) {
  if (size >= 1024 ** 3) return `${(size / 1024 ** 3).toFixed(1)} GiB`;
  if (size >= 1024 ** 2) return `${(size / 1024 ** 2).toFixed(1)} MiB`;
  return `${Math.max(0, Math.round(size / 1024))} KiB`;
}
