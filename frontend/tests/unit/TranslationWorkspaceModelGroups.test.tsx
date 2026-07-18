/** 验证翻译页只按可运行供应商分组展示可用模型。 */

import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { TranslationWorkspace } from '../../src/features/translation/TranslationWorkspace';
import type { ProviderModelStatus, ProviderStatus } from '../../src/lib/api';

/** 构造具备完整 runtime 字段的 provider model。 */
function providerModel(
  id: string,
  displayName: string,
  overrides: Partial<ProviderModelStatus> = {},
): ProviderModelStatus {
  return {
    id,
    display_name: displayName,
    source: 'remote',
    enabled: true,
    available: true,
    reasoning_policy: null,
    reasoning_policy_override: null,
    supported_reasoning_policies: [],
    per_job_concurrency: 6,
    per_job_concurrency_override: null,
    global_concurrency: 15,
    global_concurrency_override: null,
    ...overrides,
  };
}

/** 构造一个默认处于可翻译状态的 provider。 */
function providerStatus(
  providerId: string,
  displayName: string,
  models: ProviderModelStatus[],
  overrides: Partial<ProviderStatus> = {},
): ProviderStatus {
  const enabledModelIds = models.map((model) => model.id);
  return {
    provider_id: providerId,
    display_name: displayName,
    protocol: 'openai',
    is_custom: false,
    base_url: `https://${providerId}.example.com`,
    base_url_overridden: false,
    base_url_editable: false,
    deletable: false,
    available: true,
    configured: true,
    active: true,
    supports_model_sync: true,
    enabled_model_ids: enabledModelIds,
    default_model_id: enabledModelIds[0] ?? null,
    model_count: models.length,
    models,
    probe_status: 'succeeded',
    probe_error_code: null,
    latency_ms: 20,
    last_probed_at: '2026-07-17T00:00:00Z',
    last_synced_at: '2026-07-17T00:00:00Z',
    ...overrides,
  };
}

/** 渲染不含任务与文件的翻译页。 */
function renderWorkspace(providers: ProviderStatus[]) {
  render(
    <TranslationWorkspace
      active
      catalog={null}
      providers={providers}
      jobs={[]}
      onOpenModelSettings={vi.fn()}
      onPdfIntent={vi.fn()}
      onStart={vi.fn(async () => true)}
    />,
  );
}

describe('TranslationWorkspace model groups', () => {
  it('按 provider 稳定分组，只保留 enabled、available 且 probe 成功的模型', async () => {
    const deepSeekModels = [
      providerModel('deepseek-pro', 'DeepSeek V4 Pro'),
      providerModel('deepseek-flash', 'DeepSeek V4 Flash', {
        probe_status: 'succeeded',
      }),
      providerModel('deepseek-disabled', 'Disabled model', {
        enabled: false,
      }),
      providerModel('deepseek-unavailable', 'Unavailable model', {
        available: false,
      }),
      providerModel('deepseek-failed', 'Failed model', {
        probe_status: 'failed',
      }),
    ];
    const providers = [
      providerStatus('deepseek', 'DeepSeek', deepSeekModels, {
        default_model_id: 'deepseek-pro',
      }),
      providerStatus('kimi', 'Kimi', [providerModel('kimi-k2.6', 'Kimi K2.6')]),
      providerStatus(
        'inactive',
        'Inactive provider',
        [providerModel('inactive-model', 'Inactive model')],
        {
          active: false,
        },
      ),
      providerStatus(
        'unavailable',
        'Unavailable provider',
        [providerModel('unavailable-provider-model', 'Hidden model')],
        {
          available: false,
        },
      ),
      providerStatus(
        'unconfigured',
        'Unconfigured provider',
        [providerModel('unconfigured-model', 'Hidden unconfigured model')],
        {
          configured: false,
        },
      ),
      providerStatus(
        'failed',
        'Failed provider',
        [providerModel('failed-provider-model', 'Hidden failed model')],
        {
          probe_status: 'failed',
        },
      ),
    ];

    renderWorkspace(providers);

    const trigger = screen.getByRole('combobox', { name: '翻译模型' });
    expect(trigger).toHaveTextContent('DeepSeek V4 Pro');
    expect(
      trigger.querySelector('.provider-icon-avatar--deepseek'),
    ).not.toBeNull();

    fireEvent.keyDown(trigger, { key: 'ArrowDown' });
    const groups = await screen.findAllByRole('group');
    expect(groups).toHaveLength(2);
    expect(groups[0]).toHaveAccessibleName('DeepSeek');
    expect(groups[1]).toHaveAccessibleName('Kimi');

    const deepSeekGroup = screen.getByRole('group', { name: 'DeepSeek' });
    const deepSeekOptions = within(deepSeekGroup).getAllByRole('option');
    expect(deepSeekOptions).toHaveLength(2);
    expect(deepSeekOptions[0]).toHaveAccessibleName('DeepSeek V4 Pro');
    expect(deepSeekOptions[0]).toHaveTextContent(/^DeepSeek V4 Pro$/);
    expect(deepSeekOptions[1]).toHaveAccessibleName('DeepSeek V4 Flash');
    expect(
      deepSeekGroup.querySelector('.provider-icon-avatar--deepseek'),
    ).not.toBeNull();

    expect(screen.queryByText('Disabled model')).not.toBeInTheDocument();
    expect(screen.queryByText('Unavailable model')).not.toBeInTheDocument();
    expect(screen.queryByText('Failed model')).not.toBeInTheDocument();
    expect(screen.queryByText('Inactive provider')).not.toBeInTheDocument();
    expect(screen.queryByText('Unavailable provider')).not.toBeInTheDocument();
    expect(screen.queryByText('Unconfigured provider')).not.toBeInTheDocument();
    expect(screen.queryByText('Failed provider')).not.toBeInTheDocument();

    fireEvent.click(
      within(screen.getByRole('group', { name: 'Kimi' })).getByRole('option', {
        name: 'Kimi K2.6',
      }),
    );
    expect(trigger).toHaveTextContent('Kimi K2.6');
    expect(trigger.querySelector('.provider-icon-avatar--kimi')).not.toBeNull();
  });

  it('provider 未激活时不因配置和 probe 成功进入选择器', () => {
    renderWorkspace([
      providerStatus(
        'deepseek',
        'DeepSeek',
        [providerModel('deepseek-pro', 'DeepSeek V4 Pro')],
        {
          active: false,
        },
      ),
    ]);

    expect(
      screen.queryByRole('combobox', { name: '翻译模型' }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: '配置模型' }),
    ).toBeInTheDocument();
  });
});
