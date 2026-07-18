/** 创建 PageFerry 的 React root，并在开发期保留 StrictMode 检查。 */

import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import { App } from './App';
import logoUrl from './assets/logo.svg';
import { resolveInitialUiLocale, type UiLocale } from './i18n/i18n';
import { initializeApiRuntime } from './lib/api';
import './styles/global.css';

const root = document.getElementById('root');

if (root === null) {
  throw new Error('PageFerry root element is missing');
}

const reactRoot = createRoot(root);

/** 按用户保存的界面偏好与系统语言决定启动阶段文案。 */
function usesChineseLocale(locale: UiLocale) {
  return locale === 'zh-CN';
}

/** sidecar 初始化期间立即给出明确、但不伪造百分比的可见状态。 */
function renderStartupStatus() {
  const locale = resolveInitialUiLocale();
  const usesChinese = usesChineseLocale(locale);
  document.documentElement.lang = locale;
  reactRoot.render(
    <main className="startup-state" role="status" aria-live="polite">
      <section className="startup-panel">
        <img className="startup-logo" src={logoUrl} alt="" />
        <strong>
          {usesChinese ? '正在启动 PageFerry…' : 'Starting PageFerry…'}
        </strong>
        <span>
          {usesChinese
            ? '正在准备本地文档引擎，首次启动可能需要一点时间。'
            : 'Preparing the local document engine. The first launch may take a moment.'}
        </span>
        <span
          className="startup-progress"
          role="progressbar"
          aria-label={usesChinese ? '启动中' : 'Starting'}
        >
          <i />
        </span>
      </section>
    </main>,
  );
}

/** 渲染已经完成 sidecar handshake 的主应用。 */
function renderApp() {
  reactRoot.render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}

/** sidecar 无法启动时给出紧凑、不会误导用户继续操作的阻断页。 */
function renderStartupFailure() {
  const locale = resolveInitialUiLocale();
  const usesChinese = usesChineseLocale(locale);
  document.documentElement.lang = locale;
  reactRoot.render(
    <main className="startup-state startup-state--failure" role="alert">
      <section className="startup-panel">
        <img className="startup-logo" src={logoUrl} alt="" />
        <strong>
          {usesChinese
            ? 'PageFerry 本地服务未能启动'
            : 'PageFerry could not start its local service'}
        </strong>
        <span>
          {usesChinese ? '请退出应用后重试。' : 'Quit the app and try again.'}
        </span>
      </section>
    </main>,
  );
}

renderStartupStatus();
void initializeApiRuntime().then(renderApp).catch(renderStartupFailure);
