/** Local media requests keep their write token only in this page's memory. */
const BASE = '/api/local-media';

export interface LocalCapabilities {
  ffmpeg_ready: boolean;
  asr_configured: boolean;
  asr_model: string;
  asr_endpoint: string;
  cloud_verified: boolean;
  session_token: string;
  limits: { media_bytes: number; subtitle_bytes: number; duration_seconds: number };
}
export type LocalMode = 'subtitles' | 'qwen';
export interface LocalJob {
  id: string;
  asset_id: string | null;
  filename?: string;
  mode?: LocalMode;
  status: string;
  stage: string;
  message: string;
  progress: { done: number; total: number } | null;
  error: { code: string; message: string } | null;
  request_count: number;
  transcript_reused?: boolean;
}
export interface LocalAsset {
  id: string;
  title: string;
  filename: string;
  size_bytes: number;
  duration_ms: number | null;
  has_result: boolean;
  origin?: string;
  updated_at: string;
  warnings?: string[];
}
export interface LocalNotes { text: string; revision: string; }
export interface TranscriptSegment { id: string; start_ms: number; end_ms: number; text: string; }
export interface LocalDetail {
  asset: LocalAsset;
  transcript: { segments: TranscriptSegment[]; origin?: string; warnings?: string[] } | null;
  knowledge: string | null;
  notes: LocalNotes;
  job: LocalJob | null;
}
export interface CreateJob {
  media_upload_id: string;
  subtitle_upload_id?: string;
  mode: LocalMode;
  allow_cloud_asr: boolean;
  max_requests: number;
}

const MESSAGES: Record<string, string> = {
  notes_conflict: '笔记已在其他窗口更新。你的草稿仍保留，请先比较最新内容。',
  revision_conflict: '笔记已在其他窗口更新。你的草稿仍保留，请先比较最新内容。',
  worker_busy: '已有任务正在处理，请等待结束后再提交。',
  job_busy: '已有任务正在处理，请等待结束后再提交。',
  asset_busy: '这份资料正在处理，请稍后重试。',
  missing_credentials: '云端识别尚未配置。请在本机配置服务凭证后重试。',
  cloud_asr_not_authorized: '请先确认本次音频上传与可能产生的费用。',
  ffmpeg_not_found: '没有找到 ffmpeg，请完成本机安装后重新连接。',
  unsupported_type: '请选择 MP4、MP3、M4A 或 WAV 文件。',
  invalid_media: '文件无法解码或没有可用音轨，请检查后重试。',
  invalid_subtitles: '字幕无效，请检查 SRT/VTT 格式、编码和时间范围。',
  invalid_duration: '媒体时长超过当前限制或无法读取。',
  file_too_large: '文件超过当前大小限制，请选择较小的文件。',
  upload_too_large: '文件超过当前大小限制，请选择较小的文件。',
  request_budget_exhausted: '已达到本次请求次数上限。已完成的识别已保留。',
  invalid_token: '本机连接凭证已更新，请点击“重新连接”后再次操作。',
  invalid_session_token: '本机连接凭证已更新，请点击“重新连接”后再次操作。',
  asset_not_found: '这份本地资料已不存在，请刷新资料列表。',
  job_not_found: '此任务已不存在，请刷新任务列表。',
  upload_not_found: '暂存文件已不存在，请重新选择文件。',
  invalid_upload: '暂存文件已变化或丢失，请重新选择文件。',
  missing_subtitles: '已有字幕模式需要一份 SRT 或 VTT 文件。',
  notes_too_large: '人工笔记不得超过 1 MiB，请缩短后保存。',
  not_found: '记录已不存在，请刷新本地资料列表。',
  no_result: '此媒体尚未生成完整结果，请先完成处理。',
  empty_upload: '文件为空，请重新选择媒体或字幕。',
  invalid_record: '本地记录暂时无法读取，请保留资料并检查本机存储。',
  no_speech: '没有检测到可用语音，请确认视频音轨。',
};

export class LocalMediaError extends Error {
  constructor(public code: string, message: string, public status: number) { super(message); }
}

export function localErrorMessage(error: unknown): string {
  return error instanceof LocalMediaError ? error.message : '无法连接本机服务，请检查启动窗口后重试。';
}

export function createLocalMediaClient(fetcher: typeof fetch = (...args) => fetch(...args)) {
  let sessionToken = '';
  async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
    const writes = options.method != null && options.method !== 'GET';
    if (writes && !sessionToken) throw new LocalMediaError('not_connected', '请先连接本机服务，再开始操作。', 0);
    const headers = new Headers(options.headers);
    if (writes) headers.set('X-Local-Media-Token', sessionToken);
    const response = await fetcher(`${BASE}${path}`, { ...options, headers, credentials: 'same-origin' });
    if (!response.ok) {
      let code = '';
      try {
        const body = await response.json();
        code = typeof body?.detail?.code === 'string' ? body.detail.code : typeof body?.code === 'string' ? body.code : '';
      } catch { /* Never display raw server bodies or provider errors. */ }
      const fallback = response.status === 409 ? '操作冲突，请刷新后重试；你的笔记草稿仍保留。'
        : response.status === 403 ? '本机连接验证失败，请重新连接后重试。'
        : response.status === 413 ? '文件超过当前大小限制，请选择较小的文件。'
        : response.status === 422 ? '提交内容不符合要求，请检查文件和选项。'
        : '操作未完成，请稍后重试。已保存的资料不会因此丢失。';
      throw new LocalMediaError(code, Object.prototype.hasOwnProperty.call(MESSAGES, code) ? MESSAGES[code] : fallback, response.status);
    }
    return response.json() as Promise<T>;
  }
  const json = (value: unknown): RequestInit => ({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value) });
  function checkBudget(value: number) {
    if (!Number.isInteger(value) || value < 1 || value > 100) {
      throw new LocalMediaError('invalid_budget', '请求次数上限需为 1–100 的整数。', 0);
    }
  }
  return {
    async capabilities(signal?: AbortSignal) {
      const result = await request<LocalCapabilities>('/capabilities', { signal });
      sessionToken = result.session_token;
      return result;
    },
    upload(file: File, kind: 'media' | 'subtitles', signal?: AbortSignal) {
      return request<{ upload_id: string; filename: string; size_bytes: number }>(
        `/uploads?${new URLSearchParams({ filename: file.name, kind })}`,
        { method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file, signal });
    },
    async createJob(value: CreateJob) {
      checkBudget(value.max_requests);
      if (value.mode === 'qwen' && !value.allow_cloud_asr) {
        throw new LocalMediaError('cloud_asr_not_authorized', MESSAGES.cloud_asr_not_authorized, 0);
      }
      return request<LocalJob>('/jobs', json(value));
    },
    jobs(signal?: AbortSignal) { return request<{ items: LocalJob[] }>('/jobs', { signal }); },
    assets(signal?: AbortSignal) { return request<{ items: LocalAsset[] }>('/assets', { signal }); },
    detail(id: string, signal?: AbortSignal) { return request<LocalDetail>(`/assets/${encodeURIComponent(id)}`, { signal }); },
    cancel(id: string) { return request<LocalJob>(`/jobs/${encodeURIComponent(id)}/cancel`, json({})); },
    async retry(id: string, allow_cloud_asr: boolean, max_requests: number) {
      checkBudget(max_requests);
      return request<LocalJob>(`/jobs/${encodeURIComponent(id)}/retry`, json({ allow_cloud_asr, max_requests }));
    },
    saveNotes(id: string, notes: LocalNotes) {
      return request<LocalNotes>(`/assets/${encodeURIComponent(id)}/notes`, { ...json(notes), method: 'PUT' });
    },
  };
}

export const localMediaApi = createLocalMediaClient();
export const EXPORT_FILES = ['source_manifest.json', 'transcript.json', 'transcript.md', 'knowledge.md', 'notes.md'] as const;
export function mediaUrl(id: string) { return `${BASE}/assets/${encodeURIComponent(id)}/media`; }
export function exportUrl(id: string, filename: typeof EXPORT_FILES[number]) {
  if (!EXPORT_FILES.includes(filename)) throw new Error('不支持的导出文件');
  return `${BASE}/assets/${encodeURIComponent(id)}/export/${filename}`;
}
export function isActiveJob(job: LocalJob) { return ['queued', 'running', 'cancel_requested'].includes(job.status); }
export function formatTimestamp(ms: number) {
  const safe = Math.max(0, Math.round(ms));
  const hours = Math.floor(safe / 3600000);
  const minutes = Math.floor(safe / 60000) % 60;
  const seconds = Math.floor(safe / 1000) % 60;
  return `${hours ? `${hours}:` : ''}${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}.${String(safe % 1000).padStart(3, '0')}`;
}
export function formatBytes(size: number) {
  return size >= 1024 ** 3 ? `${(size / 1024 ** 3).toFixed(1)} GiB` : `${(size / 1024 ** 2).toFixed(1)} MiB`;
}
export function inspectLocalImport(media: File | null, subtitles: File | null, mode: LocalMode,
  consent: boolean, maxRequests: number, capabilities: LocalCapabilities | null): string {
  if (!capabilities) return '请先连接本机服务。';
  if (!capabilities.ffmpeg_ready) return '没有找到 ffmpeg，请完成本机安装后重新连接。';
  if (!media) return '请选择一个本地视频或音频文件。';
  if (!/\.(mp4|mp3|m4a|wav)$/i.test(media.name)) return '支持 MP4、MP3、M4A、WAV 文件。';
  if (!media.size || media.size > capabilities.limits.media_bytes) return `媒体需非空，且不超过 ${formatBytes(capabilities.limits.media_bytes)}。`;
  if (mode === 'subtitles') {
    if (!subtitles) return '已有字幕模式需要一份 SRT 或 VTT 文件。';
    if (!/\.(srt|vtt)$/i.test(subtitles.name) || !subtitles.size || subtitles.size > capabilities.limits.subtitle_bytes) {
      return `请选择非空的 SRT/VTT 字幕，大小不超过 ${formatBytes(capabilities.limits.subtitle_bytes)}。`;
    }
  } else {
    if (!capabilities.asr_configured) return '云端识别尚未配置，请先完成本机服务配置。';
    if (!consent) return '请先勾选本次云端音频上传与费用授权。';
    if (!Number.isInteger(maxRequests) || maxRequests < 1 || maxRequests > 100) return '请求次数上限需为 1–100 的整数。';
  }
  return '';
}
