/** 验证三页信息架构、任务创建、高级选项与 provider 配置。 */

import { fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { App } from '../../src/App';
import type { TranslationJob } from '../../src/lib/api';

const catalog = {
  schema_version: 1,
  catalog_version: '0.1.0-dev',
  providers: [
    {
      id: 'deepseek',
      display_name: 'DeepSeek',
      protocol: 'openai',
      available: true,
      base_url_editable: false,
      supports_model_sync: true,
      docs_url: 'https://api-docs.deepseek.com/',
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

/** 创建足够模拟本地 JSON endpoint 的 Response。 */
function jsonResponse(payload: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  } as Response;
}

/** 返回 generic provider API 使用的 DeepSeek 安全状态。 */
function providerStatus(configured: boolean) {
  return {
    provider_id: 'deepseek',
    display_name: 'DeepSeek',
    available: true,
    configured,
    supports_model_sync: true,
    enabled_model_ids: configured ? ['deepseek-v4-flash'] : [],
    default_model_id: configured ? 'deepseek-v4-flash' : null,
    model_count: 1,
    models: [
      {
        id: 'deepseek-v4-flash',
        display_name: 'DeepSeek V4 Flash',
        source: 'catalog',
        enabled: configured,
      },
    ],
    probe_status: configured ? 'succeeded' : 'not_configured',
    probe_error_code: null,
    latency_ms: configured ? 246 : null,
    last_probed_at: configured ? '2026-07-16T08:00:00Z' : null,
    last_synced_at: null,
  };
}

/** 返回已激活的 OpenAI-compatible custom provider 安全状态。 */
function customProviderStatus() {
  return {
    provider_id: 'custom-local',
    display_name: '内部网关',
    protocol: 'openai',
    is_custom: true,
    base_url: 'https://llm.example.com/v1',
    base_url_editable: false,
    deletable: true,
    available: true,
    configured: true,
    supports_model_sync: true,
    enabled_model_ids: ['local-translate'],
    default_model_id: 'local-translate',
    model_count: 1,
    models: [
      {
        id: 'local-translate',
        display_name: '内部翻译模型',
        source: 'remote',
        enabled: true,
        available: true,
      },
    ],
    probe_status: 'succeeded',
    probe_error_code: null,
    latency_ms: 88,
    last_probed_at: '2026-07-16T08:00:00Z',
    last_synced_at: '2026-07-16T08:00:00Z',
  };
}

/** 构造前端和历史 API 共用的完整任务快照。 */
function translationJob(
  sourceName = 'sample.docx',
  id = 'job-1',
  documentType: 'docx' | 'pptx' | 'txt' | 'md' = 'docx',
): TranslationJob {
  return {
    id,
    source_name: sourceName,
    document_type: documentType,
    status: 'succeeded',
    progress: 100,
    progress_stage: 'formatting',
    processed_segments: 4,
    total_segments: 4,
    provider_id: 'deepseek',
    model_id: 'deepseek-v4-flash',
    source_language: null,
    target_language: 'zh-CN',
    output_path: `/tmp/${sourceName}`,
    error_code: null,
    translated_segments: 4,
    fallback_segments: 0,
    warning_codes: [],
    created_at: '2026-07-16T08:00:00Z',
    updated_at: '2026-07-16T08:00:01Z',
  };
}

/** 打开一个 Radix Select 并选择可访问名称匹配的 option。 */
async function chooseSelectOption(label: string, optionName: string) {
  const trigger = screen.getByRole('combobox', { name: label });
  trigger.focus();
  fireEvent.keyDown(trigger, { key: 'ArrowDown' });
  const option = await screen.findByRole('option', { name: optionName });
  fireEvent.click(option);
}

describe('App', () => {
  let configured = true;
  let initialJobs: ReturnType<typeof translationJob>[] = [];
  let statusFactory: () => ReturnType<typeof providerStatus>;
  let providerListFactory: () => unknown[];
  let discoveryModels: ReturnType<typeof providerStatus>['models'];
  let deleteCompleted: boolean;
  let failProviderRefreshAfterDelete: boolean;

  beforeEach(() => {
    configured = true;
    initialJobs = [];
    statusFactory = () => providerStatus(configured);
    providerListFactory = () => [statusFactory()];
    deleteCompleted = false;
    failProviderRefreshAfterDelete = false;
    discoveryModels = [
      {
        id: 'deepseek-v4-flash',
        display_name: 'DeepSeek V4 Flash',
        source: 'remote',
        enabled: true,
      },
      {
        id: 'deepseek-chat',
        display_name: 'DeepSeek Chat',
        source: 'remote',
        enabled: false,
      },
    ];
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.endsWith('/healthz')) {
        return jsonResponse({
          code: 'success',
          data: { service: 'pageferry-api', version: '0.1.0' },
        });
      }
      if (url.endsWith('/api/v1/model-catalog')) return jsonResponse(catalog);
      if (
        url.endsWith('/api/v1/providers/deepseek/models/discover') &&
        init?.method === 'POST'
      ) {
        return jsonResponse({
          models: discoveryModels,
        });
      }
      if (
        url.endsWith('/api/v1/providers/deepseek') &&
        init?.method === 'PUT'
      ) {
        return jsonResponse({
          ...providerStatus(true),
          latency_ms: 288,
        });
      }
      if (url.includes('/api/v1/providers/') && init?.method === 'DELETE') {
        deleteCompleted = true;
        return jsonResponse(undefined, 204);
      }
      if (url.endsWith('/api/v1/providers') && init?.method === undefined) {
        if (deleteCompleted && failProviderRefreshAfterDelete) {
          return jsonResponse({ message: 'refresh failed' }, 503);
        }
        return jsonResponse(providerListFactory());
      }
      if (url.endsWith('/api/v1/jobs/upload') && init?.method === 'POST') {
        const formData = init.body as FormData;
        const file = formData.get('file') as File;
        const kind = file.name.split('.').at(-1) as
          'docx' | 'pptx' | 'txt' | 'md';
        return jsonResponse(translationJob(file.name, 'job-1', kind));
      }
      if (url.endsWith('/api/v1/jobs')) return jsonResponse(initialJobs);
      return jsonResponse({ message: 'Unexpected test endpoint' }, 404);
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('并行读取本地服务、catalog、provider 与任务', async () => {
    render(<App />);

    const version = await screen.findByText('v0.1.0');
    expect(version).toBeInTheDocument();
    expect(
      version.closest('.sidebar-service')?.querySelector('svg'),
    ).toBeNull();
    expect(
      screen.getByRole('heading', { name: '文件翻译' }),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /模型服务/ })).toHaveTextContent(
      '1',
    );
    expect(screen.queryByText('暂无记录')).not.toBeInTheDocument();
    expect(globalThis.fetch).toHaveBeenCalledTimes(4);
  });

  it('将历史任务和文件翻译页彻底分开', async () => {
    initialJobs = [translationJob('old-contract.docx', 'history-1')];
    render(<App />);
    await screen.findByText('v0.1.0');

    expect(
      screen.queryByRole('button', { name: '打开文件' }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '历史记录' }));

    expect(
      screen.getByRole('heading', { name: '历史记录' }),
    ).toBeInTheDocument();
    expect(await screen.findByText('old-contract.docx')).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: '打开文件' }),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '历史记录' })).toHaveAttribute(
      'aria-current',
      'page',
    );
  });

  it('切换一级页面时保留尚未提交的文件、语言与 API Key', async () => {
    render(<App />);
    await screen.findByText('v0.1.0');
    const input = document.querySelector('input[type="file"]');
    fireEvent.change(input as HTMLInputElement, {
      target: { files: [new File(['draft'], 'draft.docx')] },
    });
    await chooseSelectOption('源语言', 'English');

    fireEvent.click(screen.getByRole('button', { name: /^模型服务/ }));
    fireEvent.change(screen.getByLabelText('API Key'), {
      target: { value: 'sk-unsaved-draft' },
    });
    fireEvent.click(screen.getByRole('button', { name: '历史记录' }));
    fireEvent.click(screen.getByRole('button', { name: /^模型服务/ }));
    expect(screen.getByLabelText('API Key')).toHaveValue('sk-unsaved-draft');

    fireEvent.click(screen.getByRole('button', { name: '文件翻译' }));
    expect(screen.getByText('draft.docx')).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: '源语言' })).toHaveTextContent(
      'English',
    );
  });

  it('通过可访问状态收起并重新展开侧边栏', async () => {
    render(<App />);
    await screen.findByText('v0.1.0');

    expect(screen.getByText('Page')).toBeInTheDocument();
    expect(screen.getByText('Ferry')).toBeInTheDocument();
    const collapseButton = screen.getByRole('button', { name: '收起侧边栏' });
    expect(collapseButton).toHaveAttribute('aria-expanded', 'true');
    collapseButton.focus();
    fireEvent.click(collapseButton);
    expect(screen.getByText('Page')).not.toBeVisible();
    expect(screen.getByText('Ferry')).not.toBeVisible();
    const expandButton = screen.getByRole('button', { name: '展开侧边栏' });
    expect(expandButton).toBe(collapseButton);
    expect(expandButton).toHaveFocus();
    expect(expandButton).toHaveAttribute('aria-expanded', 'false');
    expect(expandButton).toHaveClass('sidebar-toggle');
    expect(expandButton).not.toHaveClass('sidebar-toggle--expand');
    fireEvent.click(expandButton);
    expect(screen.getByRole('button', { name: '收起侧边栏' })).toBe(
      collapseButton,
    );
    expect(collapseButton).toHaveAttribute('aria-expanded', 'true');
  });

  it('鼠标收起侧边栏后不把展开按钮留在焦点态', async () => {
    render(<App />);
    await screen.findByText('v0.1.0');

    const collapseButton = screen.getByRole('button', { name: '收起侧边栏' });
    collapseButton.focus();
    fireEvent.pointerUp(collapseButton);
    fireEvent.click(collapseButton);

    expect(
      screen.getByRole('button', { name: '展开侧边栏' }),
    ).not.toHaveFocus();
  });

  it('让窗口拖拽带横跨在侧栏与主内容之前', async () => {
    render(<App />);
    await screen.findByText('v0.1.0');

    const titlebar = screen.getByRole('banner', { name: '窗口标题栏' });
    const dragSurface = titlebar.querySelector('.titlebar-drag-surface');

    expect(dragSurface).toHaveAttribute('data-tauri-drag-region');
    expect(titlebar.nextElementSibling).toHaveAttribute(
      'aria-label',
      '主要导航',
    );
    expect(within(titlebar).queryByText('文件翻译')).not.toBeInTheDocument();
  });

  it('在自定义下拉框中提供中文语种，并在源语言明确时交换', async () => {
    render(<App />);
    await screen.findByText('v0.1.0');

    const swapButton = screen.getByRole('button', {
      name: '交换源语言和目标语言',
    });
    expect(swapButton).toBeDisabled();

    await chooseSelectOption('源语言', 'English');
    await chooseSelectOption('目标语言', '繁體中文（香港）');
    expect(swapButton).toBeEnabled();
    fireEvent.click(swapButton);

    expect(screen.getByRole('combobox', { name: '源语言' })).toHaveTextContent(
      '繁體中文（香港）',
    );
    expect(
      screen.getByRole('combobox', { name: '目标语言' }),
    ).toHaveTextContent('English');
  });

  it('拒绝未支持格式，且不宣称支持 PDF 或 XLSX', async () => {
    render(<App />);
    await screen.findByText('v0.1.0');
    const input = document.querySelector('input[type="file"]');
    const unsupportedFile = new File(['content'], 'sample.pdf', {
      type: 'application/pdf',
    });

    expect(screen.getByText('DOCX · PPTX · TXT · MD')).toBeInTheDocument();
    expect(input).toHaveAttribute('tabindex', '-1');
    expect(input).toHaveAttribute('aria-hidden', 'true');
    expect(screen.queryByText(/PDF|XLSX/i)).not.toBeInTheDocument();
    fireEvent.change(input as HTMLInputElement, {
      target: { files: [unsupportedFile] },
    });

    expect(screen.getByRole('alert')).toHaveTextContent(
      '仅支持 DOCX、PPTX、TXT 与 Markdown。',
    );
    expect(screen.queryByRole('button', { name: '开始翻译' })).toBeNull();
  });

  it('DOCX 高级选项进入 upload payload，并只在本次任务区显示结果', async () => {
    render(<App />);
    await screen.findByText('v0.1.0');
    const input = document.querySelector('input[type="file"]');
    const file = new File(['content'], 'sample.docx', {
      type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    });

    fireEvent.change(input as HTMLInputElement, { target: { files: [file] } });
    expect(screen.getByText('准备就绪')).toBeInTheDocument();
    expect(screen.getByText('文件选项')).toBeInTheDocument();
    const tableSwitch = screen.getByRole('switch', { name: '翻译表格' });
    expect(tableSwitch).toBeChecked();
    fireEvent.click(tableSwitch);
    fireEvent.click(screen.getByRole('button', { name: '开始翻译' }));

    expect(
      await screen.findByRole('heading', { name: '本次任务' }),
    ).toBeInTheDocument();
    expect(
      within(screen.getByRole('region', { name: '文件翻译' })).getByText(
        'sample.docx',
      ),
    ).toBeInTheDocument();
    const uploadCall = vi
      .mocked(globalThis.fetch)
      .mock.calls.find(([url]) => String(url).endsWith('/api/v1/jobs/upload'));
    const formData = uploadCall?.[1]?.body as FormData;
    expect(JSON.parse(String(formData.get('options')))).toEqual({
      kind: 'docx',
      translate_tables: false,
    });

    fireEvent.click(screen.getByRole('button', { name: '历史记录' }));
    expect(
      within(screen.getByRole('region', { name: '历史记录' })).getByText(
        'sample.docx',
      ),
    ).toBeInTheDocument();
  });

  it('PPTX 默认翻译表格和 speaker notes', async () => {
    render(<App />);
    await screen.findByText('v0.1.0');
    const input = document.querySelector('input[type="file"]');
    const file = new File(['content'], 'slides.pptx');

    fireEvent.change(input as HTMLInputElement, { target: { files: [file] } });
    expect(screen.getByText('文件选项')).toBeInTheDocument();
    expect(screen.getByRole('switch', { name: '翻译表格' })).toBeChecked();
    expect(
      screen.getByRole('switch', { name: '翻译演讲者备注' }),
    ).toBeChecked();
    fireEvent.click(screen.getByRole('button', { name: '开始翻译' }));

    await screen.findByText('slides.pptx');
    const uploadCall = vi
      .mocked(globalThis.fetch)
      .mock.calls.find(([url]) => String(url).endsWith('/api/v1/jobs/upload'));
    const formData = uploadCall?.[1]?.body as FormData;
    expect(JSON.parse(String(formData.get('options')))).toEqual({
      kind: 'pptx',
      translate_tables: true,
      translate_notes: true,
    });
  });

  it('TXT 和 Markdown 不显示也不提交虚假高级选项', async () => {
    render(<App />);
    await screen.findByText('v0.1.0');
    const input = document.querySelector('input[type="file"]');
    const file = new File(['content'], 'readme.md');

    fireEvent.change(input as HTMLInputElement, { target: { files: [file] } });
    expect(screen.queryByText('文件选项')).toBeNull();
    expect(screen.queryByRole('switch')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '开始翻译' }));
    await screen.findByText('readme.md');

    const uploadCall = vi
      .mocked(globalThis.fetch)
      .mock.calls.find(([url]) => String(url).endsWith('/api/v1/jobs/upload'));
    const formData = uploadCall?.[1]?.body as FormData;
    expect(formData.has('options')).toBe(false);
  });

  it('在独立模型服务页同步模型并保存启用集合', async () => {
    configured = false;
    render(<App />);
    await screen.findByText('v0.1.0');

    fireEvent.click(screen.getByRole('button', { name: /^模型服务/ }));
    expect(
      screen.getByRole('heading', { name: '模型服务' }),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('API Key'), {
      target: { value: 'sk-test-only' },
    });
    fireEvent.click(screen.getByRole('button', { name: '同步模型' }));
    expect(await screen.findByText('DeepSeek Chat')).toBeInTheDocument();

    fireEvent.click(
      screen.getByRole('switch', { name: '启用 DeepSeek V4 Flash' }),
    );
    fireEvent.click(
      screen.getByRole('button', {
        name: '将 DeepSeek V4 Flash 设为默认模型',
      }),
    );
    fireEvent.click(screen.getByRole('button', { name: '验证并保存' }));

    expect(await screen.findByText('连接正常 · 288 ms')).toBeInTheDocument();
    const configureCall = vi
      .mocked(globalThis.fetch)
      .mock.calls.find(
        ([url, options]) =>
          String(url).endsWith('/api/v1/providers/deepseek') &&
          options?.method === 'PUT',
      );
    expect(JSON.parse(String(configureCall?.[1]?.body))).toEqual({
      enabled_model_ids: ['deepseek-v4-flash'],
      default_model_id: 'deepseek-v4-flash',
      api_key: 'sk-test-only',
    });
  });

  it('同步模型不可用时给出可访问的具体原因', async () => {
    configured = false;
    render(<App />);
    await screen.findByText('v0.1.0');
    fireEvent.click(screen.getByRole('button', { name: /^模型服务/ }));

    const syncButton = screen.getByRole('button', {
      name: /同步模型：请先输入 API Key，再同步模型列表/,
    });
    expect(syncButton).toBeDisabled();
    expect(syncButton).toHaveAttribute(
      'title',
      '请先输入 API Key，再同步模型列表',
    );
  });

  it('同步漏报旧模型时保留 unavailable 行，并允许用户关闭和切换默认模型', async () => {
    discoveryModels = [
      {
        id: 'deepseek-chat',
        display_name: 'DeepSeek Chat',
        source: 'remote',
        enabled: false,
      },
    ];
    render(<App />);
    await screen.findByText('v0.1.0');
    fireEvent.click(screen.getByRole('button', { name: /^模型服务/ }));
    fireEvent.click(screen.getByRole('button', { name: '同步模型' }));
    expect(
      await screen.findByText(/现有启用与默认选择保持不变/),
    ).toBeInTheDocument();
    const modelList = document.querySelector('.provider-model-list');
    expect(modelList).not.toBeNull();
    const unavailableRow = within(modelList as HTMLElement)
      .getByText('DeepSeek V4 Flash')
      .closest('.provider-model-row');
    expect(unavailableRow).not.toBeNull();
    expect(
      within(unavailableRow as HTMLElement).getByText('当前未返回'),
    ).toBeInTheDocument();
    expect(
      within(unavailableRow as HTMLElement).getByRole('switch', {
        name: '启用 DeepSeek V4 Flash',
      }),
    ).toBeChecked();

    fireEvent.click(screen.getByRole('switch', { name: '启用 DeepSeek Chat' }));
    fireEvent.click(
      screen.getByRole('button', {
        name: '将 DeepSeek Chat 设为默认模型',
      }),
    );
    fireEvent.click(
      within(unavailableRow as HTMLElement).getByRole('switch', {
        name: '启用 DeepSeek V4 Flash',
      }),
    );
    fireEvent.click(screen.getByRole('button', { name: '验证并保存' }));

    const configureCall = vi
      .mocked(globalThis.fetch)
      .mock.calls.find(
        ([url, options]) =>
          String(url).endsWith('/api/v1/providers/deepseek') &&
          options?.method === 'PUT',
      );
    expect(JSON.parse(String(configureCall?.[1]?.body))).toEqual({
      enabled_model_ids: ['deepseek-chat'],
      default_model_id: 'deepseek-chat',
    });
  });

  it('preset 删除成功但刷新失败时本地重置，并从翻译模型中移除', async () => {
    failProviderRefreshAfterDelete = true;
    render(<App />);
    await screen.findByText('v0.1.0');
    expect(
      screen.getByRole('combobox', { name: '翻译模型' }),
    ).toHaveTextContent('DeepSeek V4 Flash');

    fireEvent.click(screen.getByRole('button', { name: /^模型服务/ }));
    fireEvent.click(screen.getByRole('button', { name: '停用服务' }));
    fireEvent.click(screen.getByRole('button', { name: '确认停用' }));

    expect(await screen.findByText('DeepSeek 已停用。')).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'DeepSeek，未配置' }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '文件翻译' }));
    expect(
      screen.queryByRole('combobox', { name: '翻译模型' }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: '配置模型' }),
    ).toBeInTheDocument();
  });

  it('custom 删除成功但刷新失败时本地移除，并清掉翻译模型选项', async () => {
    providerListFactory = () => [providerStatus(false), customProviderStatus()];
    failProviderRefreshAfterDelete = true;
    render(<App />);
    await screen.findByText('v0.1.0');
    expect(
      screen.getByRole('combobox', { name: '翻译模型' }),
    ).toHaveTextContent('内部翻译模型');

    fireEvent.click(screen.getByRole('button', { name: /^模型服务/ }));
    fireEvent.click(screen.getByRole('button', { name: '内部网关，已激活' }));
    fireEvent.click(screen.getByRole('button', { name: '删除供应商' }));
    fireEvent.click(screen.getByRole('button', { name: '确认删除' }));

    expect(await screen.findByText('内部网关 已删除。')).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /内部网关，/ }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '文件翻译' }));
    expect(
      screen.queryByRole('combobox', { name: '翻译模型' }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: '配置模型' }),
    ).toBeInTheDocument();
  });

  it('翻译页只使用 probe 成功的 provider，并优先已保存默认模型', async () => {
    statusFactory = () => ({
      ...providerStatus(true),
      enabled_model_ids: ['deepseek-v4-flash', 'deepseek-chat'],
      default_model_id: 'deepseek-chat',
      models: [
        ...providerStatus(true).models,
        {
          id: 'deepseek-chat',
          display_name: 'DeepSeek Chat',
          source: 'remote',
          enabled: true,
        },
      ],
    });
    const healthyView = render(<App />);
    await screen.findByText('v0.1.0');
    expect(
      screen.getByRole('combobox', { name: '翻译模型' }),
    ).toHaveTextContent('DeepSeek Chat');

    healthyView.unmount();
    statusFactory = () => ({
      ...providerStatus(true),
      probe_status: 'failed',
    });
    render(<App />);
    await screen.findByText('v0.1.0');
    expect(
      screen.queryByRole('combobox', { name: '翻译模型' }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: '配置模型' }),
    ).toBeInTheDocument();
  });

  it('切换 provider 时重新隐藏未保存的 API Key', async () => {
    render(<App />);
    await screen.findByText('v0.1.0');
    fireEvent.click(screen.getByRole('button', { name: /^模型服务/ }));
    fireEvent.change(screen.getByLabelText('API Key'), {
      target: { value: 'sk-visible-draft' },
    });
    fireEvent.click(screen.getByRole('button', { name: '显示 API Key' }));
    expect(screen.getByLabelText('API Key')).toHaveAttribute('type', 'text');

    fireEvent.click(screen.getByRole('button', { name: 'Kimi，未配置' }));
    expect(screen.getByLabelText('API Key')).toHaveAttribute(
      'type',
      'password',
    );
  });

  it('运行任务只在状态里展示当前阶段与真实片段计数', async () => {
    initialJobs = [
      {
        ...translationJob('running.docx', 'running-1'),
        status: 'running',
        progress: 68,
        progress_stage: 'translating',
        processed_segments: 17,
        total_segments: 25,
        output_path: null,
      },
    ];
    render(<App />);
    await screen.findByText('v0.1.0');
    fireEvent.click(screen.getByRole('button', { name: '历史记录' }));

    const state = screen.getByLabelText(
      '任务进度：翻译文本，已处理 17 / 25 个片段',
    );
    expect(state).toHaveTextContent('翻译文本 17 / 25');
    expect(screen.queryByText('提取内容')).toBeNull();
    expect(screen.queryByText('生成文档')).toBeNull();
    expect(screen.queryByText('68%')).not.toBeInTheDocument();
    expect(document.querySelector('.job-progress-track')).toBeNull();
  });
});
