/** Saved links and explicit downloads are separate operations. No provider keys.
 *  The TikHub token lives in the local backend only; this client never sees or sends it. */
const BASE = '/api/network-media';

export interface NetworkSource {
  id: string; url: string; platform: string; title: string; author: string;
  tags: string[]; note: string; starred: boolean; archived: boolean;
  status: string; progress: { downloaded_bytes: number; total_bytes?: number | null } | null;
  error: { code: string; message: string } | null; has_media: boolean;
  download_id: string | null;
  size_bytes: number | null; duration_ms: number | null; filename: string | null; content_type: string | null;
  media_version?: string | null;
  provider?: string | null; platform_item_id?: string | null; supported?: boolean;
  created_at: string; updated_at: string; revision: number;
}
export interface TikHubUsage { requests: number; succeeded: number; failed: number; paid_attempts: number; billable: number; estimated_cost_usd: number; }
export interface TikHubMethodStats { attempts: number; ready: number; needs_login: number; unsupported: number; failed: number; other: number; }
export interface TikHubCheck {
  at: string; mode: 'demo' | 'account'; ok: boolean; code: string | null; message: string | null;
  sample?: { item_id: string; title: string; author: string; duration_ms: number | null; downloadable: boolean; candidates: number; codec: string | null };
  account?: { balance_usd: number | null; free_credit_usd: number | null; email_verified: boolean; account_active: boolean; key_active: boolean; key_expires_at: string | null; missing_scopes: string[] };
}
export interface TikHubStatus {
  available: boolean; configured: boolean; endpoint: string | null; endpoint_valid: boolean; cookie_access: boolean; page_size: number;
  prices: { per_paid_request_usd: number; checked_on: string; free: string[] };
  usage: { today: TikHubUsage; total: TikHubUsage; daily_limit: number; remaining_today: number };
  last_check: TikHubCheck | null;
  comparison: { methods: { tikhub: TikHubMethodStats; anonymous: TikHubMethodStats }; sources: { resolved: number; downloaded: number; needs_permission: number } };
}
export interface TikHubPreviewItem {
  item_id: string; title: string; author: string; duration_ms: number | null; published_at: string | null; kind: string;
  downloadable: boolean; reason: string | null; reason_message: string | null; cover_url: string | null;
  in_library: boolean; source_id: string | null; has_media: boolean;
}
export interface TikHubPreview {
  preview_id: string; kind: 'video' | 'creator'; creator: { sec_user_id: string; nickname: string; url: string } | null;
  items: TikHubPreviewItem[]; has_more: boolean; next_cursor: number | null; requests_used: number; estimated_cost_usd: number;
  usage: TikHubStatus['usage'];
}
export interface DownloadOptions { confirm_paid?: boolean; force?: boolean; method?: 'auto' | 'tikhub' | 'anonymous'; }
export interface NetworkCapabilities {
  max_batch: number; downloader_ready: boolean; ffmpeg_available?: boolean; session_token: string;
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
  invalid_source: '没有找到可用的链接，请粘贴完整的分享文字或网址。',
  already_downloaded: '这个作品已下载到本机，没有重复请求或下载。',
  duplicate_item: '资料库中已有这个作品的本地副本，没有重复下载。',
  tikhub_unavailable: '当前后端没有启用 TikHub 数据源。',
  tikhub_not_available: '这条资料不能通过 TikHub 下载。',
  tikhub_not_configured: '尚未配置 TikHub Token。请在本机 backend/.env 设置 TIKHUB_API_KEY 后重启后端。',
  tikhub_invalid_endpoint: 'TikHub 服务地址配置无效，只允许官方 HTTPS 地址。',
  tikhub_confirmation_required: '这一步会向 TikHub 发送付费请求，需要先确认。',
  tikhub_budget_exhausted: '已达到本机设置的 TikHub 每日请求上限，没有发送请求。',
  tikhub_preview_expired: '预览已过期，请重新解析后再选择。',
  tikhub_unauthorized: 'TikHub 拒绝了当前 Token（无效、过期或已停用），请检查本机配置。',
  tikhub_payment_required: 'TikHub 账户余额或免费额度不足，本次没有取得数据。',
  tikhub_forbidden: '当前 TikHub Token 没有此接口的权限，或账户尚未完成邮箱验证。',
  tikhub_rate_limited: 'TikHub 请求过于频繁，请稍后重试。',
  tikhub_bad_request: 'TikHub 无法识别这个链接或作品，请检查后重试。',
  tikhub_network_error: '无法连接 TikHub，请检查本机网络或代理后重试。',
  tikhub_invalid_response: 'TikHub 返回了无法识别的数据，没有保存任何内容。',
  private_content: '作者已将该作品设为私密，需要登录或权限才能查看。',
  restricted_content: '该作品仅部分用户可见，需要登录或权限才能查看。',
  content_unavailable: '作品不存在、已删除，或因地区与版权限制无法取得。',
  not_douyin_video: '请粘贴一条抖音视频分享文字或链接。',
  not_douyin_creator: '请粘贴一位抖音博主的主页链接。',
  single_link_only: '一次只解析一条链接，请逐条粘贴，或使用“保存链接”批量收藏。',
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
    download(id: string, options: DownloadOptions = {}) { return request<NetworkSource>(`/sources/${encodeURIComponent(id)}/download`, json(options)); },
    tikhubStatus(signal?: AbortSignal) { return request<TikHubStatus>('/tikhub/status', { signal }); },
    tikhubCheck(mode: 'demo' | 'account') { return request<TikHubCheck & { status: TikHubStatus }>('/tikhub/check', json({ mode })); },
    /** Paid lookups are sent only with the user's confirmation for this one action. */
    async tikhubResolve(text: string, confirmPaid: boolean) {
      const error = inspectTikHubText(text);
      if (error) throw new NetworkMediaError('invalid_text', error, 0);
      return request<TikHubPreview>('/tikhub/resolve', json({ text: text.trim(), confirm_paid: confirmPaid }));
    },
    async tikhubCreator(text: string, cursor: number, confirmPaid: boolean) {
      const error = inspectTikHubText(text);
      if (error) throw new NetworkMediaError('invalid_text', error, 0);
      return request<TikHubPreview>('/tikhub/creator', json({ text: text.trim(), cursor, confirm_paid: confirmPaid }));
    },
    async tikhubImport(previewId: string, itemIds: string[]) {
      if (itemIds.length < 1 || itemIds.length > 20) throw new NetworkMediaError('invalid_batch', '请勾选 1–20 个作品。', 0);
      return request<{ items: NetworkSource[]; created: number; duplicates: number }>('/tikhub/import', json({ preview_id: previewId, item_ids: itemIds }));
    },
    cancel(id: string) { return request<NetworkSource>(`/sources/${encodeURIComponent(id)}/cancel`, json({})); },
  };
}
export const networkMediaApi = createNetworkMediaClient();
export function inspectShareText(text: string) {
  if (!text.trim()) return '请先粘贴一段分享文字或视频链接。';
  if ([...text].length > 20000) return '分享文字过长，请分批保存，每批不超过 20,000 字。';
  return '';
}
export function inspectTikHubText(text: string) {
  if (!text.trim()) return '请先粘贴一条抖音分享文字或链接。';
  if ([...text.trim()].length > 2000) return '内容过长，请只粘贴一条分享文字或链接。';
  return '';
}
/** Paid requests a lookup needs; a short profile link costs one extra lookup to find the creator. */
export function tikhubLookupCost(mode: 'video' | 'creator', text: string) {
  return mode === 'creator' && !/douyin\.com\/(?:share\/)?user\//i.test(text) ? 2 : 1;
}
export function costLabel(requests: number, unitPrice: number) { return `$${(requests * unitPrice).toFixed(3)}`; }
export function durationLabel(milliseconds: number | null | undefined) {
  if (!milliseconds || milliseconds < 0) return '';
  const seconds = Math.round(milliseconds / 1000);
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}
/** Covers come from a third-party image host; only plain https addresses are ever rendered. */
export function safeCoverUrl(value: string | null | undefined): string | undefined {
  try { const url = new URL(value ?? ''); return url.protocol === 'https:' && !url.username && !url.password ? url.href : undefined; }
  catch { return undefined; }
}
export type TikHubState = 'unavailable' | 'unconfigured' | 'error' | 'ready' | 'configured';
export function tikhubState(status: TikHubStatus | null): TikHubState {
  if (!status?.available) return 'unavailable';
  if (!status.endpoint_valid) return 'error';
  if (!status.configured) return 'unconfigured';
  if (status.last_check?.mode === 'account') return status.last_check.ok ? 'ready' : 'error';
  return 'configured';
}
export const TIKHUB_STATE_LABELS: Record<TikHubState, string> = {
  unavailable: 'TikHub 未启用', unconfigured: 'TikHub 未配置 Token', error: 'TikHub 连接异常', ready: 'TikHub 已连接', configured: 'TikHub 已配置 · 尚未检查',
};
/** Douyin sources use TikHub once it is configured; a finished copy is never requested again. */
export function usesTikHub(source: NetworkSource, status: TikHubStatus | null) {
  return Boolean(status?.available && status.configured && source.platform === 'douyin' && source.supported !== false);
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
