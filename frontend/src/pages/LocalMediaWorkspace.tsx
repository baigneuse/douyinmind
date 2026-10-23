import { useCallback, useDeferredValue, useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import { ArrowLeft, AudioLines, Check, Download, FileAudio, FileText, FolderOpen, LoaderCircle, Play, RefreshCw, Save, Search, Upload, X } from 'lucide-react';
import { EXPORT_FILES, LocalMediaError, exportUrl, formatBytes, formatTimestamp, inspectLocalImport, isActiveJob, localErrorMessage, localMediaApi as api, mediaUrl } from '../localMediaApi';
import type { LocalAsset, LocalCapabilities, LocalDetail, LocalJob, LocalMode, LocalNotes } from '../localMediaApi';
import './localMedia.css';

const STATUS: Record<string, string> = {
  queued: '等待处理', running: '处理中', cancel_requested: '正在停止后续处理', cancelled: '已取消',
  interrupted: '已中断', failed: '处理失败', blocked: '需要处理后重试', completed: '已完成', no_speech: '未检测到语音',
};
const STAGE: Record<string, string> = {
  queued: '排队', validation: '校验文件', normalization: '提取音轨', transcription: '声音转文字',
  export: '保存结果', completed: '结果已保存', upload: '保存到本机',
};
const ORIGIN: Record<string, string> = { subtitles: '已有字幕导入', qwen_asr: '云端声音识别', qwen: '云端声音识别', mock: '测试替身结果' };
const EXPORT_LABELS: Record<string, string> = {
  'source_manifest.json': '来源记录 JSON', 'transcript.json': '逐字稿 JSON', 'transcript.md': '逐字稿 Markdown',
  'knowledge.md': '知识笔记 Markdown', 'notes.md': '人工笔记 Markdown',
};
interface NotesDraft extends LocalNotes { savedText: string; conflict?: LocalNotes; saving?: boolean; message?: string; }
interface Props { onExit: () => void; onDirtyChange?: (dirty: boolean) => void; }

function CloudOptions({ capabilities, allowed, onAllowed, budget, onBudget, disabled = false, prefix }: {
  capabilities: LocalCapabilities | null; allowed: boolean; onAllowed: (value: boolean) => void;
  budget: number; onBudget: (value: number) => void; disabled?: boolean; prefix: string;
}) {
  return <div className="lm-cloud">
    <p><strong>仅将提取的音频发往云端</strong>，原视频保留在本机。服务可能产生费用。</p>
    <dl className="lm-provider"><dt>模型</dt><dd>{capabilities?.asr_model || '等待服务配置'}</dd><dt>服务地址</dt><dd>{capabilities?.asr_endpoint || '尚未连接'}</dd></dl>
    {!capabilities?.asr_configured && <p className="lm-warning">尚未配置服务凭证，请在本机配置后点击“重新连接”。</p>}
    {!capabilities?.cloud_verified && <p className="lm-small">真实云端识别尚未完成验收；结果需要人工校对。</p>}
    <label className="lm-field" htmlFor={`${prefix}-budget`}>本次最多允许的请求次数
      <input id={`${prefix}-budget`} type="number" min={1} max={100} step={1} value={Number.isNaN(budget) ? '' : budget} onChange={event => onBudget(event.target.valueAsNumber)} disabled={disabled} />
    </label>
    <p className="lm-small">范围 1–100，包含分段和重试。请求次数不是费用上限；不足时会保留已完成分段。</p>
    <label className="lm-check"><input type="checkbox" checked={allowed} onChange={event => onAllowed(event.target.checked)} disabled={disabled} /><span>我同意本次将音频发送至上述服务，并承担可能产生的费用。</span></label>
  </div>;
}

function JobCard({ job, capabilities, busy, onCancel, onRetry, onOpen }: {
  job: LocalJob; capabilities: LocalCapabilities | null; busy: boolean;
  onCancel: (job: LocalJob) => Promise<void>; onRetry: (job: LocalJob, consent: boolean, budget: number) => Promise<void>;
  onOpen: (id: string) => void;
}) {
  const [retrying, setRetrying] = useState(false);
  const [consent, setConsent] = useState(false);
  const [budget, setBudget] = useState(8);
  const active = isActiveJob(job);
  const cloud = job.mode === 'qwen';
  const canRetry = ['cancelled', 'interrupted', 'failed', 'blocked', 'no_speech'].includes(job.status);
  const progress = job.progress && job.progress.total > 0 ? job.progress : null;
  return <article className={`lm-job ${active ? 'lm-job-active' : ''}`}>
    <div className="lm-job-heading"><div><strong>{job.filename || '本地媒体任务'}</strong><p>{STATUS[job.status] || '状态待确认'}{job.stage ? ` · ${STAGE[job.stage] || '处理阶段'}` : ''}</p></div>
      {active ? <LoaderCircle size={18} className="lm-spin" aria-hidden="true" /> : job.status === 'completed' ? <Check size={18} aria-label="已完成" /> : null}
    </div>
    {progress && <div className="lm-progress"><progress value={progress.done} max={progress.total} aria-label="已完成识别分段" /><span>已完成 {progress.done} / {progress.total} 段</span></div>}
    {job.message && <p className="lm-small">{job.message}</p>}
    {job.error && <p className="lm-warning">{job.error.message}</p>}
    <p className="lm-small">已发起 {job.request_count ?? 0} 次云端请求{job.transcript_reused ? ' · 已复用逐字稿，没有重新识别' : ''}</p>
    {job.status === 'cancel_requested' && <p className="lm-small">正在停止后续请求。已发出的云端请求可能仍会完成并计费。</p>}
    <div className="lm-actions">
      {job.asset_id && <button type="button" className="lm-button" onClick={() => onOpen(job.asset_id!)}>查看资料</button>}
      {active && <button type="button" className="lm-button" disabled={busy || job.status === 'cancel_requested'} onClick={() => { void onCancel(job); }}><X size={14} />停止后续处理</button>}
      {canRetry && <button type="button" className="lm-button" disabled={busy} onClick={() => { setRetrying(value => !value); setConsent(false); }}><RefreshCw size={14} />{retrying ? '收起重试' : '重试'}</button>}
    </div>
    {retrying && <form className="lm-retry" onSubmit={event => { event.preventDefault(); void onRetry(job, consent, budget).then(() => { setRetrying(false); setConsent(false); }).catch(() => { /* The workspace shows the error and preserves these options. */ }); }}>
      <p className="lm-small">复用已完成的处理结果，保留人工笔记。中断任务不会自动重启。</p>
      {cloud && <CloudOptions prefix={`retry-${job.id}`} capabilities={capabilities} allowed={consent} onAllowed={setConsent} budget={budget} onBudget={setBudget} disabled={busy} />}
      {!job.mode && <p className="lm-warning">此任务缺少处理模式，暂不能安全重试，请重新选择文件。</p>}
      <button className="lm-button lm-primary" disabled={busy || !job.mode || (cloud && (!consent || !capabilities?.asr_configured || !Number.isInteger(budget) || budget < 1 || budget > 100))}>确认重试</button>
    </form>}
  </article>;
}

function AssetResult({ detail, draft, onDraft, onSave }: {
  detail: LocalDetail; draft: NotesDraft | undefined; onDraft: (value: string) => void;
  onSave: (revision?: string) => void;
}) {
  const [tab, setTab] = useState<'transcript' | 'knowledge' | 'notes'>('transcript');
  const [query, setQuery] = useState('');
  const [currentMs, setCurrentMs] = useState(0);
  const [mediaError, setMediaError] = useState('');
  const media = useRef<HTMLMediaElement | null>(null);
  const deferredQuery = useDeferredValue(query.trim().toLocaleLowerCase());
  const segments = detail.transcript?.segments ?? [];
  const visible = deferredQuery ? segments.filter(segment => segment.text.toLocaleLowerCase().includes(deferredQuery)) : segments;
  const isVideo = /\.mp4$/i.test(detail.asset.filename);
  const origin = detail.transcript?.origin || detail.asset.origin || '';
  const warnings = [...new Set([...(detail.asset.warnings ?? []), ...(detail.transcript?.warnings ?? [])])];
  const seek = (start: number) => {
    if (!media.current) return;
    try {
      media.current.currentTime = start / 1000;
      setCurrentMs(start);
      void media.current.play().catch(() => setMediaError('已跳转到对应时间，请点击播放器的播放按钮。'));
    } catch { setMediaError('媒体尚未准备好，请先在播放器中载入文件后再跳转。'); }
  };
  const playerProps = {
    controls: true, preload: 'metadata' as const, src: mediaUrl(detail.asset.id),
    onTimeUpdate: () => setCurrentMs((media.current?.currentTime ?? 0) * 1000),
    onError: () => setMediaError('浏览器无法播放此文件或本机媒体暂不可用。可尝试 H.264/AAC 编码的 MP4；已保存的文字仍可查看和导出。'),
    onLoadedMetadata: () => setMediaError(''),
  };
  return <section className="lm-result" aria-labelledby="local-result-title">
    <div className="lm-result-heading"><div><p className="lm-eyebrow">本地资料</p><h2 id="local-result-title">{detail.asset.title || detail.asset.filename}</h2><p className="lm-small">{detail.asset.filename} · {formatBytes(detail.asset.size_bytes)}{detail.asset.duration_ms != null ? ` · ${formatTimestamp(detail.asset.duration_ms)}` : ''}</p></div><span className="lm-badge">{ORIGIN[origin] || '等待处理'}</span></div>
    <div className={`lm-player ${isVideo ? '' : 'lm-player-audio'}`}>
      {!isVideo && <div className="lm-audio-art"><AudioLines size={38} /><span>音频资料</span></div>}
      {isVideo ? <video ref={element => { media.current = element; }} {...playerProps} aria-label="本地视频播放器" /> : <audio ref={element => { media.current = element; }} {...playerProps} aria-label="本地音频播放器" />}
    </div>
    {mediaError && <p role="status" className="lm-warning">{mediaError}</p>}
    <p className="lm-provenance">{origin === 'subtitles' ? '文字来自你导入的字幕，不是本工具识别结果。' : origin === 'mock' ? '这是测试替身结果，不能作为真实识别验收。' : '识别对象为视频或音频中的声音，不包含画面分析。'} 内容与时间尚未人工校对，请对照播放核实。</p>
    {warnings.length > 0 && <div className="lm-notice">{warnings.map((warning, index) => <p key={index}>{warning}</p>)}</div>}
    {detail.job && detail.job.status !== 'completed' && detail.asset.has_result && <p className="lm-notice">最新一次处理尚未完成。这里仍保留上次成功保存的结果。</p>}
    <div className="lm-tabs" aria-label="资料内容">
      <button type="button" className={tab === 'transcript' ? 'active' : ''} aria-pressed={tab === 'transcript'} onClick={() => setTab('transcript')}>逐字稿 <span>{segments.length}</span></button>
      <button type="button" className={tab === 'knowledge' ? 'active' : ''} aria-pressed={tab === 'knowledge'} onClick={() => setTab('knowledge')}>知识笔记</button>
      <button type="button" className={tab === 'notes' ? 'active' : ''} aria-pressed={tab === 'notes'} onClick={() => setTab('notes')}>人工笔记{draft && draft.text !== draft.savedText ? ' · 未保存' : ''}</button>
    </div>
    {tab === 'transcript' && <div className="lm-transcript">
      <label className="lm-search"><Search size={16} /><span className="lm-sr-only">搜索当前逐字稿</span><input type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索这份逐字稿" /><span>{deferredQuery ? `${visible.length} 段` : '点击时间播放'}</span></label>
      {segments.length === 0 ? <div className="lm-empty-inline"><FileText size={26} /><p>{detail.job?.status === 'no_speech' ? '未检测到可用语音。' : '逐字稿尚未生成。可在上方查看任务进度或重试。'}</p></div>
        : visible.length === 0 ? <p className="lm-empty-inline">没有匹配的文字，请试试其他关键词。</p>
          : <div className="lm-segments">{visible.map(segment => <div className={`lm-segment ${currentMs >= segment.start_ms && currentMs < segment.end_ms ? 'is-current' : ''}`} key={segment.id} id={`local-${segment.id}`}>
            <button type="button" onClick={() => seek(segment.start_ms)} className="lm-time" aria-label={`从 ${formatTimestamp(segment.start_ms)} 播放`}><Play size={12} />{formatTimestamp(segment.start_ms)}<span>— {formatTimestamp(segment.end_ms)}</span></button><p>{segment.text}</p>
          </div>)}</div>}
    </div>}
    {tab === 'knowledge' && <div className="lm-markdown"><p className="lm-notice">本阶段未启用 AI 总结。该文件保留来源和“未生成”状态。</p><ReactMarkdown components={{ img: () => null, a: ({ children, href }) => <a href={href} target="_blank" rel="noopener noreferrer">{children}</a> }}>{detail.knowledge || '知识笔记尚未生成。'}</ReactMarkdown></div>}
    {tab === 'notes' && <div className="lm-notes">
      <p className="lm-small">这是你的独立笔记。重新识别或导入不会覆盖；导出包含最近一次已保存内容。</p>
      <label className="lm-field" htmlFor="local-notes">人工笔记<textarea id="local-notes" rows={12} value={draft?.text ?? ''} onChange={event => onDraft(event.target.value)} placeholder="记录你的理解、疑问和待核对内容…" disabled={!draft} /></label>
      {draft?.conflict && <div className="lm-conflict" role="alert"><strong>笔记已在其他窗口更新，你的草稿仍保留。</strong><p>比较下面的最新内容，再决定如何合并。保存当前草稿将替换最新笔记。</p><details><summary>查看已保存的最新笔记</summary><pre>{draft.conflict.text}</pre></details><button type="button" className="lm-button" disabled={draft.saving} onClick={() => onSave(draft.conflict!.revision)}>已核对，用当前草稿保存</button></div>}
      <div className="lm-actions"><button type="button" className="lm-button lm-primary" disabled={!draft || draft.saving || draft.text === draft.savedText || Boolean(draft.conflict)} onClick={() => onSave()}><Save size={15} />{draft?.saving ? '正在保存…' : '保存笔记'}</button><span role="status" className="lm-small">{draft?.message || (draft && draft.text !== draft.savedText ? '有未保存的修改' : '已保存到本机')}</span></div>
    </div>}
    <details className="lm-exports"><summary><Download size={15} />导出文件</summary><div>{EXPORT_FILES.map(filename => <a href={exportUrl(detail.asset.id, filename)} key={filename} download><Download size={13} />{EXPORT_LABELS[filename]}</a>)}</div><p className="lm-small">仅下载已生成的文件；尚未完成的逐字稿可能不可用。</p></details>
  </section>;
}

export default function LocalMediaWorkspace({ onExit, onDirtyChange }: Props) {
  const [capabilities, setCapabilities] = useState<LocalCapabilities | null>(null);
  const [connectionError, setConnectionError] = useState('');
  const [refreshKey, setRefreshKey] = useState(0);
  const [assets, setAssets] = useState<LocalAsset[]>([]);
  const [jobs, setJobs] = useState<LocalJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState('');
  const [actionError, setActionError] = useState('');
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<LocalDetail | null>(null);
  const [detailError, setDetailError] = useState('');
  const [drafts, setDrafts] = useState<Record<string, NotesDraft>>({});
  const [media, setMedia] = useState<File | null>(null);
  const [subtitles, setSubtitles] = useState<File | null>(null);
  const [mode, setMode] = useState<LocalMode>('subtitles');
  const [consent, setConsent] = useState(false);
  const [budget, setBudget] = useState(8);
  const [submitting, setSubmitting] = useState(false);
  const [uploadStatus, setUploadStatus] = useState('');
  const [actingJob, setActingJob] = useState<string | null>(null);
  const [fileKey, setFileKey] = useState(0);
  const pendingSelection = useRef<string | null>(null);
  const uploadController = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const selectedAsset = assets.find(asset => asset.id === selectedId);
  const selectedJobStatus = jobs.find(job => job.asset_id === selectedId)?.status;
  const dirty = Object.values(drafts).some(draft => draft.text !== draft.savedText);
  const active = jobs.some(isActiveJob);

  useEffect(() => { mounted.current = true; return () => { mounted.current = false; uploadController.current?.abort(); }; }, []);
  useEffect(() => { onDirtyChange?.(dirty); return () => onDirtyChange?.(false); }, [dirty, onDirtyChange]);
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [dirty]);

  useEffect(() => {
    const controller = new AbortController();
    setConnectionError('');
    void api.capabilities(controller.signal).then(value => { if (!controller.signal.aborted) setCapabilities(value); }).catch(error => {
      if (!controller.signal.aborted) { setCapabilities(null); setConnectionError(localErrorMessage(error)); }
    });
    return () => controller.abort();
  }, [refreshKey]);

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      let fast = false;
      try {
        const [jobList, assetList] = await Promise.all([api.jobs(controller.signal), api.assets(controller.signal)]);
        if (controller.signal.aborted) return;
        setJobs(jobList.items); setAssets(assetList.items); setListError('');
        fast = jobList.items.some(isActiveJob);
        const submitted = jobList.items.find(job => job.id === pendingSelection.current);
        if (submitted?.asset_id) { setSelectedId(submitted.asset_id); pendingSelection.current = null; }
        else setSelectedId(previous => previous ?? assetList.items[0]?.id ?? null);
      } catch (error) { if (!controller.signal.aborted) setListError(localErrorMessage(error)); }
      finally { if (!controller.signal.aborted) { setLoading(false); timer = setTimeout(() => { void poll(); }, fast ? 2000 : 7000); } }
    };
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [refreshKey]);

  useEffect(() => {
    if (!selectedId) { setDetail(null); return; }
    const controller = new AbortController();
    setDetailError('');
    setDetail(previous => previous?.asset.id === selectedId ? previous : null);
    void api.detail(selectedId, controller.signal).then(value => {
      if (controller.signal.aborted) return;
      setDetail(value);
      setDrafts(previous => previous[selectedId] ? previous : { ...previous, [selectedId]: { ...value.notes, savedText: value.notes.text } });
    }).catch(error => { if (!controller.signal.aborted) setDetailError(localErrorMessage(error)); });
    return () => controller.abort();
  }, [selectedId, selectedAsset?.updated_at, selectedAsset?.has_result, selectedJobStatus, refreshKey]);

  const refresh = () => setRefreshKey(value => value + 1);
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (submitting || active) return;
    const invalid = inspectLocalImport(media, subtitles, mode, consent, budget, capabilities);
    if (invalid) { setActionError(invalid); return; }
    const controller = new AbortController();
    uploadController.current = controller;
    setSubmitting(true); setActionError(''); setUploadStatus('正在将媒体保存到本机服务…');
    try {
      const uploaded = await api.upload(media!, 'media', controller.signal);
      let subtitleId: string | undefined;
      if (mode === 'subtitles' && subtitles) {
        setUploadStatus('正在将字幕保存到本机服务…');
        subtitleId = (await api.upload(subtitles, 'subtitles', controller.signal)).upload_id;
      }
      if (controller.signal.aborted || !mounted.current) return;
      setUploadStatus('正在创建处理任务…');
      const job = await api.createJob({ media_upload_id: uploaded.upload_id, subtitle_upload_id: subtitleId, mode, allow_cloud_asr: mode === 'qwen' && consent, max_requests: mode === 'qwen' ? budget : 1 });
      if (!mounted.current) return;
      setJobs(previous => [job, ...previous.filter(item => item.id !== job.id)]);
      pendingSelection.current = job.id;
      if (job.asset_id) setSelectedId(job.asset_id);
      setConsent(false); setMedia(null); setSubtitles(null); setFileKey(value => value + 1); refresh();
    } catch (error) { if (mounted.current) setActionError(controller.signal.aborted ? '已停止上传。本次没有提交识别任务。' : localErrorMessage(error)); }
    finally { if (mounted.current) { setSubmitting(false); setUploadStatus(''); } uploadController.current = null; }
  };

  const cancel = async (job: LocalJob) => {
    setActingJob(job.id); setActionError('');
    try { const value = await api.cancel(job.id); setJobs(previous => previous.map(item => item.id === value.id ? value : item)); refresh(); }
    catch (error) { setActionError(localErrorMessage(error)); }
    finally { setActingJob(null); }
  };
  const retry = async (job: LocalJob, allowed: boolean, maxRequests: number) => {
    setActingJob(job.id); setActionError('');
    try {
      const value = await api.retry(job.id, job.mode === 'qwen' && allowed, job.mode === 'qwen' ? maxRequests : 1);
      setJobs(previous => [value, ...previous.filter(item => item.id !== value.id)]); pendingSelection.current = value.id; refresh();
    } catch (error) { setActionError(localErrorMessage(error)); throw error; }
    finally { setActingJob(null); }
  };
  const saveNotes = useCallback(async (id: string, revision?: string) => {
    const draft = drafts[id];
    if (!draft || draft.saving) return;
    setDrafts(previous => ({ ...previous, [id]: { ...previous[id], saving: true, message: '' } }));
    try {
      const saved = await api.saveNotes(id, { text: draft.text, revision: revision ?? draft.revision });
      setDrafts(previous => ({ ...previous, [id]: { ...previous[id], revision: saved.revision, savedText: saved.text, conflict: undefined, saving: false, message: '已保存到本机' } }));
    } catch (error) {
      if (error instanceof LocalMediaError && error.status === 409) {
        try {
          const latest = await api.detail(id);
          setDrafts(previous => ({ ...previous, [id]: { ...previous[id], conflict: latest.notes, saving: false, message: '草稿未丢失，请比较最新笔记后再保存。' } }));
          return;
        } catch { /* Preserve the draft even if the latest version cannot be loaded. */ }
      }
      setDrafts(previous => ({ ...previous, [id]: { ...previous[id], saving: false, message: localErrorMessage(error) } }));
    }
  }, [drafts]);

  const visibleJobs = jobs;
  return <div className="lm-root">
    <header className="lm-header"><div className="lm-brand"><span className="lm-brand-mark"><AudioLines size={22} /></span><div><strong>DouyinMind <span>/ 本地资料</span></strong><p>声音留成文字，随时回到原片</p></div></div><button type="button" className="lm-button" onClick={onExit}><ArrowLeft size={16} />返回</button></header>
    <main className="lm-layout">
      <aside className="lm-sidebar" aria-label="导入和本地资料">
        <section className="lm-import"><p className="lm-eyebrow">从一个文件开始</p><h1>导入本地媒体</h1><p className="lm-intro">无需登录抖音。先保存到这台电脑，再选择如何获得文字。</p>
          <div className="lm-health"><span className={`lm-dot ${capabilities?.ffmpeg_ready ? 'ready' : ''}`} /><span>{capabilities ? capabilities.ffmpeg_ready ? '本机处理已就绪' : '需要安装 ffmpeg' : '正在连接本机服务'}</span><button type="button" onClick={refresh} aria-label="重新连接本机服务"><RefreshCw size={13} />重新连接</button></div>
          {connectionError && <p className="lm-error" role="alert">{connectionError}</p>}
          <form onSubmit={event => { void submit(event); }}>
            <label className="lm-file-picker" htmlFor="local-media-file"><Upload size={23} /><strong>{media ? media.name : '选择视频或音频'}</strong><span>MP4 / MP3 / M4A / WAV</span><input key={`media-${fileKey}`} id="local-media-file" type="file" accept=".mp4,.mp3,.m4a,.wav" disabled={submitting} onChange={event => { setMedia(event.target.files?.[0] ?? null); setActionError(''); setConsent(false); }} /></label>
            <p className="lm-small">{media ? `${formatBytes(media.size)} · ` : ''}最多 {capabilities ? formatBytes(capabilities.limits.media_bytes) : '2 GiB'} / {capabilities ? Math.round(capabilities.limits.duration_seconds / 60) : 180} 分钟</p>
            <fieldset className="lm-modes" disabled={submitting}><legend>如何获得文字</legend>
              <label className={mode === 'subtitles' ? 'selected' : ''}><input type="radio" name="local-mode" value="subtitles" checked={mode === 'subtitles'} onChange={() => { setMode('subtitles'); setConsent(false); }} /><span><strong>导入已有字幕</strong><small>离线处理，不调用云端</small></span></label>
              <label className={mode === 'qwen' ? 'selected' : ''}><input type="radio" name="local-mode" value="qwen" checked={mode === 'qwen'} onChange={() => { setMode('qwen'); setConsent(false); }} /><span><strong>识别视频中的声音</strong><small>云端处理，需要单独授权</small></span></label>
            </fieldset>
            {mode === 'subtitles' ? <label className="lm-field" htmlFor="local-subtitle-file">选择已有字幕（SRT / VTT）<input key={`subtitle-${fileKey}`} id="local-subtitle-file" type="file" accept=".srt,.vtt" disabled={submitting} onChange={event => setSubtitles(event.target.files?.[0] ?? null)} /><span className="lm-small">没有字幕时，请选择云端声音识别。</span></label>
              : <CloudOptions prefix="new-job" capabilities={capabilities} allowed={consent} onAllowed={setConsent} budget={budget} onBudget={setBudget} disabled={submitting} />}
            <button type="submit" className="lm-button lm-primary lm-submit" disabled={submitting || active || !capabilities?.ffmpeg_ready}>{submitting ? <LoaderCircle size={16} className="lm-spin" /> : <Upload size={16} />}{submitting ? '正在保存文件…' : active ? '等待当前任务结束' : mode === 'subtitles' ? '导入字幕与媒体' : '开始声音识别'}</button>
            {submitting && <p className="lm-small" role="status">{uploadStatus}</p>}
            <p className="lm-footnote">AI 总结默认关闭。资料保存在本机，不会自动进入语义检索或跨视频问答。</p>
          </form>
        </section>
        <section className="lm-library" aria-labelledby="local-library-title"><div className="lm-section-heading"><h2 id="local-library-title"><FolderOpen size={17} />我的本地资料</h2><span>{assets.length}</span></div>
          {loading ? <p className="lm-small">正在读取本地资料…</p> : assets.length === 0 ? <p className="lm-small">导入后的文件会出现在这里，重新打开应用也能继续查看。</p> : <div className="lm-asset-list">{assets.map(asset => <button type="button" key={asset.id} className={`lm-asset ${selectedId === asset.id ? 'selected' : ''}`} onClick={() => setSelectedId(asset.id)} aria-pressed={selectedId === asset.id}><FileAudio size={17} /><span><strong>{asset.title || asset.filename}</strong><small>{asset.has_result ? ORIGIN[asset.origin || ''] || '已有结果' : '等待结果'} · {formatBytes(asset.size_bytes)}</small></span></button>)}</div>}
        </section>
      </aside>
      <div className="lm-main">
        {listError && <p className="lm-error" role="alert">{listError} <button type="button" onClick={refresh}>刷新</button></p>}
        {actionError && <p className="lm-error" role="alert">{actionError}<button type="button" onClick={() => setActionError('')} aria-label="关闭提示"><X size={16} /></button></p>}
        {visibleJobs.length > 0 && <section className="lm-jobs" aria-labelledby="local-jobs-title"><div className="lm-section-heading"><h2 id="local-jobs-title">最近处理</h2><span>{active ? '后台处理中，离开页面后仍继续' : '重启后的中断任务需要手动重试'}</span></div><div className="lm-job-list">{visibleJobs.map(job => <JobCard key={job.id} job={job} capabilities={capabilities} busy={actingJob != null || submitting || (active && !isActiveJob(job))} onCancel={cancel} onRetry={retry} onOpen={setSelectedId} />)}</div></section>}
        {detailError && <p className="lm-error" role="alert">{detailError} <button type="button" onClick={refresh}>重试加载</button></p>}
        {detail && detail.asset.id === selectedId ? <AssetResult key={detail.asset.id} detail={detail} draft={drafts[detail.asset.id]} onDraft={text => setDrafts(previous => ({ ...previous, [detail.asset.id]: { ...previous[detail.asset.id], text, message: '' } }))} onSave={revision => { void saveNotes(detail.asset.id, revision); }} />
          : selectedId ? <div className="lm-empty"><LoaderCircle size={26} className="lm-spin" /><h2>正在读取资料…</h2></div>
            : <section className="lm-empty"><div className="lm-empty-icon"><AudioLines size={45} /></div><p className="lm-eyebrow">本地声音工作台</p><h2>让一段视频，变成可以回看的文字</h2><p>选择媒体与已有字幕，或授权识别其中的声音。<br />完成后可边播放边核对，也能留下自己的笔记。</p><div className="lm-empty-steps"><span><Upload size={18} />选择文件</span><span><FileText size={18} />生成逐字稿</span><span><Play size={18} />对照原片</span></div></section>}
      </div>
    </main>
  </div>;
}
