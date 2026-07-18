/** 验证 PDF resource dialog 的进度、动作与稳定失败码文案。 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { PdfResourceDialog } from '../../src/features/pdf-resources/PdfResourceDialog';
import { I18nProvider } from '../../src/i18n/I18nProvider';
import type { PdfResourceStatus } from '../../src/lib/api';

const mebibyte = 1024 * 1024;

/** 构造两项资源组成的完整 UI 状态。 */
function resourceStatus(
  state: PdfResourceStatus['state'],
  errorCode: string | null = null,
): PdfResourceStatus {
  const completedBytes = state === 'ready' ? 150 * mebibyte : 64 * mebibyte;
  return {
    pack_revision: '2026.07.18.1',
    state,
    total_bytes: 150 * mebibyte,
    completed_bytes: completedBytes,
    current_asset_id: state === 'downloading' ? 'pp-doclayout-v3-onnx' : null,
    error_code: errorCode,
    resources: [
      {
        pack: 'layout',
        size_bytes: 128 * mebibyte,
        completed_bytes: Math.min(completedBytes, 128 * mebibyte),
        ready: state === 'ready',
      },
      {
        pack: 'fonts-common-zh-cn',
        size_bytes: 22 * mebibyte,
        completed_bytes: state === 'ready' ? 22 * mebibyte : 0,
        ready: state === 'ready',
      },
    ],
  };
}

/** 用中文 locale 渲染受控 Dialog，并返回动作 spy 与状态更新入口。 */
function renderDialog(
  status: PdfResourceStatus | null,
  action: 'check' | 'install' | 'cancel' | null = null,
  actionFailed = false,
) {
  const actions = {
    onOpenChange: vi.fn(),
    onRefresh: vi.fn(),
    onInstall: vi.fn(),
    onCancel: vi.fn(),
  };

  /** 保持同一 Dialog 实例，用于验证跨状态按钮节点的替换边界。 */
  function dialog(
    nextStatus: PdfResourceStatus | null,
    nextAction: 'check' | 'install' | 'cancel' | null = null,
    nextActionFailed = false,
  ) {
    return (
      <I18nProvider>
        <PdfResourceDialog
          open
          status={nextStatus}
          action={nextAction}
          actionFailed={nextActionFailed}
          {...actions}
        />
      </I18nProvider>
    );
  }

  const view = render(dialog(status, action, actionFailed));
  return {
    ...actions,
    rerenderDialog: (
      nextStatus: PdfResourceStatus | null,
      nextAction: 'check' | 'install' | 'cancel' | null = null,
      nextActionFailed = false,
    ) => view.rerender(dialog(nextStatus, nextAction, nextActionFailed)),
  };
}

describe('PdfResourceDialog', () => {
  beforeEach(() => {
    window.localStorage.setItem('pageferry.ui-locale.v1', 'zh-CN');
  });

  it('展示两项产品资源、大小与可访问总体进度', () => {
    const actions = renderDialog(resourceStatus('downloading'));

    expect(screen.getByText('PDF 布局检测模型')).toBeInTheDocument();
    expect(screen.getByText('简体中文基础字体')).toBeInTheDocument();
    expect(screen.getByText('64.0 MB / 128.0 MB')).toBeInTheDocument();
    expect(screen.getByText('22.0 MB')).toBeInTheDocument();
    expect(
      screen.getByRole('progressbar', { name: 'PDF 资源下载进度' }),
    ).toHaveAttribute('aria-valuetext', '64.0 MB / 150.0 MB');
    expect(screen.queryByText('稍后再说')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '取消下载' }));
    expect(actions.onCancel).toHaveBeenCalledTimes(1);
  });

  it('cancelling 时继续显示进度并禁止重复取消', () => {
    renderDialog(resourceStatus('cancelling'));

    expect(screen.getAllByText('正在取消…').length).toBeGreaterThan(0);
    expect(screen.getByRole('button', { name: '正在取消' })).toBeDisabled();
    expect(screen.queryByRole('button', { name: '重新下载' })).toBeNull();
  });

  it('打开时聚焦弹窗内容，不给“稍后再说”强制加上 focus 环', async () => {
    renderDialog(resourceStatus('missing'));

    const dialog = screen.getByRole('dialog', { name: '准备 PDF 翻译' });
    const laterButton = screen.getByRole('button', { name: '稍后再说' });
    await waitFor(() => expect(dialog).toHaveFocus());
    expect(laterButton).not.toHaveFocus();
  });

  it('切换动作时替换按钮节点，不把上一动作的 focus 与 active 外观带给新按钮', () => {
    const actions = renderDialog(resourceStatus('missing'));
    const laterButton = screen.getByRole('button', { name: '稍后再说' });
    const installButton = screen.getByRole('button', {
      name: '下载并启用 PDF',
    });

    expect(laterButton).toHaveAttribute('data-variant', 'outline');
    expect(installButton).toHaveAttribute('data-variant', 'default');
    installButton.focus();
    expect(installButton).toHaveFocus();

    actions.rerenderDialog(resourceStatus('downloading'));
    expect(
      screen.getByRole('button', { name: '关闭 PDF 资源安装' }),
    ).toHaveAttribute('data-variant', 'ghost');
    const cancelButton = screen.getByRole('button', { name: '取消下载' });
    expect(cancelButton).toHaveAttribute('data-variant', 'destructive');
    expect(cancelButton).not.toBe(installButton);
    expect(installButton).not.toBeInTheDocument();

    cancelButton.focus();
    actions.rerenderDialog(resourceStatus('downloading'), 'cancel');
    const cancellingButton = screen.getByRole('button', { name: '正在取消' });
    expect(cancellingButton).toHaveAttribute('data-variant', 'destructive');
    expect(cancellingButton).toBeDisabled();
    expect(cancellingButton).not.toBe(cancelButton);
    expect(cancelButton).not.toBeInTheDocument();

    actions.rerenderDialog(resourceStatus('cancelled'));
    const retryButton = screen.getByRole('button', { name: '重新下载' });
    expect(retryButton).toHaveAttribute('data-variant', 'default');
    expect(retryButton).not.toBe(cancellingButton);
    expect(cancellingButton).not.toBeInTheDocument();

    retryButton.focus();
    actions.rerenderDialog(resourceStatus('cancelled'), 'install');
    const preparingButton = screen.getByRole('button', { name: '正在准备' });
    expect(preparingButton).toBeDisabled();
    expect(preparingButton).not.toBe(retryButton);
    expect(retryButton).not.toBeInTheDocument();

    actions.rerenderDialog(null, null, true);
    const refreshButton = screen.getByRole('button', { name: '重新检查' });
    expect(refreshButton).toHaveAttribute('data-variant', 'default');
    refreshButton.focus();
    actions.rerenderDialog(null, 'check', true);
    const checkingButton = screen.getByRole('button', {
      name: '正在检查本机资源…',
    });
    expect(checkingButton).toBeDisabled();
    expect(checkingButton).not.toBe(refreshButton);
    expect(refreshButton).not.toBeInTheDocument();

    actions.rerenderDialog(resourceStatus('ready'));
    expect(screen.queryAllByRole('button')).toHaveLength(0);
  });

  it.each([
    ['insufficient_disk_space', '磁盘空间不足，请清理后重试。'],
    ['download_failed', '下载失败，请检查网络后重试。'],
    ['integrity_check_failed', '资源校验失败，请重新下载。'],
    ['filesystem_error', '无法写入本机资源目录，请检查磁盘权限。'],
  ])('按失败码 %s 展示可操作提示', (errorCode, message) => {
    const actions = renderDialog(resourceStatus('failed', errorCode));

    expect(screen.getByRole('alert')).toHaveTextContent(message);
    fireEvent.click(screen.getByRole('button', { name: '重新下载' }));
    expect(actions.onInstall).toHaveBeenCalledTimes(1);
  });

  it.each([
    [
      'insufficient_disk_space',
      'Not enough disk space. Free some space, then try again.',
    ],
    [
      'download_failed',
      'The download failed. Check your network, then try again.',
    ],
    [
      'integrity_check_failed',
      'Resource verification failed. Download the resources again.',
    ],
    [
      'filesystem_error',
      'The local resource directory could not be written. Check disk permissions.',
    ],
  ])('英文界面按失败码 %s 展示可操作提示', (errorCode, message) => {
    window.localStorage.setItem('pageferry.ui-locale.v1', 'en');
    renderDialog(resourceStatus('failed', errorCode));

    expect(screen.getByRole('alert')).toHaveTextContent(message);
  });

  it('英文初始 CTA 使用已确认的产品文案', () => {
    window.localStorage.setItem('pageferry.ui-locale.v1', 'en');
    renderDialog(resourceStatus('missing'));

    expect(
      screen.getByRole('button', { name: 'Download and enable PDF' }),
    ).toBeInTheDocument();
    expect(screen.getByText('PDF layout detection model')).toBeInTheDocument();
  });
});
