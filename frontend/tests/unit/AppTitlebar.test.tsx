/** 验证跨平台 titlebar 的平台识别与 Windows 窗口控制边界。 */

import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { AppTitlebar } from '../../src/features/navigation/AppTitlebar';
import { detectDesktopPlatform } from '../../src/features/navigation/desktop-platform';

describe('AppTitlebar', () => {
  it('识别 WebView 所在桌面平台', () => {
    expect(
      detectDesktopPlatform('Mozilla/5.0 (Macintosh; Intel Mac OS X)'),
    ).toBe('macos');
    expect(
      detectDesktopPlatform('Mozilla/5.0 (Windows NT 10.0; Win64; x64)'),
    ).toBe('windows');
    expect(detectDesktopPlatform('Mozilla/5.0 (X11; Linux x86_64)')).toBe(
      'linux',
    );
  });

  it('只在启用 Windows 自绘装饰时渲染三个窗口按钮', () => {
    render(<AppTitlebar platform="windows" windowControlsEnabled />);

    expect(
      screen.getByRole('button', { name: '最小化窗口' }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: '最大化或还原窗口' }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: '关闭窗口' }),
    ).toBeInTheDocument();
  });

  it('macOS 保留系统 traffic lights，不重复渲染窗口按钮', () => {
    render(<AppTitlebar platform="macos" />);

    expect(screen.queryByLabelText('窗口控制')).not.toBeInTheDocument();
  });
});
