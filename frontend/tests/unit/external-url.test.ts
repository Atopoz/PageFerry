/** 验证外链只接受 HTTP(S)，并按 runtime 选择 Tauri opener 或浏览器 fallback。 */

import { openUrl } from '@tauri-apps/plugin-opener';
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  normalizeExternalHttpUrl,
  openExternalHttpUrl,
  safeExternalHttpUrl,
} from '../../src/lib/external-url';

vi.mock('@tauri-apps/plugin-opener', () => ({
  openUrl: vi.fn(),
}));

type TauriTestWindow = Window & { __TAURI_INTERNALS__?: unknown };

/** 为单个测试切换 Tauri runtime 标记。 */
function setTauriRuntime(enabled: boolean): void {
  if (enabled) {
    Object.defineProperty(window, '__TAURI_INTERNALS__', {
      configurable: true,
      value: {},
    });
    return;
  }
  delete (window as TauriTestWindow).__TAURI_INTERNALS__;
}

describe('external URL', () => {
  afterEach(() => {
    setTauriRuntime(false);
    vi.mocked(openUrl).mockReset();
    vi.restoreAllMocks();
  });

  it('Tauri WebView 使用系统 opener 打开 HTTPS 页面', async () => {
    setTauriRuntime(true);
    vi.mocked(openUrl).mockResolvedValue();

    await openExternalHttpUrl('https://platform.deepseek.com/api-docs/');

    expect(openUrl).toHaveBeenCalledWith(
      'https://platform.deepseek.com/api-docs/',
    );
  });

  it('普通浏览器使用带隔离参数的新页 fallback', async () => {
    setTauriRuntime(false);
    const open = vi.spyOn(window, 'open').mockReturnValue({} as Window);

    await openExternalHttpUrl('http://localhost:1420/docs');

    expect(open).toHaveBeenCalledWith(
      'http://localhost:1420/docs',
      '_blank',
      'noopener,noreferrer',
    );
    expect(openUrl).not.toHaveBeenCalled();
  });

  it('拒绝任意 scheme 和包含账号信息的 URL', async () => {
    const open = vi.spyOn(window, 'open').mockReturnValue({} as Window);

    for (const unsafeUrl of [
      'javascript:alert(1)',
      'file:///tmp/provider-key',
      'https://user:secret@example.com/docs',
    ]) {
      expect(safeExternalHttpUrl(unsafeUrl)).toBeNull();
      await expect(openExternalHttpUrl(unsafeUrl)).rejects.toThrow();
    }

    expect(open).not.toHaveBeenCalled();
    expect(openUrl).not.toHaveBeenCalled();
  });

  it('对合法地址返回稳定 href，并报告浏览器 popup 拦截', async () => {
    expect(normalizeExternalHttpUrl('https://example.com')).toBe(
      'https://example.com/',
    );
    vi.spyOn(window, 'open').mockReturnValue(null);

    await expect(openExternalHttpUrl('https://example.com')).rejects.toThrow(
      '浏览器阻止了新窗口',
    );
  });
});
