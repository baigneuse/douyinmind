import { useCallback, useDeferredValue, useEffect, useRef, useState } from 'react';
import { Archive, ArrowDownToLine, ArrowUpRight, Check, ChevronRight, CircleHelp, Download, FileVideo, FolderOpen, Layers, Link, LoaderCircle, Plus, RefreshCw, Save, Search, Star, Video, X } from 'lucide-react';
import { DOWNLOAD_LABELS, PLATFORM_LABELS, NetworkMediaError, downloadActive, downloadFileUrl, filterSources, hasDownloadRecord, inspectSourcePatch, networkErrorMessage, networkMediaApi as api, networkMediaUrl, parseTags, safeSourceUrl, sizeLabel } from '../networkMediaApi';
import type { LibraryView, NetworkCapabilities, NetworkSource, SourcePatch } from '../networkMediaApi';
import './networkMedia.css';

interface Props { active: boolean; onOpenLocal: () => void; onOpenDouyin: () => void; }
interface EditDraft { title: string; tagsText: string; note: string; baseline: string; revision: number; saving?: boolean; message?: string; conflict?: boolean; }
const metadataKey = (source: Pick<NetworkSource, 'title' | 'tags' | 'note'>) => JSON.stringify([source.title, source.tags, source.note]);
const draftKey = (draft: EditDraft) => metadataKey({ title: draft.title, tags: parseTags(draft.tagsText), note: draft.note });
const sourceDraft = (source: NetworkSource): EditDraft => ({ title: source.title, tagsText: source.tags.join(', '), note: source.note, baseline: metadataKey(source), revision: source.revision });
const dateLabel = (value: string) => {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' });
};
function sourceDisplayTitle(source: NetworkSource) {
  if (source.title.trim()) return source.title;
  try { return new URL(source.url).hostname; } catch { return '已保存的链接'; }
}

function PlatformMark({ platform }: { platform: string }) {
  const marks: Record<string, string> = { douyin: '抖', bilibili: 'b', youtube: '▶', direct: '↗' };
  return <span className={`nv-platform-mark nv-platform-${platform.replace(/[^a-z]/g, '')}`} aria-hidden="true">{marks[platform] || '↗'}</span>;
}

function DownloadState({ source, compact = false }: { source: NetworkSource; compact?: boolean }) {
  const progress = source.progress;
  const total = progress?.total_bytes;
  const percent = total && total > 0 ? Math.min(100, Math.round(progress!.downloaded_bytes / total * 100)) : null;
  return <div className={`nv-download-state ${compact ? 'compact' : ''}`}>
    <span className={`nv-status nv-status-${source.status}`}>
      {downloadActive(source) ? <LoaderCircle size={12} className="nv-spin" /> : source.status === 'ready' ? <Check size={12} /> : null}
      {DOWNLOAD_LABELS[source.status] || '状态待确认'}{source.status === 'downloading' && percent != null ? ` · ${percent}%` : ''}
    </span>
    {!compact && progress && source.status === 'downloading' && <div className="nv-progress">
      {percent != null && <progress value={progress.downloaded_bytes} max={total!} aria-label="视频下载进度" />}
      <p>{sizeLabel(progress.downloaded_bytes)}{total ? ` / ${sizeLabel(total)}` : ' · 文件总大小暂未知'}</p>
    </div>}
  </div>;
}

function SourceDetail({ source, draft, capability, busy, onEdit, onSave, onToggleStar, onArchive, onDownload, onCancel }: {
  source: NetworkSource; draft: EditDraft | undefined; capability: NetworkCapabilities | null; busy: boolean;
  onEdit: (patch: Partial<EditDraft>) => void; onSave: (revision?: number) => void; onToggleStar: () => void;
  onArchive: () => void; onDownload: () => void; onCancel: () => void;
}) {
  const [playbackError, setPlaybackError] = useState(false);
  const active = downloadActive(source);
  const origin = safeSourceUrl(source.url);
  const dirty = draft ? draftKey(draft) !== draft.baseline : false;
  const changedElsewhere = Boolean(draft && dirty && metadataKey(source) !== draft.baseline);
  const mediaPath = `${networkMediaUrl(source.id)}?v=${encodeURIComponent(source.media_version ?? '')}`;
  const playerProps = { controls: true, preload: 'metadata' as const, src: mediaPath, onError: () => setPlaybackError(true), onLoadedMetadata: () => setPlaybackError(false) };
  return <section className="nv-detail" aria-labelledby="network-detail-title">
    <div className="nv-detail-top"><span className="nv-eyebrow">资料详情</span><div><button type="button" className={`nv-icon-button ${source.starred ? 'starred' : ''}`} disabled={busy} onClick={onToggleStar} aria-label={source.starred ? '取消星标' : '添加星标'} aria-pressed={source.starred}><Star size={17} fill={source.starred ? 'currentColor' : 'none'} /></button><button type="button" className="nv-icon-button" disabled={busy} onClick={onArchive} aria-label={source.archived ? '恢复到资料库' : '归档资料'} title={source.archived ? '恢复到资料库' : '归档资料'}><Archive size={17} /></button></div></div>
    <div className="nv-detail-identity"><PlatformMark platform={source.platform} /><span>{PLATFORM_LABELS[source.platform] || source.platform}{source.author ? ` · ${source.author}` : ''}</span>{source.archived && <span className="nv-archived-label">已归档</span>}</div>
    <h2 id="network-detail-title">{sourceDisplayTitle(source)}</h2>
    <div className="nv-origin"><span>来源</span>{origin ? <a href={origin} target="_blank" rel="noopener noreferrer" title={source.url}>{source.url}<ArrowUpRight size={14} /></a> : <span>{source.url}</span>}</div>

    {source.has_media ? <div className="nv-media-panel">
      {source.content_type?.startsWith('audio/') ? <audio {...playerProps} aria-label="本地媒体播放器" /> : <video {...playerProps} aria-label="已下载视频播放器" />}
      {playbackError && <p className="nv-alert nv-alert-warning">浏览器暂时无法播放这个文件。可以下载原件后用本机播放器打开。</p>}
      <div className="nv-media-caption"><span><Check size={12} />已有本地副本{source.size_bytes != null ? ` · ${sizeLabel(source.size_bytes)}` : ''}</span><a href={downloadFileUrl(source.id)} download><ArrowDownToLine size={13} />下载原件</a></div>
    </div> : <div className="nv-no-media"><FileVideo size={30} /><strong>链接已保存</strong><p>需要离线观看时，再下载到这台电脑。</p>{origin && <a href={origin} target="_blank" rel="noopener noreferrer">在原站观看<ArrowUpRight size={13} /></a>}</div>}

    <div className="nv-download-box"><div className="nv-download-row"><DownloadState source={source} />{active ? <button type="button" className="nv-button" disabled={busy || source.status === 'cancel_requested'} onClick={onCancel}><X size={14} />取消下载</button> : <button type="button" className="nv-button" disabled={busy || !capability?.downloader_ready} onClick={onDownload}><Download size={14} />{source.has_media ? '重新下载' : ['failed', 'needs_login', 'unsupported', 'cancelled', 'interrupted'].includes(source.status) ? '手动重试下载' : '下载到本机'}</button>}</div>
      {source.error && <p className="nv-download-error">{source.error.message}</p>}
      {source.status === 'needs_login' && <p className="nv-small">需要到原站完成登录或验证。本工具不会索取账号、读取浏览器 Cookie 或绕过访问限制。</p>}
      {source.status === 'interrupted' && <p className="nv-small">上次下载因后台停止而中断。系统不会自动重试，请按需手动继续。</p>}
      {source.has_media && source.status !== 'ready' && <p className="nv-small">这里保留上次成功保存的副本，新一次下载状态单独显示。</p>}
      <p className="nv-small">下载受平台与网络影响，以本次结果为准。保存链接不代表可下载。</p>
      {!capability?.downloader_ready && <p className="nv-small">下载工具尚未就绪，仍可保存和整理链接。</p>}
      <details className="nv-download-limits"><summary>下载范围与限制</summary><p>仅在点击下载后访问原站。本轮不执行转写或 AI 总结。{capability ? `文件最多 ${sizeLabel(capability.limits.media_bytes)}，视频最多 ${Math.round(capability.limits.duration_seconds / 60)} 分钟，清晰度最高 ${capability.limits.max_height}p。` : ''}失败和取消不会删除已保存的链接。</p></details>
    </div>

    <form className="nv-edit-form" onSubmit={event => { event.preventDefault(); if (!draft?.conflict && !changedElsewhere) onSave(); }}>
      <div className="nv-section-title"><h3>整理资料</h3><span>{dirty ? '有未保存的修改' : '已保存'}</span></div>
      <label htmlFor="network-title">标题<input id="network-title" value={draft?.title ?? ''} maxLength={512} disabled={!draft} onChange={event => onEdit({ title: event.target.value, message: '' })} placeholder="写一个方便回忆的标题" /></label>
      <label htmlFor="network-tags">标签<input id="network-tags" value={draft?.tagsText ?? ''} disabled={!draft} onChange={event => onEdit({ tagsText: event.target.value, message: '' })} placeholder="用逗号分隔，如 设计, 学习, 待看" /><small>最多 20 个标签，每个不超过 40 字。</small></label>
      <label htmlFor="network-note">备注<textarea id="network-note" value={draft?.note ?? ''} rows={5} disabled={!draft} onChange={event => onEdit({ note: event.target.value, message: '' })} placeholder="为什么收藏？有哪些值得留意的内容？" /></label>
      {(changedElsewhere || draft?.conflict) && <div className="nv-conflict" role="alert"><strong>这条资料已更新，你的草稿仍保留。</strong><p>比较最新内容，在上方编辑框合并后再保存。</p><details><summary>查看最新保存的内容</summary><dl><dt>标题</dt><dd>{source.title}</dd><dt>标签</dt><dd>{source.tags.join(', ') || '无'}</dd><dt>备注</dt><dd>{source.note || '无'}</dd></dl></details><button type="button" className="nv-button" disabled={draft?.saving} onClick={() => onSave(source.revision)}>已合并，保存当前草稿</button></div>}
      <div className="nv-edit-footer"><button type="submit" className="nv-button nv-primary" disabled={!draft || !dirty || draft.saving || Boolean(draft.conflict) || changedElsewhere}><Save size={14} />{draft?.saving ? '正在保存…' : '保存修改'}</button><span className="nv-small" role="status">{draft?.message || '备注与标签仅保存在本机'}</span></div>
    </form>
    <div className="nv-detail-footer"><span>添加于 {dateLabel(source.created_at)}</span><button type="button" onClick={onArchive} disabled={busy}>{source.archived ? '恢复到资料库' : '归档资料'}<ChevronRight size={13} /></button><p>归档只是收起资料，随时可以恢复。</p></div>
  </section>;
}

export default function NetworkMediaLibrary({ active, onOpenLocal, onOpenDouyin }: Props) {
  const [capability, setCapability] = useState<NetworkCapabilities | null>(null);
  const [sources, setSources] = useState<NetworkSource[]>([]);
  const [loading, setLoading] = useState(true);
  const [connectionError, setConnectionError] = useState('');
  const [capabilityError, setCapabilityError] = useState('');
  const [actionError, setActionError] = useState('');
  const [notice, setNotice] = useState('');
  const [refreshKey, setRefreshKey] = useState(0);
  const [view, setView] = useState<LibraryView>('all');
  const [platform, setPlatform] = useState('all');
  const [search, setSearch] = useState('');
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, EditDraft>>({});
  const [shareText, setShareText] = useState('');
  const [collecting, setCollecting] = useState(false);
  const [collectorOpen, setCollectorOpen] = useState(true);
  const [acting, setActing] = useState<Set<string>>(new Set());
  const collectLock = useRef(false);
  const actionLocks = useRef(new Set<string>());
  const mutationEpoch = useRef(0);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const deferredSearch = useDeferredValue(search);
  const filtered = filterSources(sources, view, platform, deferredSearch);
  const selected = sources.find(source => source.id === selectedId);
  const downloadRecords = sources.filter(hasDownloadRecord);
  const unarchived = sources.filter(source => !source.archived);
  const dirty = Object.values(drafts).some(draft => draftKey(draft) !== draft.baseline) || Boolean(shareText.trim());
  const platforms = capability?.platforms ?? [
    { id: 'douyin', label: '抖音', download_verified: false }, { id: 'bilibili', label: 'Bilibili', download_verified: false },
    { id: 'youtube', label: 'YouTube', download_verified: false }, { id: 'direct', label: '媒体直链', download_verified: false },
  ];
  const viewTitles: Record<LibraryView, string> = { all: '全部资料', starred: '星标资料', archived: '已归档', downloads: '下载记录' };
  const refresh = useCallback(() => setRefreshKey(value => value + 1), []);

  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [dirty]);
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    void api.capabilities(controller.signal).then(value => { if (!controller.signal.aborted) { setCapability(value); setCapabilityError(''); } })
      .catch(error => { if (!controller.signal.aborted) { setCapability(null); setCapabilityError(networkErrorMessage(error)); } });
    return () => controller.abort();
  }, [active, refreshKey]);
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      let fast = false;
      const startedAtEpoch = mutationEpoch.current;
      try {
        const result = await api.sources(controller.signal);
        if (controller.signal.aborted || startedAtEpoch !== mutationEpoch.current) return;
        setSources(result.items); setConnectionError('');
        fast = result.items.some(downloadActive);
        setDrafts(previous => {
          const next = { ...previous };
          for (const source of result.items) {
            const draft = previous[source.id];
            if (!draft || (!draft.saving && !draft.conflict && draftKey(draft) === draft.baseline)) next[source.id] = { ...sourceDraft(source), message: draft?.message };
          }
          return next;
        });
        setSelectedId(previous => previous ?? result.items.find(source => !source.archived)?.id ?? null);
      } catch (error) { if (!controller.signal.aborted) setConnectionError(networkErrorMessage(error)); }
      finally { if (!controller.signal.aborted) { setLoading(false); timer = setTimeout(() => { void poll(); }, fast ? 1500 : 6000); } }
    };
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [active, refreshKey]);

  const putSource = (source: NetworkSource) => setSources(previous => {
    const exists = previous.some(item => item.id === source.id);
    return exists ? previous.map(item => item.id === source.id ? source : item) : [source, ...previous];
  });
  const collect = async (event: React.FormEvent) => {
    event.preventDefault();
    if (collectLock.current) return;
    collectLock.current = true; setCollecting(true); setActionError(''); setNotice('');
    const input = shareText;
    try {
      const result = await api.collect(input);
      mutationEpoch.current++;
      setSources(previous => { const items = new Map(previous.map(source => [source.id, source])); result.items.forEach(source => items.set(source.id, source)); return [...items.values()].sort((a, b) => b.created_at.localeCompare(a.created_at)); });
      setDrafts(previous => { const next = { ...previous }; result.items.forEach(source => { if (!next[source.id]) next[source.id] = sourceDraft(source); }); return next; });
      setNotice(`已新增 ${result.created} 条链接${result.duplicates ? `，${result.duplicates} 条已存在` : ''}。没有发起下载。`);
      setShareText(current => current === input ? '' : current);
      const first = result.items[0];
      if (first) { setSelectedId(first.id); setView(first.archived ? 'archived' : 'all'); setPlatform('all'); setSearch(''); }
      setCollectorOpen(false); refresh();
    } catch (error) { setActionError(networkErrorMessage(error)); }
    finally { collectLock.current = false; setCollecting(false); }
  };
  const act = async (source: NetworkSource, action: 'star' | 'archive' | 'download' | 'cancel') => {
    if (actionLocks.current.has(source.id)) return;
    actionLocks.current.add(source.id); setActing(new Set(actionLocks.current)); setActionError('');
    try {
      const value = action === 'download' ? await api.download(source.id) : action === 'cancel' ? await api.cancel(source.id)
        : await api.update(source.id, action === 'star' ? { starred: !source.starred, expected_revision: source.revision } : { archived: !source.archived, expected_revision: source.revision });
      mutationEpoch.current++;
      putSource(value);
      if (action === 'star' || action === 'archive') setDrafts(previous => {
        const draft = previous[source.id];
        return draft && draft.revision === source.revision ? { ...previous, [source.id]: { ...draft, revision: value.revision } } : previous;
      });
      if (action === 'archive') { setNotice(value.archived ? '已归档，可在“已归档”中恢复。' : '已恢复到资料库。'); setView(value.archived ? 'archived' : 'all'); }
      refresh();
    } catch (error) { setActionError(networkErrorMessage(error)); }
    finally { actionLocks.current.delete(source.id); setActing(new Set(actionLocks.current)); }
  };
  const save = async (source: NetworkSource, revision?: number) => {
    const draft = drafts[source.id];
    if (!draft || draft.saving || actionLocks.current.has(source.id)) return;
    const patch: SourcePatch = { title: draft.title, tags: parseTags(draft.tagsText), note: draft.note, expected_revision: revision ?? draft.revision };
    const invalid = inspectSourcePatch(patch);
    if (invalid) { setDrafts(previous => ({ ...previous, [source.id]: { ...previous[source.id], message: invalid } })); return; }
    actionLocks.current.add(source.id); setActing(new Set(actionLocks.current));
    setDrafts(previous => ({ ...previous, [source.id]: { ...previous[source.id], saving: true, message: '' } }));
    try {
      const value = await api.update(source.id, patch); mutationEpoch.current++; putSource(value);
      setDrafts(previous => ({ ...previous, [source.id]: {
        ...(draftKey(previous[source.id]) === draftKey(draft) ? sourceDraft(value) : previous[source.id]),
        baseline: metadataKey(value), revision: value.revision, saving: false, conflict: false, message: '修改已保存到本机',
      } }));
    } catch (error) {
      const conflict = error instanceof NetworkMediaError && error.code === 'source_conflict';
      setDrafts(previous => ({ ...previous, [source.id]: { ...previous[source.id], saving: false, conflict, message: networkErrorMessage(error) } }));
      if (conflict) refresh();
    }
    finally { actionLocks.current.delete(source.id); setActing(new Set(actionLocks.current)); }
  };
  const openCollector = () => { setCollectorOpen(true); setTimeout(() => textarea.current?.focus(), 0); };

  return <div className="nv-root">
    <header className="nv-header"><div className="nv-brand"><span><Video size={21} /></span><div><strong>DouyinMind</strong><p>视频资料库</p></div></div><div className="nv-header-right"><span className="nv-local-badge"><span />资料保存在本机</span><button type="button" className="nv-button" onClick={refresh}><RefreshCw size={14} />重新连接</button></div></header>
    <div className="nv-workspace">
      <aside className="nv-sidebar"><button type="button" className="nv-button nv-primary nv-collect-button" onClick={openCollector}><Plus size={17} />收集视频链接</button>
        <nav className="nv-navigation" aria-label="资料视图">
          {([{ id: 'all', label: '全部资料', Icon: Layers, count: unarchived.length }, { id: 'starred', label: '星标', Icon: Star, count: unarchived.filter(source => source.starred).length }, { id: 'downloads', label: '下载记录', Icon: Download, count: downloadRecords.length }, { id: 'archived', label: '已归档', Icon: Archive, count: sources.filter(source => source.archived).length }] as const).map(({ id, label, Icon, count }) => <button type="button" key={id} className={view === id ? 'active' : ''} aria-current={view === id ? 'page' : undefined} onClick={() => { setView(id); }}><Icon size={16} /><span>{label}</span><small>{count}</small></button>)}
        </nav>
        <div className="nv-platform-filters"><p className="nv-eyebrow">按来源筛选</p><button type="button" className={platform === 'all' ? 'selected' : ''} aria-pressed={platform === 'all'} onClick={() => setPlatform('all')}><span className="nv-platform-dot" />全部来源</button>{platforms.map(item => <button type="button" key={item.id} className={platform === item.id ? 'selected' : ''} aria-pressed={platform === item.id} onClick={() => setPlatform(item.id)}><PlatformMark platform={item.id} />{item.label}</button>)}</div>
        <div className="nv-sidebar-footer"><button type="button" onClick={onOpenLocal}><FolderOpen size={15} /><span>本地字幕工具</span><ArrowUpRight size={13} /></button><button type="button" onClick={onOpenDouyin}><Layers size={15} /><span>原抖音知识库</span><ArrowUpRight size={13} /></button><p>先保存来源，再整理和回看。<br />转写和 AI 总结暂不扩展。</p></div>
      </aside>
      <main className="nv-main"><div className="nv-page-heading"><div><p className="nv-eyebrow">把值得留住的视频，放在一起</p><h1>{viewTitles[view]} <span>{filtered.length}</span></h1></div><button type="button" className="nv-button nv-mobile-collect" onClick={openCollector}><Plus size={16} />收集链接</button></div>
        {(connectionError || capabilityError) && <p className="nv-alert nv-alert-error" role="alert">{connectionError || capabilityError}</p>}
        {actionError && <p className="nv-alert nv-alert-error" role="alert">{actionError}<button type="button" onClick={() => setActionError('')} aria-label="关闭错误提示"><X size={14} /></button></p>}
        {notice && <p className="nv-alert nv-alert-success" role="status"><Check size={14} />{notice}<button type="button" onClick={() => setNotice('')} aria-label="关闭操作提示"><X size={14} /></button></p>}
        {collectorOpen && <form className="nv-collector" onSubmit={event => { void collect(event); }}><div><h2><Link size={16} />粘贴分享文字或视频链接</h2>{sources.length > 0 && <button type="button" className="nv-icon-button" onClick={() => setCollectorOpen(false)} aria-label="收起收集框"><X size={16} /></button>}</div><label htmlFor="network-share-text" className="nv-sr-only">分享文字或视频链接</label><textarea ref={textarea} id="network-share-text" value={shareText} onChange={event => setShareText(event.target.value)} rows={3} placeholder="把分享文字直接粘贴到这里，也可以一行一条链接…" /><div className="nv-collector-footer"><p>每次最多 {capability?.max_batch ?? 20} 条 · 保存链接不联网，也不会自动下载</p><button type="submit" className="nv-button nv-primary" disabled={collecting || !capability || !shareText.trim()}>{collecting ? <LoaderCircle size={15} className="nv-spin" /> : <Plus size={15} />}{collecting ? '正在保存…' : '保存链接'}</button></div><p className="nv-supported">可整理抖音 / Bilibili / YouTube 分享链接及 MP4、WebM 直链。下载可用性按实际结果确认。</p></form>}
        <div className="nv-search-row"><label><Search size={16} /><span className="nv-sr-only">搜索资料</span><input type="search" value={search} onChange={event => setSearch(event.target.value)} placeholder="搜索标题、标签、备注或链接" /></label>{(search || platform !== 'all') && <button type="button" onClick={() => { setSearch(''); setPlatform('all'); }}>清除筛选</button>}</div>
        <div className="nv-content-grid"><section className="nv-source-list" aria-label="已保存的视频来源">
          {loading ? <div className="nv-list-empty"><LoaderCircle size={26} className="nv-spin" /><h2>正在读取资料…</h2></div> : filtered.length === 0 ? <div className="nv-list-empty"><span className="nv-empty-icon">{view === 'archived' ? <Archive size={32} /> : <Link size={32} />}</span><h2>{sources.length ? '这里还没有匹配的资料' : '从一条视频链接开始'}</h2><p>{sources.length ? '切换视图或清除筛选后再看看。' : '把看过、想看、值得重看的视频收集起来。无需登录，先留住来源。'}</p>{!sources.length && <button type="button" className="nv-button" onClick={openCollector}><Plus size={15} />添加第一条链接</button>}</div>
            : filtered.map(source => <article className={`nv-source-card ${selectedId === source.id ? 'selected' : ''}`} key={source.id}><button type="button" className="nv-source-open" onClick={() => setSelectedId(source.id)} aria-pressed={selectedId === source.id}><div className="nv-source-meta"><PlatformMark platform={source.platform} /><span>{PLATFORM_LABELS[source.platform] || source.platform}</span><time dateTime={source.created_at}>{dateLabel(source.created_at)}</time></div><h2>{sourceDisplayTitle(source)}</h2><p className="nv-source-url">{source.url}</p>{source.tags.length > 0 && <div className="nv-tags">{source.tags.slice(0, 3).map(tag => <span key={tag}>{tag}</span>)}{source.tags.length > 3 && <span>+{source.tags.length - 3}</span>}</div>}<DownloadState source={source} compact /></button><button type="button" className={`nv-card-star ${source.starred ? 'starred' : ''}`} disabled={acting.has(source.id)} onClick={() => { void act(source, 'star'); }} aria-label={`${source.starred ? '取消' : '添加'}星标：${sourceDisplayTitle(source)}`} aria-pressed={source.starred}><Star size={16} fill={source.starred ? 'currentColor' : 'none'} /></button></article>)}
        </section>
          {selected ? <SourceDetail key={selected.id} source={selected} draft={drafts[selected.id]} capability={capability} busy={acting.has(selected.id) || Boolean(drafts[selected.id]?.saving)} onEdit={patch => setDrafts(previous => ({ ...previous, [selected.id]: { ...previous[selected.id], ...patch } }))} onSave={revision => { void save(selected, revision); }} onToggleStar={() => { void act(selected, 'star'); }} onArchive={() => { void act(selected, 'archive'); }} onDownload={() => { void act(selected, 'download'); }} onCancel={() => { void act(selected, 'cancel'); }} />
            : <section className="nv-detail-placeholder"><FileVideo size={39} /><h2>选一条资料，展开看看</h2><p>整理标题、标签和备注，<br />需要时再下载到本机。</p><div><CircleHelp size={14} /><span>保存的是来源链接，不会自动抓取视频、登录账号或读取 Cookie。</span></div></section>}
        </div>
      </main>
    </div>
  </div>;
}
