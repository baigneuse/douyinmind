import { useRef, useState } from 'react';
import { Check, CircleHelp, LoaderCircle, Plug, Search, UserRound, Video, X } from 'lucide-react';
import { NetworkMediaError, TIKHUB_STATE_LABELS, costLabel, durationLabel, networkErrorMessage, networkMediaApi as api, safeCoverUrl, tikhubLookupCost, tikhubState } from '../networkMediaApi';
import type { NetworkSource, TikHubCheck, TikHubPreview, TikHubPreviewItem, TikHubStatus } from '../networkMediaApi';

interface Props {
  status: TikHubStatus | null;
  connected: boolean;
  onStatus: (status: TikHubStatus) => void;
  onImported: (result: { items: NetworkSource[]; created: number; duplicates: number }, download: boolean) => Promise<void> | void;
}
type Mode = 'video' | 'creator';
const selectable = (item: TikHubPreviewItem) => !item.has_media;
const dateLabel = (value: string | null) => {
  const date = value ? new Date(value) : null;
  return date && !Number.isNaN(date.getTime()) ? date.toLocaleDateString('zh-CN', { year: 'numeric', month: 'short', day: 'numeric' }) : '';
};

function CheckResult({ check }: { check: TikHubCheck }) {
  if (!check.ok) return <p className="nv-tk-check nv-tk-check-failed" role="status">{check.mode === 'demo' ? 'Demo 测试' : 'Token 检查'}未通过：{check.message}</p>;
  if (check.mode === 'demo' && check.sample) return <p className="nv-tk-check" role="status"><Check size={12} />免费 Demo 可用：取得固定样例《{check.sample.title}》，{check.sample.candidates} 个可用媒体地址{check.sample.codec ? `（${check.sample.codec.toUpperCase()}）` : ''}。这只验证连接和数据结构，没有下载视频。</p>;
  const account = check.account;
  if (!account) return null;
  const problems = [!account.key_active && 'Token 已停用', !account.account_active && '账户不可用', !account.email_verified && '邮箱未验证', account.missing_scopes.length > 0 && 'Token 权限不含所需接口'].filter(Boolean);
  return <p className={`nv-tk-check ${problems.length ? 'nv-tk-check-failed' : ''}`} role="status">{problems.length ? null : <Check size={12} />}Token 可用{problems.length ? `，但${problems.join('、')}` : ''}。余额 {account.balance_usd == null ? '未知' : `$${account.balance_usd.toFixed(3)}`} · 免费额度 {account.free_credit_usd == null ? '未知' : `$${account.free_credit_usd.toFixed(3)}`}</p>;
}

export default function TikHubPanel({ status, connected, onStatus, onImported }: Props) {
  const [mode, setMode] = useState<Mode>('video');
  const [text, setText] = useState('');
  const [busy, setBusy] = useState<'' | 'lookup' | 'import' | 'demo' | 'account'>('');
  const [prompt, setPrompt] = useState<{ mode: Mode; text: string; cursor: number } | null>(null);
  const [preview, setPreview] = useState<TikHubPreview | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [hiddenCovers, setHiddenCovers] = useState<Set<string>>(new Set());
  const [error, setError] = useState('');
  const lock = useRef(false);
  const state = tikhubState(status);
  if (!status?.available) return null;
  const unit = status.prices.per_paid_request_usd;
  const usage = status.usage;
  const check = status.last_check;

  const run = async (kind: typeof busy, task: () => Promise<void>) => {
    if (lock.current) return;
    lock.current = true; setBusy(kind); setError('');
    try { await task(); }
    catch (failure) { setError(networkErrorMessage(failure)); }
    finally { lock.current = false; setBusy(''); }
  };
  const runCheck = (kind: 'demo' | 'account') => run(kind, async () => { onStatus((await api.tikhubCheck(kind)).status); });
  // The first request is unconfirmed: a post already in the library is answered
  // without contacting TikHub; anything that costs money comes back as a prompt.
  const lookup = (target: { mode: Mode; text: string; cursor: number }, confirmed: boolean) => run('lookup', async () => {
    try {
      const result = target.mode === 'video' ? await api.tikhubResolve(target.text, confirmed) : await api.tikhubCreator(target.text, target.cursor, confirmed);
      setPreview(result); setPrompt(null); setHiddenCovers(new Set());
      setSelected(new Set(result.items.filter(selectable).map(item => item.item_id)));
      onStatus({ ...status, usage: result.usage });
    } catch (failure) {
      if (failure instanceof NetworkMediaError && failure.code === 'tikhub_confirmation_required' && !confirmed) { setPrompt(target); return; }
      setPrompt(null);
      throw failure;
    }
  });
  const importSelected = (download: boolean) => run('import', async () => {
    if (!preview) return;
    const result = await api.tikhubImport(preview.preview_id, preview.items.filter(item => selected.has(item.item_id)).map(item => item.item_id));
    await onImported(result, download);
    setPreview(null); setSelected(new Set()); setText('');
  });
  const toggle = (id: string) => setSelected(previous => { const next = new Set(previous); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  const choices = preview?.items.filter(selectable) ?? [];
  const allSelected = choices.length > 0 && choices.every(item => selected.has(item.item_id));
  const promptCost = prompt ? tikhubLookupCost(prompt.mode, prompt.text) : 0;

  return <section className="nv-collector nv-tikhub" aria-labelledby="tikhub-title">
    <div><h2 id="tikhub-title"><Plug size={16} />TikHub 抖音数据源</h2><span className={`nv-tk-pill nv-tk-${state}`} role="status"><span />{TIKHUB_STATE_LABELS[state]}</span></div>
    <p className="nv-small">用 TikHub 解析抖音公开作品和博主公开视频列表，替代不稳定的匿名解析。Token 只保存在本机后端，不会出现在浏览器、链接或日志中；不读取抖音 Cookie，不同步私人收藏夹。</p>
    <div className="nv-tk-usage"><span>今日付费请求 <strong>{usage.today.paid_attempts}</strong> / {usage.daily_limit}</span><span>今日预估费用 <strong>${usage.today.estimated_cost_usd.toFixed(3)}</strong></span><span>累计 {usage.total.requests} 次请求 · ${usage.total.estimated_cost_usd.toFixed(3)}</span><span>单价约 ${unit.toFixed(3)}/次（{status.prices.checked_on} 公开价）</span></div>
    <div className="nv-tk-actions">
      <button type="button" className="nv-button" disabled={!connected || Boolean(busy)} onClick={() => { void runCheck('demo'); }}>{busy === 'demo' ? <LoaderCircle size={13} className="nv-spin" /> : <CircleHelp size={13} />}免费 Demo 测试</button>
      <button type="button" className="nv-button" disabled={!connected || !status.configured || Boolean(busy)} onClick={() => { void runCheck('account'); }}>{busy === 'account' ? <LoaderCircle size={13} className="nv-spin" /> : <Plug size={13} />}检查 Token 与余额（免费）</button>
    </div>
    {check && <CheckResult check={check} />}
    {!status.endpoint_valid && <p className="nv-alert nv-alert-error" role="alert">TikHub 服务地址配置无效。请把 backend/.env 中的 TIKHUB_BASE_URL 改回 https://api.tikhub.io 后重启后端。</p>}
    {!status.configured && <p className="nv-tk-hint">尚未配置 Token：在这台电脑的 <code>backend/.env</code> 写入 <code>TIKHUB_API_KEY=你的Token</code>，重启后端再点“重新连接”。未配置时，抖音链接仍使用原来的匿名下载方式。</p>}

    {status.configured && <form className="nv-tk-form" onSubmit={event => { event.preventDefault(); void lookup({ mode, text, cursor: 0 }, false); }}>
      <div className="nv-tk-modes" role="group" aria-label="TikHub 导入方式">
        <button type="button" className={mode === 'video' ? 'active' : ''} aria-pressed={mode === 'video'} onClick={() => { setMode('video'); setPrompt(null); }}><Video size={13} />粘贴视频链接</button>
        <button type="button" className={mode === 'creator' ? 'active' : ''} aria-pressed={mode === 'creator'} onClick={() => { setMode('creator'); setPrompt(null); }}><UserRound size={13} />导入博主公开视频</button>
      </div>
      <label htmlFor="tikhub-link" className="nv-sr-only">{mode === 'video' ? '抖音视频分享文字或链接' : '抖音博主主页链接'}</label>
      <div className="nv-tk-input"><input id="tikhub-link" value={text} maxLength={2000} onChange={event => { setText(event.target.value); setPrompt(null); }} placeholder={mode === 'video' ? '粘贴一条抖音视频的分享文字或链接' : '粘贴博主主页链接，如 https://www.douyin.com/user/…'} />
        <button type="submit" className="nv-button nv-primary" disabled={!connected || Boolean(busy) || !text.trim()}>{busy === 'lookup' ? <LoaderCircle size={14} className="nv-spin" /> : <Search size={14} />}{mode === 'video' ? '解析视频' : `获取第一页（最多 ${status.page_size} 条）`}</button></div>
      <p className="nv-small">{mode === 'video' ? `一次解析一条链接，约 ${costLabel(1, unit)}；资料库里已有的作品不会再次请求。` : `一次获取一页，约 ${costLabel(1, unit)}；主页短链接需要多一次查询。只读取公开作品。`}发送前会请你确认。</p>
    </form>}

    {prompt && <div className="nv-tk-confirm" role="alertdialog" aria-labelledby="tikhub-confirm-title">
      <p id="tikhub-confirm-title"><strong>确认发送付费请求</strong>将向 TikHub 发送 {promptCost} 次请求，按公开单价预估 {costLabel(promptCost, unit)}，实际以 TikHub 账单为准。今日还可发送 {usage.remaining_today} 次。</p>
      <div><button type="button" className="nv-button nv-primary" disabled={Boolean(busy)} onClick={() => { void lookup(prompt, true); }}>{busy === 'lookup' ? <LoaderCircle size={13} className="nv-spin" /> : <Check size={13} />}确认并发送</button><button type="button" className="nv-button" disabled={Boolean(busy)} onClick={() => setPrompt(null)}>取消</button></div>
    </div>}
    {error && <p className="nv-alert nv-alert-error" role="alert">{error}<button type="button" onClick={() => setError('')} aria-label="关闭 TikHub 错误提示"><X size={14} /></button></p>}

    {preview && <div className="nv-tk-preview">
      <div className="nv-tk-preview-head"><div><strong>{preview.kind === 'creator' ? `${preview.creator?.nickname || '该博主'}的公开作品` : '解析结果'}</strong><span>{preview.requests_used ? `本次使用 ${preview.requests_used} 次请求 · 预估 $${preview.estimated_cost_usd.toFixed(3)}` : '来自本机资料库 · 未发送请求'}</span></div><button type="button" className="nv-icon-button" onClick={() => { setPreview(null); setSelected(new Set()); }} aria-label="关闭预览"><X size={15} /></button></div>
      {preview.items.length === 0 ? <p className="nv-tk-hint">没有取得可公开查看的作品。账号可能是私密账号，或暂时没有公开视频。</p> : <>
        <label className="nv-tk-all"><input type="checkbox" checked={allSelected} disabled={!choices.length} onChange={() => setSelected(allSelected ? new Set() : new Set(choices.map(item => item.item_id)))} />全选（{choices.length} 条可加入）</label>
        <ul className="nv-tk-items">{preview.items.map(item => {
          const cover = hiddenCovers.has(item.item_id) ? undefined : safeCoverUrl(item.cover_url);
          const meta = [item.author, durationLabel(item.duration_ms), dateLabel(item.published_at)].filter(Boolean).join(' · ');
          return <li key={item.item_id} className={selected.has(item.item_id) ? 'selected' : ''}><label>
            <input type="checkbox" checked={selected.has(item.item_id)} disabled={!selectable(item)} onChange={() => toggle(item.item_id)} aria-label={`选择：${item.title}`} />
            {cover ? <img src={cover} alt="" loading="lazy" referrerPolicy="no-referrer" onError={() => setHiddenCovers(previous => new Set(previous).add(item.item_id))} /> : <span className="nv-tk-cover" aria-hidden="true"><Video size={16} /></span>}
            <span className="nv-tk-item-text"><strong>{item.title}</strong>{meta && <span>{meta}</span>}
              <span className="nv-tk-badges">{item.has_media ? <em className="done">已下载到本机</em> : item.in_library ? <em>已在资料库</em> : null}{!item.downloadable && <em className="warn">{item.reason_message || '暂不支持下载，可保存来源'}</em>}</span></span>
          </label></li>;
        })}</ul>
        <div className="nv-tk-preview-footer"><span>已选 {selected.size} 条</span>
          {preview.kind === 'creator' && preview.has_more && preview.creator && preview.next_cursor != null && <button type="button" className="nv-button" disabled={Boolean(busy)} onClick={() => { void lookup({ mode: 'creator', text: preview.creator!.url, cursor: preview.next_cursor! }, false); }}>下一页（需确认）</button>}
          <button type="button" className="nv-button" disabled={Boolean(busy) || selected.size === 0} onClick={() => { void importSelected(false); }}>加入资料库</button>
          <button type="button" className="nv-button nv-primary" disabled={Boolean(busy) || selected.size === 0} onClick={() => { void importSelected(true); }}>{busy === 'import' ? <LoaderCircle size={13} className="nv-spin" /> : null}加入并下载</button></div>
        <p className="nv-small">加入资料库不产生请求。预览里的下载地址只暂存在本机内存中；过期后下载需要再确认一次请求。</p>
      </>}
    </div>}
  </section>;
}
