/** 验证 Tauri runtime 在首个请求前注入动态端口与 boot token。 */

import { afterEach, describe, expect, it, vi } from 'vitest';

const invoke = vi.fn();

vi.mock('@tauri-apps/api/core', () => ({
  invoke,
  isTauri: () => true,
}));

describe('Tauri API runtime', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    invoke.mockReset();
    vi.resetModules();
  });

  it('使用 Rust handshake 的 loopback 端口，并为请求覆盖 boot token header', async () => {
    invoke.mockResolvedValue({
      baseUrl: 'http://127.0.0.1:43127',
      bootToken: 'runtime-token-0123456789abcdef012345',
    });
    const requestSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        code: 'success',
        data: { service: 'pageferry-api' },
      }),
    } as Response);
    const { getHealth, initializeApiRuntime } =
      await import('../../src/lib/api');

    await initializeApiRuntime();
    await getHealth();

    expect(invoke).toHaveBeenCalledWith('sidecar_connection');
    expect(requestSpy).toHaveBeenCalledWith(
      'http://127.0.0.1:43127/healthz',
      expect.objectContaining({
        headers: expect.any(Headers),
      }),
    );
    const request = requestSpy.mock.calls[0]?.[1];
    expect(new Headers(request?.headers).get('X-PageFerry-Boot-Token')).toBe(
      'runtime-token-0123456789abcdef012345',
    );
  });

  it('拒绝非 loopback endpoint，且不会退回固定 8765 端口', async () => {
    invoke.mockResolvedValue({
      baseUrl: 'https://example.com:443',
      bootToken: 'runtime-token-0123456789abcdef012345',
    });
    const requestSpy = vi.spyOn(globalThis, 'fetch');
    const { initializeApiRuntime } = await import('../../src/lib/api');

    await expect(initializeApiRuntime()).rejects.toThrow(
      'Invalid sidecar endpoint',
    );
    expect(requestSpy).not.toHaveBeenCalled();
  });
});
