/** 验证 provider 二级列表默认陈列 preset，并支持轻量 custom provider 创建。 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ProviderPage } from '../../src/features/providers/ProviderPage';
import type {
  ConfigureProviderInput,
  CreateCustomProviderInput,
  ModelCatalog,
  ProviderStatus,
} from '../../src/lib/api';

interface ProviderPageHarnessProps {
  onCreate?: (input: CreateCustomProviderInput) => void;
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
      },
    ],
    probe_status: 'succeeded',
    probe_error_code: null,
    latency_ms: 120,
    last_probed_at: '2026-07-17T00:00:00Z',
    last_synced_at: null,
  };
}

/** 直接渲染 preset 配置页，并让测试接管保存 callback。 */
function renderPresetProvider(
  status: ProviderStatus,
  onSave: (input: ConfigureProviderInput) => Promise<ProviderStatus>,
): void {
  render(
    <ProviderPage
      catalog={presetCatalog}
      providers={[status]}
      onCreate={async () => {
        throw new Error('本测试不创建 provider。');
      }}
      onSave={async (_providerId, input) => onSave(input)}
      onDelete={async () => undefined}
    />,
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
function ProviderPageHarness({ onCreate }: ProviderPageHarnessProps) {
  const [providers, setProviders] = useState<ProviderStatus[]>([]);

  /** 创建后立即把新定义反馈给 ProviderPage。 */
  async function createProvider(input: CreateCustomProviderInput) {
    onCreate?.(input);
    const created = customProviderStatus();
    setProviders([created]);
    return created;
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
      onSave={saveProvider}
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
    const onSave = vi.fn(async () => ({
      ...status,
      base_url: 'https://gateway.example.com/deepseek',
      base_url_overridden: true,
    }));
    renderPresetProvider(status, onSave);

    const input = screen.getByLabelText('Base URL');
    expect(input).toHaveValue('https://api.deepseek.com');
    fireEvent.change(input, {
      target: { value: 'https://gateway.example.com/deepseek/' },
    });
    fireEvent.click(screen.getByRole('button', { name: '验证并保存' }));

    await waitFor(() =>
      expect(onSave).toHaveBeenCalledWith(
        expect.objectContaining({
          base_url: 'https://gateway.example.com/deepseek',
        }),
      ),
    );
  });

  it('恢复默认时提交 null，而不是把当前 catalog URL 固化成 override', async () => {
    const status = presetProviderStatus(true);
    const onSave = vi.fn(async () => ({
      ...status,
      base_url: 'https://api.deepseek.com',
      base_url_overridden: false,
    }));
    renderPresetProvider(status, onSave);

    fireEvent.click(screen.getByRole('button', { name: '恢复默认' }));
    expect(screen.getByLabelText('Base URL')).toHaveValue(
      'https://api.deepseek.com',
    );
    fireEvent.click(screen.getByRole('button', { name: '验证并保存' }));

    await waitFor(() =>
      expect(onSave).toHaveBeenCalledWith(
        expect.objectContaining({ base_url: null }),
      ),
    );
  });

  it('同步模型时使用尚未保存的 Base URL draft', async () => {
    const request = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ models: [] }),
    } as Response);
    const status = presetProviderStatus();
    renderPresetProvider(status, async () => status);

    fireEvent.change(screen.getByLabelText('Base URL'), {
      target: { value: 'https://gateway.example.com/deepseek/' },
    });
    fireEvent.click(screen.getByRole('button', { name: '同步模型' }));

    await waitFor(() => expect(request).toHaveBeenCalledTimes(1));
    expect(request).toHaveBeenCalledWith(
      'http://127.0.0.1:8765/api/v1/providers/deepseek/models/discover',
      expect.objectContaining({
        body: JSON.stringify({
          base_url: 'https://gateway.example.com/deepseek',
        }),
      }),
    );
  });
});
