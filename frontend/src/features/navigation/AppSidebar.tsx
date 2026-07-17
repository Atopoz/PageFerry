/** 定义 PageFerry 三个一级页面的紧凑桌面侧边栏。 */

import {
  Boxes,
  CircleDashed,
  FileText,
  History,
  PanelLeftClose,
  PanelLeftOpen,
  WifiOff,
} from 'lucide-react';
import type { PointerEvent } from 'react';

export type AppRoute = 'translate' | 'history' | 'providers';
export type ServiceState = 'checking' | 'connected' | 'offline';

interface AppSidebarProps {
  activeRoute: AppRoute;
  collapsed: boolean;
  activeProviderCount: number;
  serviceState: ServiceState;
  serviceLabel: string;
  onNavigate: (route: AppRoute) => void;
  onToggleCollapsed: () => void;
}

const routeItems = [
  { route: 'translate', label: '文件翻译', icon: FileText },
  { route: 'history', label: '历史记录', icon: History },
  { route: 'providers', label: '模型服务', icon: Boxes },
] as const;

/** 渲染一级页面导航；收起后仍保留 tooltip 与可访问名称。 */
export function AppSidebar({
  activeRoute,
  collapsed,
  activeProviderCount,
  serviceState,
  serviceLabel,
  onNavigate,
  onToggleCollapsed,
}: AppSidebarProps) {
  const ServiceIcon = serviceState === 'offline' ? WifiOff : CircleDashed;

  /** 点击 wordmark 时回到文件翻译工作区。 */
  function openTranslationWorkspace() {
    onNavigate('translate');
  }

  /** 鼠标操作结束后移走焦点，避免展开按钮被误读成持续激活；键盘焦点仍保留。 */
  function releasePointerFocus(event: PointerEvent<HTMLButtonElement>) {
    event.currentTarget.blur();
  }

  return (
    <aside className="app-sidebar" aria-label="主要导航">
      <div className="sidebar-brand-region">
        <button
          className="sidebar-wordmark-button"
          type="button"
          aria-label="返回文件翻译"
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
          aria-label={collapsed ? '展开侧边栏' : '收起侧边栏'}
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

      <nav className="sidebar-nav" aria-label="工作区">
        {routeItems.map((item) => {
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
              {item.route === 'providers' && activeProviderCount > 0 ? (
                <small className="sidebar-count">{activeProviderCount}</small>
              ) : null}
            </button>
          );
        })}
      </nav>

      <div
        className={`sidebar-service sidebar-service--${serviceState}`}
        title={collapsed ? serviceLabel : undefined}
        role={serviceState === 'connected' ? undefined : 'status'}
      >
        {serviceState === 'connected' ? null : (
          <ServiceIcon
            className={serviceState === 'checking' ? 'spin' : ''}
            aria-hidden="true"
            size={14}
          />
        )}
        <span>{serviceLabel}</span>
      </div>
    </aside>
  );
}
