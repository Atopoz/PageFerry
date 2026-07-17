/** 验证 provider 二级列表默认陈列 preset，并支持轻量 custom provider 创建。 */

import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  ProviderPage,
  type ProviderSaveInput,
} from '../../src/features/providers/ProviderPage';
import {
  ApiError,
  type CreateCustomProviderInput,
  type CreateProviderModelInput,
  type ModelCatalog,
  type ProbeProviderInput,
  type ProviderModelStatus,
  type ProviderModelSync,
  type ProviderProbeResult,
  type ProviderStatus,
  type UpdateProviderModelSettingsInput,
} from '../../src/lib/api';

interface ProviderPageHarnessProps {
  onCreate?: (input: CreateCustomProviderInput) => void;
  onAddModel?: (providerId: string, input: CreateProviderModelInput) => void;
}

const presetCatalog: ModelCatalog = {
  schema_version: 1,
  catalog_version: 'test',
  providers: [
    {
      id: 'deepseek',
      display_name: 'DeepSeek',
      protocol: 'openai',
      available: true,
      base_url_editable: true,
      supports_model_sync: true,
      default_base_url: 'https://api.deepseek.com',
    },
  ],
  models: [
    {
      id: 'deepseek-v4-flash',
      display_name: 'DeepSeek V4 Flash',
      capabilities: ['text', 'translation'],
    },
  ],
  provider_models: [
    {
      provider_id: 'deepseek',
      model_id: 'deepseek-v4-flash',
      upstream_model_id: 'deepseek-v4-flash',
      enabled_by_default: true,
    },
  ],
};

/** 构造已激活的 DeepSeek 状态，供 Base URL override 测试使用。 */
function presetProviderStatus(overridden = false): ProviderStatus {
  return {
    provider_id: 'deepseek',
    display_name: 'DeepSeek',
    protocol: 'openai',
    is_custom: false,
    base_url: overridden
      ? 'https://gateway.example.com/deepseek'
      : 'https://api.deepseek.com',
    base_url_overridden: overridden,
    base_url_editable: true,
    deletable: false,
    available: true,
    configured: true,
    active: true,
    supports_model_sync: true,
    enabled_model_ids: ['deepseek-v4-flash'],
    default_model_id: 'deepseek-v4-flash',
    model_count: 1,
    models: [
      {
        id: 'deepseek-v4-flash',
        display_name: 'DeepSeek V4 Flash',
        source: 'catalog',
        available: true,
        enabled: true,
        reasoning_policy: 'off',
        reasoning_policy_override: null,
        supported_reasoning_policies: [
          'provider_default',
          'off',
          'high',
          'max',
        ],
        per_job_concurrency: 6,
        per_job_concurrency_override: null,
        global_concurrency: 15,
        global_concurrency_override: null,
      },
    ],
    probe_status: 'succeeded',
    probe_error_code: null,
    latency_ms: 120,
    last_probed_at: '2026-07-17T00:00:00Z',
    last_synced_at: null,
  };
}

/** 构造尚未 probe 的手动模型，确保 UI 不会把登记动作误当成启用。 */
function manualModelStatus(
  input: CreateProviderModelInput = { model_id: 'org/translate-v2' },
): ProviderModelStatus {
  return {
    id: input.model_id,
    display_name: input.display_name ?? input.model_id,
    source: 'manual',
    available: true,
    enabled: false,
    reasoning_policy: null,
    reasoning_policy_override: null,
    supported_reasoning_policies: [],
    per_job_concurrency: 6,
    per_job_concurrency_override: null,
    global_concurrency: 15,
    global_concurrency_override: null,
  };
}

/** 模拟从 Keychain 读取真实密钥，默认保持稳定引用避免测试 harness 重渲染后重复请求。 */
async function loadSavedApiKey(): Promise<string> {
  return 'sk-saved-existing';
}

/** 直接渲染 preset 配置页，并让测试接管保存 callback。 */
function renderPresetProvider(
  status: ProviderStatus,
  onSave: (input: ProviderSaveInput) => Promise<ProviderStatus>,
  options: {
    onAddModel?: (
      providerId: string,
      input: CreateProviderModelInput,
    ) => Promise<ProviderModelStatus>;
    onLoadApiKey?: (
      providerId: string,
      signal?: AbortSignal,
    ) => Promise<string>;
    onProbe?: (
      providerId: string,
      input: ProbeProviderInput,
    ) => Promise<ProviderProbeResult>;
    additionalProviders?: ProviderStatus[];
    onSyncModels?: (providerId: string) => Promise<ProviderModelSync>;
    onProviderActiveChange?: (
      providerId: string,
      active: boolean,
    ) => Promise<ProviderStatus>;
    onModelEnabledChange?: (
      providerId: string,
      modelId: string,
      enabled: boolean,
    ) => Promise<ProviderStatus>;
    onSaveModelSettings?: (
      providerId: string,
      modelId: string,
      input: UpdateProviderModelSettingsInput,
    ) => Promise<ProviderModelStatus>;
  } = {},
): void {
  /** 模拟 App 用 mutation 返回的完整 ProviderStatus 回写唯一状态。 */
  function PresetProviderHarness() {
    const [currentStatus, setCurrentStatus] = useState(status);

    /** 保存后以完整响应替换 provider 状态。 */
    async function save(input: ProviderSaveInput) {
      const next = await onSave(input);
      setCurrentStatus(next);
      return next;
    }

    /** active callback 的默认实现只改变 active，定制测试可接管异常与延迟。 */
    async function changeActive(providerId: string, active: boolean) {
      const next = options.onProviderActiveChange
        ? await options.onProviderActiveChange(providerId, active)
        : ({ ...currentStatus, active } as ProviderStatus);
      setCurrentStatus(next);
      return next;
    }

    /** model callback 的默认实现模拟 backend 自动维护 enabled set 与 default。 */
    async function changeModelEnabled(
      providerId: string,
      modelId: string,
      enabled: boolean,
    ) {
      if (options.onModelEnabledChange) {
        const next = await options.onModelEnabledChange(
          providerId,
          modelId,
          enabled,
        );
        setCurrentStatus(next);
        return next;
      }
      const enabledModelIds = enabled
        ? [...new Set([...currentStatus.enabled_model_ids, modelId])]
        : currentStatus.enabled_model_ids.filter((id) => id !== modelId);
      const next = {
        ...currentStatus,
        enabled_model_ids: enabledModelIds,
        default_model_id:
          currentStatus.default_model_id === modelId && !enabled
            ? (enabledModelIds.at(0) ?? null)
            : currentStatus.default_model_id,
        models: currentStatus.models.map((model) =>
          model.id === modelId ? { ...model, enabled } : model,
        ),
      };
      setCurrentStatus(next);
      return next;
    }

    return (
      <ProviderPage
        catalog={presetCatalog}
        providers={[currentStatus, ...(options.additionalProviders ?? [])]}
        onCreate={async () => {
          throw new Error('本测试不创建 provider。');
        }}
        onAddModel={
          options.onAddModel ??
          (async (_providerId, input) => manualModelStatus(input))
        }
        onLoadApiKey={options.onLoadApiKey ?? loadSavedApiKey}
        onProbe={
          options.onProbe ??
          (async (providerId, input) => ({
            provider_id: providerId,
            model_id:
              input.model_id ??
              currentStatus.default_model_id ??
              'auto-selected-model',
            display_name: 'DeepSeek V4 Flash',
            latency_ms: 96,
          }))
        }
        onSave={async (_providerId, input) => save(input)}
        onProviderActiveChange={changeActive}
        onModelEnabledChange={changeModelEnabled}
        onSyncModels={
          options.onSyncModels ??
          (async () => ({
            provider_id: currentStatus.provider_id,
            models: currentStatus.models,
            last_synced_at: '2026-07-17T01:00:00Z',
            added: 0,
            restored: 0,
            unavailable: 0,
            unchanged: currentStatus.models.length,
          }))
        }
        onSaveModelSettings={
          options.onSaveModelSettings ??
          (async () => {
            const model = currentStatus.models[0];
            if (!model) throw new Error('测试状态缺少模型。');
            return model;
          })
        }
        onDelete={async () => undefined}
      />
    );
  }

  render(<PresetProviderHarness />);
}

/** 等待已配置 provider 的 Keychain 密钥进入受控输入，避免测试抢在 effect 前交互。 */
async function waitForSavedApiKey(): Promise<void> {
  await waitFor(() =>
    expect(screen.getByLabelText('API Key')).toHaveValue('sk-saved-existing'),
  );
}

/** 构造尚未输入密钥的 custom provider 公开状态。 */
function customProviderStatus(): ProviderStatus {
  return {
    provider_id: 'custom-a1',
    display_name: '内部网关',
    protocol: 'openai',
    is_custom: true,
    base_url: 'https://llm.example.com/v1',
    base_url_overridden: false,
    base_url_editable: false,
    deletable: true,
    available: true,
    configured: false,
    active: false,
    supports_model_sync: true,
    enabled_model_ids: [],
    default_model_id: null,
    model_count: 0,
    models: [],
    probe_status: 'not_configured',
    probe_error_code: null,
    latency_ms: null,
    last_probed_at: null,
    last_synced_at: null,
  };
}

/** 用最小本地状态模拟 App 对 create 回调结果的 upsert。 */
function ProviderPageHarness({
  onCreate,
  onAddModel,
}: ProviderPageHarnessProps) {
  const [providers, setProviders] = useState<ProviderStatus[]>([]);

  /** 创建后立即把新定义反馈给 ProviderPage。 */
  async function createProvider(input: CreateCustomProviderInput) {
    onCreate?.(input);
    const created = customProviderStatus();
    setProviders([created]);
    return created;
  }

  /** 返回登记后的 disabled model，启用只保留在 ProviderPage 草稿中。 */
  async function addModel(providerId: string, input: CreateProviderModelInput) {
    onAddModel?.(providerId, input);
    return manualModelStatus(input);
  }

  /** 测试不触发保存，保留窄 callback contract。 */
  async function saveProvider() {
    return customProviderStatus();
  }

  /** 测试不触发删除，保留窄 callback contract。 */
  async function deleteProvider() {
    return Promise.resolve();
  }

  return (
    <ProviderPage
      catalog={null}
      providers={providers}
      onCreate={createProvider}
      onAddModel={addModel}
      onLoadApiKey={loadSavedApiKey}
      onProbe={async (providerId, input) => ({
        provider_id: providerId,
        model_id: input.model_id ?? 'auto-selected-model',
        display_name: '自动选择模型',
        latency_ms: 96,
      })}
      onSave={saveProvider}
      onProviderActiveChange={async () => customProviderStatus()}
      onModelEnabledChange={async () => customProviderStatus()}
      onSyncModels={async () => ({
        provider_id: 'custom-a1',
        models: [],
        last_synced_at: '2026-07-17T01:00:00Z',
        added: 0,
        restored: 0,
        unavailable: 0,
        unchanged: 0,
      })}
      onSaveModelSettings={async () => {
        throw new Error('本测试不保存模型设置。');
      }}
      onDelete={deleteProvider}
    />
  );
}

describe('ProviderPage', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('无需添加操作便陈列全部预设供应商', () => {
    render(<ProviderPageHarness />);

    expect(
      screen.getByRole('heading', { name: '预设供应商' }),
    ).toBeInTheDocument();
    for (const name of [
      'DeepSeek，未配置',
      'Kimi，未配置',
      '智谱 GLM，未配置',
      'MiniMax，未配置',
      'MiMo，未配置',
    ]) {
      expect(screen.getByRole('button', { name })).toBeInTheDocument();
    }
    expect(
      screen.getByRole('button', { name: '添加自定义供应商' }),
    ).toBeInTheDocument();
    const providerSwitch = screen.getByRole('switch', {
      name: '启用 DeepSeek 供应商',
    });
    expect(providerSwitch).not.toBeChecked();
    expect(providerSwitch).toBeDisabled();
  });

  it('只收名称和 base URL，创建后进入统一的 Key 配置区', async () => {
    const onCreate = vi.fn();
    render(<ProviderPageHarness onCreate={onCreate} />);

    fireEvent.click(screen.getByRole('button', { name: '添加自定义供应商' }));
    fireEvent.change(screen.getByLabelText('供应商名称'), {
      target: { value: '内部网关' },
    });
    fireEvent.change(screen.getByLabelText('API 地址'), {
      target: { value: 'https://llm.example.com/v1/' },
    });
    fireEvent.click(screen.getByRole('button', { name: '创建并配置' }));

    expect(
      await screen.findByRole('button', { name: '内部网关，未配置' }),
    ).toHaveAttribute('aria-pressed', 'true');
    expect(
      screen.getByRole('heading', { name: '内部网关' }),
    ).toBeInTheDocument();
    expect(screen.getByText('https://llm.example.com/v1')).toBeInTheDocument();
    expect(screen.getByLabelText('API Key')).toHaveAttribute(
      'type',
      'password',
    );
    expect(onCreate).toHaveBeenCalledWith({
      display_name: '内部网关',
      base_url: 'https://llm.example.com/v1',
    });
  });

  it('首次配置 custom provider 时无需预选模型也可直接检测', async () => {
    render(<ProviderPageHarness />);

    fireEvent.click(screen.getByRole('button', { name: '添加自定义供应商' }));
    fireEvent.change(screen.getByLabelText('供应商名称'), {
      target: { value: '内部网关' },
    });
    fireEvent.change(screen.getByLabelText('API 地址'), {
      target: { value: 'https://llm.example.com/v1' },
    });
    fireEvent.click(screen.getByRole('button', { name: '创建并配置' }));
    await screen.findByRole('button', { name: '内部网关，未配置' });
    fireEvent.change(screen.getByLabelText('API Key'), {
      target: { value: 'sk-custom-test' },
    });

    expect(screen.getByRole('button', { name: '检测 API Key' })).toBeEnabled();
  });

  it('自定义供应商发现首个模型后自动选为默认，不要求额外点选', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        models: [
          {
            id: 'org/translate-v2',
            display_name: 'Translate V2',
            source: 'remote',
            enabled: false,
            available: true,
            reasoning_policy: null,
            reasoning_policy_override: null,
            supported_reasoning_policies: [],
            per_job_concurrency: 6,
            per_job_concurrency_override: null,
            global_concurrency: 15,
            global_concurrency_override: null,
          },
        ],
      }),
    } as Response);
    render(<ProviderPageHarness />);

    fireEvent.click(screen.getByRole('button', { name: '添加自定义供应商' }));
    fireEvent.change(screen.getByLabelText('供应商名称'), {
      target: { value: '内部网关' },
    });
    fireEvent.change(screen.getByLabelText('API 地址'), {
      target: { value: 'https://llm.example.com/v1' },
    });
    fireEvent.click(screen.getByRole('button', { name: '创建并配置' }));
    await screen.findByRole('button', { name: '内部网关，未配置' });
    fireEvent.change(screen.getByLabelText('API Key'), {
      target: { value: 'sk-custom-test' },
    });
    fireEvent.click(screen.getByRole('button', { name: '同步模型' }));

    expect(await screen.findByText('Translate V2')).toBeInTheDocument();
    expect(
      screen.getByRole('button', {
        name: '将 Translate V2 设为默认模型',
      }),
    ).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: '检测 API Key' })).toBeEnabled();
  });

  it('已配置密钥以真实值回填，并可通过眼睛查看原值', async () => {
    const status = presetProviderStatus();
    renderPresetProvider(status, async () => status);

    await waitForSavedApiKey();
    const input = screen.getByLabelText('API Key');
    expect(input).toHaveAttribute('type', 'password');
    expect(input).toHaveAttribute('placeholder', '输入 API Key');
    expect(input).toHaveValue('sk-saved-existing');

    fireEvent.click(screen.getByRole('button', { name: '显示 API Key' }));
    expect(input).toHaveAttribute('type', 'text');
    expect(input).toHaveValue('sk-saved-existing');
  });

  it('保存按钮常驻，并允许已配置 provider 重新验证当前配置', async () => {
    const status = presetProviderStatus();
    const onSave = vi.fn(async () => status);
    renderPresetProvider(status, onSave);
    await waitForSavedApiKey();

    const save = screen.getByRole('button', { name: '保存配置' });
    expect(save).toBeEnabled();
    fireEvent.click(save);

    await waitFor(() =>
      expect(onSave).toHaveBeenCalledWith({
        default_model_id: 'deepseek-v4-flash',
        enabled_model_ids: ['deepseek-v4-flash'],
      }),
    );
  });

  it('行内检测使用当前草稿但不保存配置或更新 baseline', async () => {
    const status = presetProviderStatus();
    const onSave = vi.fn(async () => status);
    const onProbe = vi.fn(async () => ({
      provider_id: 'deepseek',
      model_id: 'deepseek-v4-flash',
      display_name: 'DeepSeek V4 Flash',
      latency_ms: 73,
    }));
    renderPresetProvider(status, onSave, { onProbe });
    await waitForSavedApiKey();

    expect(screen.getByRole('button', { name: '保存配置' })).toBeEnabled();
    fireEvent.change(screen.getByLabelText('API Key'), {
      target: { value: 'sk-probe-only' },
    });
    expect(screen.getByText('更改尚未保存')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Base URL'), {
      target: { value: 'https://probe.example.com/v1/' },
    });
    const probe = screen.getByRole('button', { name: '检测 API Key' });
    expect(probe.querySelector('.lucide-check')).toBeNull();
    fireEvent.click(probe);

    await waitFor(() =>
      expect(onProbe).toHaveBeenCalledWith('deepseek', {
        api_key: 'sk-probe-only',
        base_url: 'https://probe.example.com/v1',
        model_id: 'deepseek-v4-flash',
      }),
    );
    expect(onSave).not.toHaveBeenCalled();
    expect(
      await screen.findByText(
        '检测通过 · DeepSeek V4 Flash · 73 ms，更改尚未保存',
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '保存配置' })).toBeEnabled();
  });

  it('切换 provider 会清空旧 Key，且迟到请求不能覆盖新 provider', async () => {
    const status = presetProviderStatus();
    const kimiStatus: ProviderStatus = {
      ...status,
      provider_id: 'kimi',
      display_name: 'Kimi',
      base_url: 'https://api.moonshot.cn/v1',
    };
    let resolveDeepSeek: ((value: string) => void) | undefined;
    let resolveKimi: ((value: string) => void) | undefined;
    let deepSeekSignal: AbortSignal | undefined;
    const onLoadApiKey = vi.fn(
      (providerId: string, signal?: AbortSignal) =>
        new Promise<string>((resolve) => {
          if (providerId === 'deepseek') {
            deepSeekSignal = signal;
            resolveDeepSeek = resolve;
          } else {
            resolveKimi = resolve;
          }
        }),
    );
    renderPresetProvider(status, async () => status, {
      additionalProviders: [kimiStatus],
      onLoadApiKey,
    });

    await waitFor(() =>
      expect(onLoadApiKey).toHaveBeenCalledWith(
        'deepseek',
        expect.any(AbortSignal),
      ),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Kimi，已启用' }));
    await waitFor(() =>
      expect(onLoadApiKey).toHaveBeenCalledWith(
        'kimi',
        expect.any(AbortSignal),
      ),
    );
    expect(screen.getByLabelText('API Key')).toHaveValue('');

    await act(async () => {
      resolveDeepSeek?.('sk-deepseek-late');
      await Promise.resolve();
    });
    expect(deepSeekSignal?.aborted).toBe(true);
    expect(screen.getByLabelText('API Key')).toHaveValue('');

    await act(async () => {
      resolveKimi?.('sk-kimi-current');
      await Promise.resolve();
    });
    expect(screen.getByLabelText('API Key')).toHaveValue('sk-kimi-current');
  });

  it('公网 HTTP 会在前端拦截，但本机 loopback 仍可创建', async () => {
    const onCreate = vi.fn();
    render(<ProviderPageHarness onCreate={onCreate} />);

    fireEvent.click(screen.getByRole('button', { name: '添加自定义供应商' }));
    fireEvent.change(screen.getByLabelText('供应商名称'), {
      target: { value: '本地代理' },
    });
    const baseUrlInput = screen.getByLabelText('API 地址');
    fireEvent.change(baseUrlInput, {
      target: { value: 'http://api.example.com/v1' },
    });
    fireEvent.click(screen.getByRole('button', { name: '创建并配置' }));

    expect(
      screen.getByText(
        '公网或局域网服务必须使用 HTTPS；HTTP 仅支持本机 loopback。',
      ),
    ).toBeInTheDocument();
    expect(onCreate).not.toHaveBeenCalled();

    fireEvent.change(baseUrlInput, {
      target: { value: 'http://127.0.0.1:8000/v1/' },
    });
    fireEvent.click(screen.getByRole('button', { name: '创建并配置' }));

    expect(
      await screen.findByRole('button', { name: '内部网关，未配置' }),
    ).toBeInTheDocument();
    expect(onCreate).toHaveBeenCalledWith({
      display_name: '本地代理',
      base_url: 'http://127.0.0.1:8000/v1',
    });
  });

  it('显示 preset Base URL，并把用户覆盖地址交给保存 contract', async () => {
    const status = presetProviderStatus();
    const onSave = vi.fn(async (input: ProviderSaveInput) => ({
      ...status,
      base_url: input.base_url ?? status.base_url,
      base_url_overridden: input.base_url !== null,
    }));
    renderPresetProvider(status, onSave);
    await waitForSavedApiKey();

    const input = screen.getByLabelText('Base URL');
    expect(input).toHaveValue('https://api.deepseek.com');
    fireEvent.change(input, {
      target: { value: 'https://gateway.example.com/deepseek/' },
    });
    fireEvent.click(screen.getByRole('button', { name: '保存配置' }));

    await waitFor(() =>
      expect(onSave).toHaveBeenCalledWith(
        expect.objectContaining({
          base_url: 'https://gateway.example.com/deepseek',
        }),
      ),
    );
    expect(onSave.mock.calls[0]?.[0]).not.toHaveProperty('api_key');
    expect(screen.getByRole('button', { name: '保存配置' })).toBeEnabled();
  });

  it('恢复默认时提交 null，而不是把当前 catalog URL 固化成 override', async () => {
    const status = presetProviderStatus(true);
    const onSave = vi.fn(async () => ({
      ...status,
      base_url: 'https://api.deepseek.com',
      base_url_overridden: false,
    }));
    renderPresetProvider(status, onSave);
    await waitForSavedApiKey();

    fireEvent.click(screen.getByRole('button', { name: '恢复默认' }));
    expect(screen.getByLabelText('Base URL')).toHaveValue(
      'https://api.deepseek.com',
    );
    fireEvent.click(screen.getByRole('button', { name: '保存配置' }));

    await waitFor(() =>
      expect(onSave).toHaveBeenCalledWith(
        expect.objectContaining({ base_url: null }),
      ),
    );
  });

  it('慢速验证期间冻结 Key 与 Base URL，避免成功回调清掉新输入', async () => {
    const status = presetProviderStatus(true);
    let resolveSave: ((next: ProviderStatus) => void) | undefined;
    const onSave = vi.fn(
      () =>
        new Promise<ProviderStatus>((resolve) => {
          resolveSave = resolve;
        }),
    );
    renderPresetProvider(status, onSave);
    await waitForSavedApiKey();

    fireEvent.change(screen.getByLabelText('Base URL'), {
      target: { value: 'https://gateway-2.example.com/deepseek' },
    });
    fireEvent.click(screen.getByRole('button', { name: '保存配置' }));

    expect(screen.getByLabelText('API Key')).toBeDisabled();
    expect(screen.getByLabelText('Base URL')).toBeDisabled();
    expect(screen.getByRole('button', { name: '恢复默认' })).toBeDisabled();
    expect(resolveSave).toBeDefined();
    await act(async () => {
      resolveSave?.(status);
    });
    await waitFor(() => expect(screen.getByLabelText('API Key')).toBeEnabled());
  });

  it('检测失败在 API Key 区域就近给出可访问错误', async () => {
    const status = presetProviderStatus();
    const onSave = vi.fn(async () => status);
    const onProbe = vi.fn(async () => {
      throw new ApiError('Invalid key.', 401, 'key');
    });
    renderPresetProvider(status, onSave, { onProbe });
    await waitForSavedApiKey();

    fireEvent.click(screen.getByRole('button', { name: '检测 API Key' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('API Key 无效或没有访问权限。');
    expect(alert).toHaveClass('provider-credential-feedback--error');
    expect(alert.closest('.provider-config-section')).not.toBeNull();
    expect(onSave).not.toHaveBeenCalled();
  });

  it('标题栏 active 开关只对已配置 provider 开放，并即时提交完整状态', async () => {
    const status = presetProviderStatus();
    const onProviderActiveChange = vi.fn(
      async (_providerId: string, active: boolean) =>
        ({ ...status, active }) as ProviderStatus,
    );
    renderPresetProvider(status, async () => status, {
      onProviderActiveChange,
    });

    const providerSwitch = screen.getByRole('switch', {
      name: '启用 DeepSeek 供应商',
    });
    expect(providerSwitch).toBeChecked();
    fireEvent.click(providerSwitch);

    await waitFor(() =>
      expect(onProviderActiveChange).toHaveBeenCalledWith('deepseek', false),
    );
    expect(providerSwitch).not.toBeChecked();
    expect(
      screen.getByRole('button', { name: 'DeepSeek，已停用' }),
    ).toBeInTheDocument();
    expect(screen.getByText('DeepSeek 已停用。')).toBeInTheDocument();
  });

  it('active mutation 失败时保留原开关，并用 alert 暴露错误', async () => {
    const status = presetProviderStatus();
    const onProviderActiveChange = vi.fn(async () => {
      throw new ApiError('The provider state changed.', 409, 'conflict');
    });
    renderPresetProvider(status, async () => status, {
      onProviderActiveChange,
    });

    const providerSwitch = screen.getByRole('switch', {
      name: '启用 DeepSeek 供应商',
    });
    fireEvent.click(providerSwitch);

    expect(await screen.findByRole('alert')).toHaveTextContent(
      '模型服务配置已变化，请重试当前操作。',
    );
    expect(providerSwitch).toBeChecked();
  });

  it('probe 失败的已配置 provider 在左栏明确显示异常', () => {
    const status: ProviderStatus = {
      ...presetProviderStatus(),
      probe_status: 'failed',
      probe_error_code: 'network',
    };
    renderPresetProvider(status, async () => status);

    expect(
      screen.getByRole('button', { name: 'DeepSeek，异常' }),
    ).toBeInTheDocument();
  });

  it('模型开关即时调用 callback，并按权威响应切换 default', async () => {
    const original = presetProviderStatus();
    const firstModel = original.models[0];
    if (!firstModel) throw new Error('测试状态缺少模型。');
    const secondModel: ProviderModelStatus = {
      ...firstModel,
      id: 'deepseek-v4-pro',
      display_name: 'DeepSeek V4 Pro',
      enabled: true,
    };
    const status: ProviderStatus = {
      ...original,
      enabled_model_ids: ['deepseek-v4-flash', 'deepseek-v4-pro'],
      model_count: 2,
      models: [...original.models, secondModel],
    };
    const next: ProviderStatus = {
      ...status,
      enabled_model_ids: ['deepseek-v4-pro'],
      default_model_id: 'deepseek-v4-pro',
      models: status.models.map((model) =>
        model.id === 'deepseek-v4-flash' ? { ...model, enabled: false } : model,
      ),
    };
    const onModelEnabledChange = vi.fn(async () => next);
    renderPresetProvider(status, async () => status, {
      onModelEnabledChange,
    });

    const flashSwitch = screen.getByRole('switch', {
      name: '启用 DeepSeek V4 Flash',
    });
    fireEvent.click(flashSwitch);

    await waitFor(() =>
      expect(onModelEnabledChange).toHaveBeenCalledWith(
        'deepseek',
        'deepseek-v4-flash',
        false,
      ),
    );
    expect(flashSwitch).not.toBeChecked();
    expect(
      screen.getByRole('button', {
        name: '将 DeepSeek V4 Pro 设为默认模型',
      }),
    ).toHaveAttribute('aria-pressed', 'true');
  });

  it('已配置 provider 更换默认模型后才通过保存提交', async () => {
    const original = presetProviderStatus();
    const firstModel = original.models[0];
    if (!firstModel) throw new Error('测试状态缺少模型。');
    const secondModel: ProviderModelStatus = {
      ...firstModel,
      id: 'deepseek-v4-pro',
      display_name: 'DeepSeek V4 Pro',
      enabled: true,
    };
    const status: ProviderStatus = {
      ...original,
      enabled_model_ids: ['deepseek-v4-flash', 'deepseek-v4-pro'],
      model_count: 2,
      models: [...original.models, secondModel],
    };
    const onSave = vi.fn(async (input: ProviderSaveInput) => ({
      ...status,
      default_model_id: input.default_model_id,
    }));
    renderPresetProvider(status, onSave);
    await waitForSavedApiKey();

    expect(screen.getByRole('button', { name: '保存配置' })).toBeEnabled();
    fireEvent.click(
      screen.getByRole('button', {
        name: '将 DeepSeek V4 Pro 设为默认模型',
      }),
    );

    const save = screen.getByRole('button', { name: '保存配置' });
    expect(save).toBeEnabled();
    expect(
      screen.getByRole('button', {
        name: '同步模型：请先保存新的默认模型，再同步模型',
      }),
    ).toBeDisabled();
    fireEvent.click(save);

    await waitFor(() =>
      expect(onSave).toHaveBeenCalledWith({
        default_model_id: 'deepseek-v4-pro',
        enabled_model_ids: ['deepseek-v4-flash', 'deepseek-v4-pro'],
      }),
    );
    expect(onSave.mock.calls[0]?.[0]).not.toHaveProperty('api_key');
    expect(screen.getByRole('button', { name: '保存配置' })).toBeEnabled();
  });

  it('关闭最后一个模型时保留开关，并给出可操作提示', async () => {
    const status = presetProviderStatus();
    const onModelEnabledChange = vi.fn(async () => {
      throw new ApiError(
        'At least one model is required.',
        409,
        'model_required',
      );
    });
    renderPresetProvider(status, async () => status, {
      onModelEnabledChange,
    });

    const modelSwitch = screen.getByRole('switch', {
      name: '启用 DeepSeek V4 Flash',
    });
    fireEvent.click(modelSwitch);

    expect(await screen.findByRole('alert')).toHaveTextContent(
      '至少保留一个已启用且验证通过的模型。',
    );
    expect(modelSwitch).toBeChecked();
  });

  it('破坏动作只表达移除配置，不再与 active 停用混用', () => {
    const status = presetProviderStatus();
    renderPresetProvider(status, async () => status);

    const remove = screen.getByRole('button', { name: '移除配置' });
    expect(screen.queryByRole('button', { name: '停用服务' })).toBeNull();
    fireEvent.click(remove);
    expect(
      screen.getByRole('button', { name: '确认移除' }),
    ).toBeInTheDocument();
  });

  it('首次配置预览全部模型为开启但不可操作，并提交 enable_all_models', async () => {
    const configuredStatus = presetProviderStatus();
    const status: ProviderStatus = {
      ...configuredStatus,
      configured: false,
      enabled_model_ids: [],
      default_model_id: null,
      probe_status: 'not_configured',
      models: configuredStatus.models.map((model) => ({
        ...model,
        enabled: false,
      })),
    };
    const onSave = vi.fn(async () => status);
    renderPresetProvider(status, onSave);

    const modelSwitch = screen.getByRole('switch', {
      name: '启用 DeepSeek V4 Flash',
    });
    expect(modelSwitch).toBeChecked();
    expect(modelSwitch).toBeDisabled();
    expect(screen.getByText(/验证后默认全部启用/)).toBeInTheDocument();
    const save = screen.getByRole('button', { name: '保存配置' });
    expect(save).toBeDisabled();
    fireEvent.change(screen.getByLabelText('API Key'), {
      target: { value: 'sk-first-config' },
    });
    expect(save).toBeEnabled();
    fireEvent.click(save);

    await waitFor(() =>
      expect(onSave).toHaveBeenCalledWith({
        api_key: 'sk-first-config',
        enable_all_models: true,
        default_model_id: 'deepseek-v4-flash',
      }),
    );
  });

  it('同步模型时使用尚未保存的 Base URL draft', async () => {
    const request = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ models: [] }),
    } as Response);
    const status = {
      ...presetProviderStatus(),
      configured: false,
      enabled_model_ids: [],
      default_model_id: null,
      probe_status: 'not_configured' as const,
    };
    renderPresetProvider(status, async () => status);

    fireEvent.change(screen.getByLabelText('API Key'), {
      target: { value: 'sk-preview-only' },
    });
    fireEvent.change(screen.getByLabelText('Base URL'), {
      target: { value: 'https://gateway.example.com/deepseek/' },
    });
    fireEvent.click(screen.getByRole('button', { name: '同步模型' }));

    await waitFor(() => expect(request).toHaveBeenCalledTimes(1));
    expect(request).toHaveBeenCalledWith(
      'http://127.0.0.1:8765/api/v1/providers/deepseek/models/discover',
      expect.objectContaining({
        body: JSON.stringify({
          api_key: 'sk-preview-only',
          base_url: 'https://gateway.example.com/deepseek',
        }),
      }),
    );
  });

  it('已配置 provider 使用持久化同步，并展示幂等合并结果', async () => {
    const status = presetProviderStatus();
    const onSyncModels = vi.fn(async () => ({
      provider_id: 'deepseek',
      models: status.models,
      last_synced_at: '2026-07-17T02:00:00Z',
      added: 1,
      restored: 2,
      unavailable: 3,
      unchanged: 4,
    }));
    renderPresetProvider(status, async () => status, { onSyncModels });
    await waitForSavedApiKey();

    fireEvent.click(screen.getByRole('button', { name: '同步模型' }));

    await waitFor(() => expect(onSyncModels).toHaveBeenCalledWith('deepseek'));
    expect(
      screen.getByText(
        '模型同步完成：新增 1 个，恢复 2 个，标记不可用 3 个，未变化 4 个。',
      ),
    ).toBeInTheDocument();
  });

  it('已配置 provider 输入新 Key 后必须先保存，不能用旧 Key 同步', () => {
    const status = presetProviderStatus();
    const onSyncModels = vi.fn(async () => ({
      provider_id: 'deepseek',
      models: status.models,
      last_synced_at: '2026-07-17T02:00:00Z',
      added: 0,
      restored: 0,
      unavailable: 0,
      unchanged: 1,
    }));
    renderPresetProvider(status, async () => status, { onSyncModels });

    fireEvent.change(screen.getByLabelText('API Key'), {
      target: { value: 'sk-not-saved-yet' },
    });
    const sync = screen.getByRole('button', {
      name: '同步模型：请先保存新 API Key，再同步模型',
    });
    expect(sync).toBeDisabled();
    fireEvent.click(sync);
    expect(onSyncModels).not.toHaveBeenCalled();
  });

  it('手动模型立即进入当前草稿，但不覆盖已有默认或提前开放运行设置', async () => {
    const status = presetProviderStatus();
    const onAddModel = vi.fn(
      async (_providerId: string, input: CreateProviderModelInput) =>
        manualModelStatus(input),
    );
    renderPresetProvider(status, async () => status, { onAddModel });

    fireEvent.click(screen.getByRole('button', { name: '添加模型' }));
    const dialog = screen.getByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('模型 ID'), {
      target: { value: ' org/translate-v2 ' },
    });
    fireEvent.change(within(dialog).getByLabelText('显示名称（可选）'), {
      target: { value: ' Translate V2 ' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: '添加模型' }));

    await waitFor(() =>
      expect(onAddModel).toHaveBeenCalledWith('deepseek', {
        model_id: 'org/translate-v2',
        display_name: 'Translate V2',
      }),
    );
    const modelList = document.querySelector('.provider-model-list');
    expect(modelList).not.toBeNull();
    const manualRow = within(modelList as HTMLElement)
      .getByText('Translate V2')
      .closest('.provider-model-row');
    expect(manualRow).not.toBeNull();
    expect(
      within(manualRow as HTMLElement).getByText('手动'),
    ).toBeInTheDocument();
    expect(
      within(manualRow as HTMLElement).getByRole('switch', {
        name: '启用 Translate V2',
      }),
    ).not.toBeChecked();
    expect(
      within(manualRow as HTMLElement).getByRole('button', {
        name: '将 Translate V2 设为默认模型',
      }),
    ).toHaveAttribute('aria-pressed', 'false');
    expect(
      screen.getByRole('button', {
        name: '将 DeepSeek V4 Flash 设为默认模型',
      }),
    ).toHaveAttribute('aria-pressed', 'true');
    expect(
      screen.queryByRole('button', { name: '设置 Translate V2' }),
    ).toBeNull();
    expect(
      screen.getByText('Translate V2 已添加；开启后将验证并启用。'),
    ).toBeInTheDocument();
  });

  it('空 inventory 手动添加首个模型时才自动设为默认', async () => {
    const onAddModel = vi.fn();
    render(<ProviderPageHarness onAddModel={onAddModel} />);

    fireEvent.click(screen.getByRole('button', { name: '添加模型' }));
    const dialog = screen.getByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('模型 ID'), {
      target: { value: 'first-manual-model' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: '添加模型' }));

    await waitFor(() =>
      expect(onAddModel).toHaveBeenCalledWith('deepseek', {
        model_id: 'first-manual-model',
      }),
    );
    expect(
      screen.getByRole('button', {
        name: '将 first-manual-model 设为默认模型',
      }),
    ).toHaveAttribute('aria-pressed', 'true');
    expect(
      screen.getByRole('switch', { name: '启用 first-manual-model' }),
    ).toBeChecked();
  });

  it('手动添加模型在本地拦截空值与当前列表中的重复 id', () => {
    const status = presetProviderStatus();
    const onAddModel = vi.fn(async () => manualModelStatus());
    renderPresetProvider(status, async () => status, { onAddModel });

    fireEvent.click(screen.getByRole('button', { name: '添加模型' }));
    const dialog = screen.getByRole('dialog');
    const submit = within(dialog).getByRole('button', { name: '添加模型' });
    fireEvent.click(submit);
    expect(within(dialog).getByText('请输入模型 ID。')).toBeInTheDocument();

    fireEvent.change(within(dialog).getByLabelText('模型 ID'), {
      target: { value: 'bad model' },
    });
    fireEvent.click(submit);
    expect(
      within(dialog).getByText('模型 ID 不能包含空白字符或控制字符。'),
    ).toBeInTheDocument();

    fireEvent.change(within(dialog).getByLabelText('模型 ID'), {
      target: { value: 'valid-model' },
    });
    fireEvent.change(within(dialog).getByLabelText('显示名称（可选）'), {
      target: { value: 'bad\u007fname' },
    });
    fireEvent.click(submit);
    expect(
      within(dialog).getByText('显示名称不能包含控制字符。'),
    ).toBeInTheDocument();

    fireEvent.change(within(dialog).getByLabelText('模型 ID'), {
      target: { value: 'deepseek-v4-flash' },
    });
    fireEvent.change(within(dialog).getByLabelText('显示名称（可选）'), {
      target: { value: '' },
    });
    fireEvent.click(submit);
    expect(
      within(dialog).getByText('该模型已在当前列表中。'),
    ).toBeInTheDocument();
    expect(onAddModel).not.toHaveBeenCalled();
  });

  it('手动添加发生服务端重复竞态时保留对话框与输入', async () => {
    const status = presetProviderStatus();
    const onAddModel = vi.fn(async () => {
      throw new ApiError('The provider model already exists.', 409, 'conflict');
    });
    renderPresetProvider(status, async () => status, { onAddModel });

    fireEvent.click(screen.getByRole('button', { name: '添加模型' }));
    const dialog = screen.getByRole('dialog');
    const modelIdInput = within(dialog).getByLabelText('模型 ID');
    fireEvent.change(modelIdInput, {
      target: { value: 'racing-manual-model' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: '添加模型' }));

    expect(
      await within(dialog).findByText('该模型已存在。'),
    ).toBeInTheDocument();
    expect(modelIdInput).toHaveValue('racing-manual-model');
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('慢速手动添加占用页面级 pending gate，阻止重复提交和切换上下文', async () => {
    const status = presetProviderStatus();
    let resolveAdd: ((model: ProviderModelStatus) => void) | undefined;
    const onAddModel = vi.fn(
      () =>
        new Promise<ProviderModelStatus>((resolve) => {
          resolveAdd = resolve;
        }),
    );
    renderPresetProvider(status, async () => status, { onAddModel });

    fireEvent.click(screen.getByRole('button', { name: '添加模型' }));
    const dialog = screen.getByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('模型 ID'), {
      target: { value: 'slow-manual-model' },
    });
    const submit = within(dialog).getByRole('button', { name: '添加模型' });
    fireEvent.click(submit);

    expect(onAddModel).toHaveBeenCalledTimes(1);
    expect(within(dialog).getByLabelText('模型 ID')).toBeDisabled();
    expect(
      within(dialog).getByRole('button', { name: '关闭添加模型' }),
    ).toBeDisabled();
    const providerButton = document.querySelector<HTMLButtonElement>(
      'button[aria-label="DeepSeek，已启用"]',
    );
    expect(providerButton).not.toBeNull();
    expect(providerButton).toBeDisabled();
    expect(resolveAdd).toBeDefined();
    await act(async () => {
      resolveAdd?.(manualModelStatus({ model_id: 'slow-manual-model' }));
    });
    await waitFor(() =>
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument(),
    );
    expect(onAddModel).toHaveBeenCalledTimes(1);
  });

  it('不可用且已关闭的模型不能重新启用', () => {
    const original = presetProviderStatus();
    const status: ProviderStatus = {
      ...original,
      enabled_model_ids: [],
      default_model_id: null,
      models: original.models.map((model) => ({
        ...model,
        enabled: false,
        available: false,
      })),
    };
    renderPresetProvider(status, async () => status);

    const modelSwitch = screen.getByRole('switch', {
      name: '启用 DeepSeek V4 Flash',
    });
    expect(modelSwitch).not.toBeChecked();
    expect(modelSwitch).toBeDisabled();
    fireEvent.click(modelSwitch);
    expect(modelSwitch).not.toBeChecked();
  });

  it('只为已启用模型开放设置，并校验两层并发上限后提交 override', async () => {
    const status = presetProviderStatus();
    const model = status.models[0];
    if (!model) throw new Error('测试状态缺少模型。');
    const onSaveModelSettings = vi.fn(
      async (
        _providerId: string,
        _modelId: string,
        input: UpdateProviderModelSettingsInput,
      ) => ({
        ...model,
        reasoning_policy: input.reasoning_policy_override ?? 'provider_default',
        reasoning_policy_override: input.reasoning_policy_override ?? null,
        per_job_concurrency: input.per_job_concurrency_override ?? 6,
        per_job_concurrency_override:
          input.per_job_concurrency_override ?? null,
        global_concurrency: input.global_concurrency_override ?? 15,
        global_concurrency_override: input.global_concurrency_override ?? null,
      }),
    );
    renderPresetProvider(status, async () => status, {
      onSaveModelSettings,
    });

    fireEvent.click(
      screen.getByRole('button', { name: '设置 DeepSeek V4 Flash' }),
    );
    expect(
      screen.getByRole('option', { name: '供应商默认' }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('option', { name: '关闭思考' }),
    ).toBeInTheDocument();
    expect(screen.getByRole('option', { name: '高' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: '最高' })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: '中' })).toBeNull();

    fireEvent.change(screen.getByRole('combobox', { name: '思考模式' }), {
      target: { value: 'high' },
    });
    fireEvent.change(screen.getByRole('spinbutton', { name: '单任务并发' }), {
      target: { value: '16' },
    });
    fireEvent.change(screen.getByRole('spinbutton', { name: '跨任务总并发' }), {
      target: { value: '12' },
    });
    expect(
      screen.getByText('单任务并发不能超过跨任务总并发。'),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '保存设置' })).toBeDisabled();

    fireEvent.change(screen.getByRole('spinbutton', { name: '单任务并发' }), {
      target: { value: '8' },
    });
    fireEvent.change(screen.getByRole('spinbutton', { name: '跨任务总并发' }), {
      target: { value: '20' },
    });
    fireEvent.click(screen.getByRole('button', { name: '保存设置' }));

    await waitFor(() =>
      expect(onSaveModelSettings).toHaveBeenCalledWith(
        'deepseek',
        'deepseek-v4-flash',
        {
          reasoning_policy_override: 'high',
          per_job_concurrency_override: 8,
          global_concurrency_override: 20,
        },
      ),
    );
    expect(
      screen.getByText('DeepSeek V4 Flash 的运行设置已保存。'),
    ).toBeInTheDocument();
  });

  it('恢复应用默认时一次清空三个模型 override', async () => {
    const status = presetProviderStatus();
    const model = status.models[0];
    if (!model) throw new Error('测试状态缺少模型。');
    const onSaveModelSettings = vi.fn(async () => model);
    renderPresetProvider(status, async () => status, {
      onSaveModelSettings,
    });

    fireEvent.click(
      screen.getByRole('button', { name: '设置 DeepSeek V4 Flash' }),
    );
    fireEvent.click(screen.getByRole('button', { name: '恢复应用默认' }));

    await waitFor(() =>
      expect(onSaveModelSettings).toHaveBeenCalledWith(
        'deepseek',
        'deepseek-v4-flash',
        {
          reasoning_policy_override: null,
          per_job_concurrency_override: null,
          global_concurrency_override: null,
        },
      ),
    );
  });

  it('未持久化启用的模型不显示运行设置入口', () => {
    const status = presetProviderStatus();
    status.models = status.models.map((model) => ({
      ...model,
      enabled: false,
    }));
    renderPresetProvider(status, async () => status);

    expect(
      screen.queryByRole('button', { name: '设置 DeepSeek V4 Flash' }),
    ).toBeNull();
  });
});
