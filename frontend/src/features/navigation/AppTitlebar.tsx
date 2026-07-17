/** 渲染跨平台窗口拖拽带，并在 Windows 提供原生风格窗口控制。 */

import { isTauri } from '@tauri-apps/api/core';
import { getCurrentWindow } from '@tauri-apps/api/window';
import { Minus, Square, X } from 'lucide-react';

import { useI18n } from '@/i18n/i18n';

import type { DesktopPlatform } from './desktop-platform';

interface AppTitlebarProps {
  platform: DesktopPlatform;
  windowControlsEnabled?: boolean;
}

/** 请求 Tauri 最小化当前窗口。 */
function minimizeWindow(): void {
  void getCurrentWindow().minimize();
}

/** 请求 Tauri 在最大化与还原之间切换当前窗口。 */
function toggleMaximizeWindow(): void {
  void getCurrentWindow().toggleMaximize();
}

/** 请求 Tauri 关闭当前窗口。 */
function closeWindow(): void {
  void getCurrentWindow().close();
}

/** 横跨整个窗口渲染 titlebar，避免 sidebar 与系统窗口按钮争用空间。 */
export function AppTitlebar({
  platform,
  windowControlsEnabled = platform === 'windows' && isTauri(),
}: AppTitlebarProps) {
  const { t } = useI18n();

  return (
    <header className="app-titlebar" aria-label={t('titlebar.label')}>
      <div className="titlebar-drag-surface" data-tauri-drag-region />

      {windowControlsEnabled ? (
        <div
          className="titlebar-window-controls"
          aria-label={t('titlebar.controls')}
        >
          <button
            className="titlebar-window-button"
            type="button"
            aria-label={t('titlebar.minimize')}
            onClick={minimizeWindow}
          >
            <Minus aria-hidden="true" size={16} strokeWidth={1.5} />
          </button>
          <button
            className="titlebar-window-button"
            type="button"
            aria-label={t('titlebar.maximize')}
            onClick={toggleMaximizeWindow}
          >
            <Square aria-hidden="true" size={11} strokeWidth={1.6} />
          </button>
          <button
            className="titlebar-window-button titlebar-window-button--close"
            type="button"
            aria-label={t('titlebar.close')}
            onClick={closeWindow}
          >
            <X aria-hidden="true" size={16} strokeWidth={1.5} />
          </button>
        </div>
      ) : null}
    </header>
  );
}
