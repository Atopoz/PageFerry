/** 定义 PageFerry 工作页面与底部通用设置入口的紧凑桌面侧边栏。 */

import {
  CircleDashed,
  Cloud,
  FileText,
  History,
  PanelLeftClose,
  PanelLeftOpen,
  Settings,
  WifiOff,
} from 'lucide-react';
import type { PointerEvent } from 'react';

import { useI18n } from '@/i18n/i18n';

export type AppRoute = 'translate' | 'history' | 'providers' | 'settings';
export type ServiceState = 'checking' | 'connected' | 'offline';

interface AppSidebarProps {
  activeRoute: AppRoute;
  collapsed: boolean;
  serviceState: ServiceState;
  onNavigate: (route: AppRoute) => void;
  onToggleCollapsed: () => void;
}

/** 渲染一级页面导航；收起后仍保留 tooltip 与可访问名称。 */
export function AppSidebar({
  activeRoute,
  collapsed,
  serviceState,
  onNavigate,
  onToggleCollapsed,
}: AppSidebarProps) {
  const { t } = useI18n();
  const ServiceIcon = serviceState === 'offline' ? WifiOff : CircleDashed;
  const routeGroups = [
    [
      { route: 'translate', label: t('sidebar.translate'), icon: FileText },
      { route: 'history', label: t('sidebar.history'), icon: History },
    ],
    [{ route: 'providers', label: t('sidebar.providers'), icon: Cloud }],
  ] as const;
  const settingsLabel = t('sidebar.settings');
  const serviceLabel =
    serviceState === 'offline' ? t('service.offline') : t('service.connecting');

  /** 点击 wordmark 时回到文件翻译工作区。 */
  function openTranslationWorkspace() {
    onNavigate('translate');
  }

  /** 鼠标操作结束后移走焦点，避免展开按钮被误读成持续激活；键盘焦点仍保留。 */
  function releasePointerFocus(event: PointerEvent<HTMLButtonElement>) {
    event.currentTarget.blur();
  }

  return (
    <aside className="app-sidebar" aria-label={t('sidebar.primaryNavigation')}>
      <div className="sidebar-brand-region">
        <button
          className="sidebar-wordmark-button"
          type="button"
          aria-label={t('sidebar.backToTranslate')}
          hidden={collapsed}
          onClick={openTranslationWorkspace}
        >
          <span className="sidebar-wordmark" aria-hidden="true">
            <span className="wordmark-page">Page</span>
            <span className="wordmark-ferry">Ferry</span>
            <svg
              className="wordmark-route"
              viewBox="0 0 52 7"
              aria-hidden="true"
              focusable="false"
            >
              <path d="M1 4.8C11 1.2 18 6.4 29 3.5C36 1.7 41 1.8 48 2.8" />
              <circle cx="50" cy="2.9" r="1.5" />
            </svg>
          </span>
        </button>
        <button
          className="sidebar-toggle"
          type="button"
          aria-label={collapsed ? t('sidebar.expand') : t('sidebar.collapse')}
          aria-expanded={!collapsed}
          onClick={onToggleCollapsed}
          onPointerUp={releasePointerFocus}
        >
          {collapsed ? (
            <PanelLeftOpen aria-hidden="true" size={18} strokeWidth={1.75} />
          ) : (
            <PanelLeftClose aria-hidden="true" size={18} strokeWidth={1.75} />
          )}
        </button>
      </div>

      <nav className="sidebar-nav" aria-label={t('sidebar.workspace')}>
        {routeGroups.map((group, groupIndex) => (
          <div
            className={`sidebar-nav-group ${groupIndex === 1 ? 'sidebar-nav-group--services' : ''}`}
            key={group[0].route}
          >
            {group.map((item) => {
              const Icon = item.icon;
              const isActive = item.route === activeRoute;
              return (
                <button
                  className={`sidebar-nav-item ${isActive ? 'sidebar-nav-item--active' : ''}`}
                  type="button"
                  key={item.route}
                  title={collapsed ? item.label : undefined}
                  aria-current={isActive ? 'page' : undefined}
                  onClick={() => onNavigate(item.route)}
                >
                  <Icon aria-hidden="true" size={18} strokeWidth={1.8} />
                  <span>{item.label}</span>
                </button>
              );
            })}
          </div>
        ))}
      </nav>

      <div className="sidebar-footer">
        {serviceState !== 'connected' ? (
          <div
            className={`sidebar-service sidebar-service--${serviceState}`}
            title={collapsed ? serviceLabel : undefined}
            role="status"
          >
            <ServiceIcon
              className={serviceState === 'checking' ? 'spin' : ''}
              aria-hidden="true"
              size={14}
            />
            <span>{serviceLabel}</span>
          </div>
        ) : null}

        <nav
          className="sidebar-nav sidebar-nav--utility"
          aria-label={settingsLabel}
        >
          <button
            className={`sidebar-nav-item ${activeRoute === 'settings' ? 'sidebar-nav-item--active' : ''}`}
            type="button"
            title={collapsed ? settingsLabel : undefined}
            aria-current={activeRoute === 'settings' ? 'page' : undefined}
            onClick={() => onNavigate('settings')}
          >
            <Settings aria-hidden="true" size={18} strokeWidth={1.8} />
            <span>{settingsLabel}</span>
          </button>
        </nav>
      </div>
    </aside>
  );
}
