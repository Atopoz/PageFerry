/** 验证本地 API client 对空响应和 provider 顶层错误体的处理。 */

import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  ApiError,
  configureProvider,
  createCustomProvider,
  createProviderModel,
  deleteProvider,
  getProviderApiKey,
  probeProvider,
  setProviderActive,
  setProviderModelEnabled,
  syncProviderModels,
  updateProviderModelSettings,
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

  it('首次配置显式发送整组启用标记，不伪造逐项 enabled 列表', async () => {
    const requestSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        provider_id: 'deepseek',
        configured: true,
        active: true,
        enabled_model_ids: ['deepseek-v4-flash', 'deepseek-v4-pro'],
        default_model_id: 'deepseek-v4-flash',
        probe_status: 'succeeded',
      }),
    } as Response);

    await configureProvider('deepseek', {
      api_key: 'sk-test-only',
      enable_all_models: true,
      default_model_id: 'deepseek-v4-flash',
    });

    expect(requestSpy).toHaveBeenCalledWith(
      'http://127.0.0.1:8765/api/v1/providers/deepseek',
      expect.objectContaining({
        method: 'PUT',
        body: JSON.stringify({
          api_key: 'sk-test-only',
          enable_all_models: true,
          default_model_id: 'deepseek-v4-flash',
        }),
      }),
    );
  });

  it('纯检测安全编码 provider id，并原样返回具体模型与延迟', async () => {
    const requestSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        provider_id: 'custom/gateway',
        model_id: 'org/translate-v2',
        display_name: 'Translate V2',
        latency_ms: 84,
      }),
    } as Response);

    await expect(
      probeProvider('custom/gateway', {
        api_key: 'sk-probe-only',
        base_url: 'https://gateway.example.com/v1',
        model_id: 'org/translate-v2',
      }),
    ).resolves.toEqual({
      provider_id: 'custom/gateway',
      model_id: 'org/translate-v2',
      display_name: 'Translate V2',
      latency_ms: 84,
    });
    expect(requestSpy).toHaveBeenCalledWith(
      'http://127.0.0.1:8765/api/v1/providers/custom%2Fgateway/probe',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          api_key: 'sk-probe-only',
          base_url: 'https://gateway.example.com/v1',
          model_id: 'org/translate-v2',
        }),
      }),
    );
  });

  it('读取真实 API Key 时安全编码 provider id 并禁用 HTTP cache', async () => {
    const requestSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ api_key: 'sk-saved-existing' }),
    } as Response);

    await expect(getProviderApiKey('custom/gateway')).resolves.toBe(
      'sk-saved-existing',
    );
    expect(requestSpy).toHaveBeenCalledWith(
      'http://127.0.0.1:8765/api/v1/providers/custom%2Fgateway/api-key',
      expect.objectContaining({ cache: 'no-store' }),
    );
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
      active: false,
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

  it('供应商启停使用非破坏 active endpoint，并保留后端权威状态', async () => {
    const requestSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        provider_id: 'deepseek',
        configured: true,
        active: false,
        probe_status: 'succeeded',
      }),
    } as Response);

    await expect(setProviderActive('deepseek', false)).resolves.toMatchObject({
      provider_id: 'deepseek',
      configured: true,
      active: false,
    });
    expect(requestSpy).toHaveBeenCalledWith(
      'http://127.0.0.1:8765/api/v1/providers/deepseek/active',
      expect.objectContaining({
        method: 'PUT',
        body: JSON.stringify({ active: false }),
      }),
    );
  });

  it('模型启停安全编码 model id，并接收可能变化的默认模型', async () => {
    const requestSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        provider_id: 'custom-a1',
        configured: true,
        active: true,
        enabled_model_ids: ['fallback'],
        default_model_id: 'fallback',
        probe_status: 'succeeded',
      }),
    } as Response);

    await expect(
      setProviderModelEnabled('custom-a1', 'org/translate-v2', false),
    ).resolves.toMatchObject({
      enabled_model_ids: ['fallback'],
      default_model_id: 'fallback',
    });
    expect(requestSpy).toHaveBeenCalledWith(
      'http://127.0.0.1:8765/api/v1/providers/custom-a1/models/org%2Ftranslate-v2/enabled',
      expect.objectContaining({
        method: 'PUT',
        body: JSON.stringify({ enabled: false }),
      }),
    );
  });

  it('模型设置使用 override payload，并安全编码带斜杠的模型 id', async () => {
    const requestSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        id: 'org/translate-v2',
        source: 'remote',
        available: true,
        reasoning_policy: 'medium',
        reasoning_policy_override: 'medium',
        supported_reasoning_policies: ['provider_default', 'medium'],
        per_job_concurrency: 8,
        per_job_concurrency_override: 8,
        global_concurrency: 20,
        global_concurrency_override: 20,
      }),
    } as Response);

    await updateProviderModelSettings('custom-a1', 'org/translate-v2', {
      reasoning_policy_override: 'medium',
      per_job_concurrency_override: 8,
      global_concurrency_override: 20,
    });

    expect(requestSpy).toHaveBeenCalledWith(
      'http://127.0.0.1:8765/api/v1/providers/custom-a1/models/org%2Ftranslate-v2/settings',
      expect.objectContaining({
        method: 'PUT',
        body: JSON.stringify({
          reasoning_policy_override: 'medium',
          per_job_concurrency_override: 8,
          global_concurrency_override: 20,
        }),
      }),
    );
  });

  it('手动添加模型使用 provider-scoped POST，模型 id 保留在 JSON body', async () => {
    const requestSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      status: 201,
      json: async () => ({
        id: 'org/translate-v2',
        display_name: 'Translate V2',
        source: 'manual',
        enabled: false,
        available: true,
        reasoning_policy: null,
        reasoning_policy_override: null,
        supported_reasoning_policies: [],
        per_job_concurrency: 6,
        per_job_concurrency_override: null,
        global_concurrency: 15,
        global_concurrency_override: null,
      }),
    } as Response);

    await expect(
      createProviderModel('custom-a1', {
        model_id: 'org/translate-v2',
        display_name: 'Translate V2',
      }),
    ).resolves.toMatchObject({
      id: 'org/translate-v2',
      source: 'manual',
      enabled: false,
    });

    expect(requestSpy).toHaveBeenCalledWith(
      'http://127.0.0.1:8765/api/v1/providers/custom-a1/models',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          model_id: 'org/translate-v2',
          display_name: 'Translate V2',
        }),
      }),
    );
  });

  it('已配置 provider 的模型同步走持久化 sync endpoint', async () => {
    const requestSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        provider_id: 'deepseek',
        models: [],
        last_synced_at: '2026-07-17T02:00:00Z',
        added: 0,
        restored: 0,
        unavailable: 0,
        unchanged: 0,
      }),
    } as Response);

    await syncProviderModels('deepseek');

    expect(requestSpy).toHaveBeenCalledWith(
      'http://127.0.0.1:8765/api/v1/providers/deepseek/models/sync',
      { method: 'POST' },
    );
  });
});
