import { useState, useEffect, useCallback, useRef, lazy, Suspense } from 'react';
import LandingPage from './pages/LandingPage';
import Workspace from './pages/Workspace';
import LoginModal from './components/LoginModal';
import * as api from './api';

const LocalMediaWorkspace = lazy(() => import('./pages/LocalMediaWorkspace'));
const NetworkMediaLibrary = lazy(() => import('./pages/NetworkMediaLibrary'));
type AppRoute = 'videos' | 'local' | 'douyin';
const currentRoute = (): AppRoute => window.location.hash === '#local' ? 'local' : window.location.hash === '#douyin' ? 'douyin' : 'videos';

export default function App() {
  const [loggedIn, setLoggedIn] = useState(false);
  const [chatOnly, setChatOnly] = useState(false);
  const [showLogin, setShowLogin] = useState(false);
  const [loginBusy, setLoginBusy] = useState(false);
  const [route, setRoute] = useState<AppRoute>(currentRoute);
  const [networkVisited, setNetworkVisited] = useState(() => currentRoute() === 'videos');
  const localNotesDirty = useRef(false);
  const localRouteActive = useRef(route === 'local');
  const localReturnRoute = useRef<AppRoute>('videos');
  const setLocalNotesDirty = useCallback((dirty: boolean) => { localNotesDirty.current = dirty; }, []);
  const openLocal = useCallback(() => { localReturnRoute.current = route; window.location.hash = 'local'; }, [route]);
  const openVideos = useCallback(() => { window.location.hash = 'videos'; }, []);
  const openDouyin = useCallback(() => { window.location.hash = 'douyin'; }, []);

  useEffect(() => {
    const changed = () => {
      const next = currentRoute();
      if (next !== 'local' && localRouteActive.current && localNotesDirty.current
          && !window.confirm('人工笔记有未保存的修改。离开后将丢失这些草稿，仍要返回吗？')) {
        window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}#local`);
        return;
      }
      localRouteActive.current = next === 'local';
      if (next === 'videos') setNetworkVisited(true);
      setRoute(next);
    };
    window.addEventListener('hashchange', changed);
    return () => window.removeEventListener('hashchange', changed);
  }, []);

  // Poll login status
  useEffect(() => {
    if (route !== 'douyin') return;
    const check = async () => {
      try {
        const s = await api.loginStatus();
        setLoggedIn(s.status === 'logged_in');
      } catch { /* backend not ready */ }
    };
    check();
    const timer = setInterval(check, loggedIn ? 30000 : 5000);
    return () => clearInterval(timer);
  }, [loggedIn, route]);

  const handleLogin = useCallback(async () => {
    setLoginBusy(true);
    try {
      const r = await api.loginStart();
      if (r.success) {
        setShowLogin(true);
      }
    } catch (e) {
      console.error(e);
    }
    setLoginBusy(false);
  }, []);

  const handleLoginSuccess = useCallback(() => {
    setShowLogin(false);
    setLoggedIn(true);
  }, []);

  const handleLogout = useCallback(async () => {
    if (loggedIn) {
      try { await api.logout(); } catch {}
    }
    setLoggedIn(false);
    setChatOnly(false);
  }, [loggedIn]);

  return <>
    {networkVisited && <div hidden={route !== 'videos'}><Suspense fallback={<div className="min-h-screen flex items-center justify-center" role="status">正在打开视频资料库…</div>}>
      <NetworkMediaLibrary active={route === 'videos'} onOpenLocal={openLocal} onOpenDouyin={openDouyin} />
    </Suspense></div>}
    {route === 'local' && <Suspense fallback={<div className="min-h-screen flex items-center justify-center" role="status">正在打开本地资料…</div>}>
      <LocalMediaWorkspace onExit={() => { window.location.hash = localReturnRoute.current; }} onDirtyChange={setLocalNotesDirty} />
    </Suspense>}
    <div hidden={route !== 'douyin'}>
      {!loggedIn && !chatOnly ? (
      <>
        <LandingPage onStartLogin={handleLogin} onTryChat={() => setChatOnly(true)} onOpenLocal={openLocal} onOpenVideos={openVideos} busy={loginBusy} />
        {showLogin && (
          <LoginModal
            onClose={() => setShowLogin(false)}
            onSuccess={handleLoginSuccess}
          />
        )}
      </>
      ) : <Workspace onLogout={handleLogout} loggedIn={loggedIn} onOpenLocal={openLocal} onOpenVideos={openVideos} active={route === 'douyin'} />}
    </div>
  </>;
}
