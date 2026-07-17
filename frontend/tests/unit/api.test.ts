/** 验证本地 API client 对空响应和 provider 顶层错误体的处理。 */

import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  ApiError,
  configureProvider,
  createCustomProvider,
  deleteProvider,
} from '../../src/lib/api';

describe('API client', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('删除 provider 时不解析 204 空响应', async () => {
    const parseJson = vi.fn();
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      status: 204,
      json: parseJson,
    } as unknown as Response);

    await expect(deleteProvider('deepseek')).resolves.toBeUndefined();
    expect(parseJson).not.toHaveBeenCalled();
  });

  it('保留 provider 顶层错误码供设置面板翻译', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: false,
      status: 401,
      json: async () => ({ code: 'key', message: 'Invalid key.' }),
    } as Response);

    const request = configureProvider('deepseek', {
      api_key: 'sk-test-only',
      enabled_model_ids: ['deepseek-v4-flash'],
      default_model_id: 'deepseek-v4-flash',
    });
    await expect(request).rejects.toBeInstanceOf(ApiError);
    await expect(request).rejects.toMatchObject({
      code: 'key',
      status: 401,
      message: 'Invalid key.',
    });
  });

  it('创建 OpenAI-compatible 自定义供应商并补齐公开状态', async () => {
    const requestSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      status: 201,
      json: async () => ({
        provider_id: 'custom-a1',
        display_name: '内部网关',
        protocol: 'openai',
        is_custom: true,
        base_url: 'https://llm.example.com/v1',
        base_url_editable: true,
        deletable: true,
      }),
    } as Response);

    await expect(
      createCustomProvider({
        display_name: '内部网关',
        base_url: 'https://llm.example.com/v1',
      }),
    ).resolves.toMatchObject({
      provider_id: 'custom-a1',
      configured: false,
      is_custom: true,
      base_url: 'https://llm.example.com/v1',
    });
    expect(requestSpy).toHaveBeenCalledWith(
      'http://127.0.0.1:8765/api/v1/providers/custom',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          display_name: '内部网关',
          base_url: 'https://llm.example.com/v1',
        }),
      }),
    );
  });
});
