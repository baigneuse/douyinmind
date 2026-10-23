import { BookOpen, ScanLine, FolderOpen, Video } from 'lucide-react';

interface Props { onStartLogin: () => void; onTryChat: () => void; onOpenLocal: () => void; onOpenVideos: () => void; busy: boolean; }

export default function LandingPage({ onStartLogin, onTryChat, onOpenLocal, onOpenVideos, busy }: Props) {
  return (
    <div className="min-h-screen flex flex-col bg-[var(--color-bg)]">
      {/* Top Bar */}
      <header className="flex items-center justify-between px-8 py-4 bg-white/50 backdrop-blur">
        <div className="flex items-center gap-3">
          <div className="brand-mark"><BookOpen size={20} /></div>
          <span className="font-display text-lg text-[var(--color-ink)]">DouyinMind</span>
        </div>
      </header>

      {/* Hero */}
      <main className="flex-1 flex flex-col items-center justify-center text-center px-6 gap-10">
        <span className="px-5 py-2 rounded-full bg-accent-light text-accent text-xs tracking-[0.15em] uppercase">
          收集视频，整理资料，随时回看
        </span>

        <h1 className="font-display text-5xl leading-tight text-[var(--color-ink)] max-w-lg">
          把值得留住的视频<br />放在一起
        </h1>

        <p className="text-[var(--color-ink-soft)] text-base max-w-md leading-relaxed">
          粘贴抖音、Bilibili、YouTube 分享文字或媒体直链，先留住来源，
          再整理标题、标签和备注。需要离线回看时，单独下载到本机。
        </p>

        <button
          onClick={onOpenVideos}
          className="inline-flex items-center gap-3 px-8 py-3.5 rounded-xl bg-accent text-white font-medium text-base hover:bg-accent-hover transition-colors disabled:opacity-60 disabled:cursor-wait"
        >
          <Video size={20} />打开视频资料库
        </button>

        <div className="-mt-5">
          <p className="mb-4 text-xs text-[var(--color-ink-muted)]">保存链接不联网，不自动下载，也不要求登录账号。</p>
          <div className="flex flex-wrap justify-center gap-3"><button onClick={onOpenLocal} className="ui-button"><FolderOpen size={17} />本地已有字幕工具</button>
          <button onClick={onStartLogin} disabled={busy} className="ui-button"><ScanLine size={17} />{busy ? '启动中…' : '原抖音知识库 · 扫码登录'}</button></div>
          <button onClick={onTryChat} className="mt-4 text-xs text-accent underline underline-offset-4">原 AI 对话入口</button>
        </div>

        {/* Pipeline */}
        <div className="flex flex-wrap justify-center gap-5 md:gap-10 mt-6">
          {[['1', '收集', '保存来源链接'], ['2', '整理', '标签与备注'], ['3', '下载', '按需保存本机'], ['4', '回看', '原站或本地播放']].map(([n, label, desc]) => (
            <div key={n} className="flex flex-col items-center gap-2 w-20">
              <div className="w-9 h-9 rounded-full bg-accent-light flex items-center justify-center text-accent font-bold text-sm">{n}</div>
              <span className="text-sm font-semibold text-[var(--color-ink)]">{label}</span>
              <span className="text-xs text-[var(--color-ink-muted)]">{desc}</span>
            </div>
          ))}
        </div>
      </main>

      {/* Footer */}
      <footer className="py-3 text-center text-xs text-[var(--color-ink-muted)] bg-white/30">
        DouyinMind © 2026 · 本地个人视频资料库
      </footer>
    </div>
  );
}
