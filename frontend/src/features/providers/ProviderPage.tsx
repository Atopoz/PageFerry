/** 提供模型服务的添加、密钥配置、模型同步与启用管理页面。 */

import {
  AlertCircle,
  Check,
  CheckCircle2,
  Eye,
  EyeOff,
  ExternalLink,
  KeyRound,
  Link2,
  LoaderCircle,
  Plus,
  RefreshCw,
  RotateCcw,
  Search,
  Trash2,
  X,
} from 'lucide-react';
import { Dialog, Switch } from 'radix-ui';
import { useState, type MouseEvent } from 'react';

import { ProviderIcon } from '@/features/providers/ProviderIcon';
import {
  ApiError,
  type ConfigureProviderInput,
  type CreateCustomProviderInput,
  discoverProviderModels,
  type ModelCatalog,
  type ProviderDefinition,
  type ProviderModelStatus,
  type ProviderStatus,
} from '@/lib/api';
import { openExternalHttpUrl, safeExternalHttpUrl } from '@/lib/external-url';

interface ProviderPageProps {
  catalog: ModelCatalog | null;
  providers: ProviderStatus[];
  onCreate: (input: CreateCustomProviderInput) => Promise<ProviderStatus>;
  onSave: (
    providerId: string,
    input: ConfigureProviderInput,
  ) => Promise<ProviderStatus>;
  onDelete: (providerId: string) => Promise<void>;
}

interface ProviderDraft {
  apiKey: string;
  baseUrl: string;
  enabledModelIds: string[];
  defaultModelId: string | null;
}

interface CustomProviderDraft {
  displayName: string;
  baseUrl: string;
}

interface ProviderViewDefinition extends ProviderDefinition {
  isCustom: boolean;
  baseUrl: string;
  deletable: boolean;
}

type PendingAction = 'discover' | 'save' | 'delete' | null;

const emptyCustomProviderDraft: CustomProviderDraft = {
  displayName: '',
  baseUrl: '',
};

const presetDefinitions: readonly ProviderDefinition[] = [
  {
    id: 'deepseek',
    display_name: 'DeepSeek',
    protocol: 'openai',
    available: true,
    base_url_editable: true,
    supports_model_sync: true,
    default_base_url: 'https://api.deepseek.com',
  },
  {
    id: 'kimi',
    display_name: 'Kimi',
    protocol: 'openai',
    available: true,
    base_url_editable: true,
    supports_model_sync: true,
    default_base_url: 'https://api.moonshot.cn/v1',
  },
  {
    id: 'glm',
    display_name: '智谱 GLM',
    protocol: 'openai',
    available: true,
    base_url_editable: true,
    supports_model_sync: false,
    default_base_url: 'https://open.bigmodel.cn/api/paas/v4',
  },
  {
    id: 'minimax',
    display_name: 'MiniMax',
    protocol: 'openai',
    available: true,
    base_url_editable: true,
    supports_model_sync: true,
    default_base_url: 'https://api.minimaxi.com/v1',
  },
  {
    id: 'mimo',
    display_name: 'MiMo',
    protocol: 'openai',
    available: true,
    base_url_editable: true,
    supports_model_sync: false,
    default_base_url: 'https://api.xiaomimimo.com/v1',
  },
];

/** 合并 frontend preset、bundled catalog 与用户创建的 custom provider。 */
function providerDefinitions(
  catalog: ModelCatalog | null,
  statuses: ProviderStatus[],
): ProviderViewDefinition[] {
  const catalogById = new Map(
    (catalog?.providers ?? []).map((provider) => [provider.id, provider]),
  );
  const presets = presetDefinitions.map((preset) => {
    const definition = { ...preset, ...catalogById.get(preset.id) };
    return {
      ...definition,
      isCustom: false,
      baseUrl: definition.default_base_url ?? '',
      deletable: false,
    };
  });
  const customProviders = statuses
    .filter((status) => status.is_custom)
    .map((status) => ({
      id: status.provider_id,
      display_name: status.display_name,
      protocol: status.protocol,
      available: status.available,
      // 当前只支持创建后重新配置密钥，不把静态 endpoint 伪装成可编辑字段。
      base_url_editable: false,
      supports_model_sync: status.supports_model_sync,
      default_base_url: status.base_url,
      isCustom: true,
      baseUrl: status.base_url,
      deletable: status.deletable,
    }));
  return [...presets, ...customProviders];
}

/** 从 bundled catalog 构造尚未进行 runtime discovery 时的模型列表。 */
function catalogModels(
  catalog: ModelCatalog | null,
  providerId: string,
): ProviderModelStatus[] {
  if (catalog === null) return [];
  const modelById = new Map(catalog.models.map((model) => [model.id, model]));
  return catalog.provider_models
    .filter((binding) => binding.provider_id === providerId)
    .map((binding) => ({
      id: binding.model_id,
      display_name:
        modelById.get(binding.model_id)?.display_name ?? binding.model_id,
      source: 'catalog' as const,
      enabled: binding.enabled_by_default,
      available: true,
    }));
}

/** 从公开状态和 catalog 默认值构造不含已保存密钥的表单草稿。 */
function initialDraft(
  status: ProviderStatus | undefined,
  models: ProviderModelStatus[],
  defaultBaseUrl: string,
): ProviderDraft {
  const enabledModelIds =
    status?.enabled_model_ids.length !== 0
      ? (status?.enabled_model_ids ?? [])
      : models.filter((model) => model.enabled).map((model) => model.id);
  return {
    apiKey: '',
    baseUrl: status?.base_url.trim() || defaultBaseUrl,
    enabledModelIds,
    defaultModelId:
      status?.default_model_id ??
      enabledModelIds.at(0) ??
      models.at(0)?.id ??
      null,
  };
}

/**
 * 合并 discovery 结果与已有选择，避免上游临时漏报模型后留下不可操作的幽灵配置。
 *
 * 仅保留已启用或作为 default 的旧模型；未被本次 endpoint 返回的项会明确标记为
 * unavailable，让用户仍能关闭它，或把 default 切换到本次实际返回的模型。
 */
function mergeDiscoveredModels(
  discovered: ProviderModelStatus[],
  previous: ProviderModelStatus[],
  draft: ProviderDraft,
): ProviderModelStatus[] {
  const discoveredIds = new Set(discovered.map((model) => model.id));
  const previousById = new Map(previous.map((model) => [model.id, model]));
  const protectedIds = [...draft.enabledModelIds];
  if (
    draft.defaultModelId !== null &&
    !protectedIds.includes(draft.defaultModelId)
  ) {
    protectedIds.push(draft.defaultModelId);
  }
  const unavailableSelections = protectedIds.flatMap((modelId) => {
    if (discoveredIds.has(modelId)) return [];
    const previousModel = previousById.get(modelId);
    return [
      {
        ...(previousModel ?? {
          id: modelId,
          display_name: modelId,
          source: 'remote' as const,
        }),
        enabled: draft.enabledModelIds.includes(modelId),
        available: false,
      },
    ];
  });
  return [
    ...discovered.map((model) => ({
      ...model,
      available: model.available ?? true,
    })),
    ...unavailableSelections,
  ];
}

/** 把 provider probe 错误转换为不泄露上游正文的用户提示。 */
function providerErrorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) {
    return error instanceof Error ? error.message : '模型服务操作失败。';
  }
  const messages: Record<string, string> = {
    key: 'API Key 无效或没有访问权限。',
    endpoint: 'Provider endpoint 当前不可用。',
    model: '当前账号无法使用所选模型。',
    rate_limit: '请求过于频繁，请稍后再试。',
    network: '无法连接模型服务，请检查网络。',
    protocol: '模型服务返回了无法识别的响应。',
  };
  return (error.code && messages[error.code]) || error.message;
}

/** 返回 provider 的短连接状态。 */
function providerStateLabel(status: ProviderStatus | undefined): string {
  if (status?.configured && status.probe_status === 'succeeded')
    return '已激活';
  if (status?.probe_status === 'failed') return '验证失败';
  return '未配置';
}

/** 判断 HTTP endpoint 是否明确落在本机 loopback，避免明文发送 Bearer Key。 */
function isLoopbackHostname(hostname: string): boolean {
  const normalized = hostname.toLowerCase().replace(/^\[|\]$/g, '');
  if (normalized === 'localhost' || normalized.endsWith('.localhost')) {
    return true;
  }
  if (normalized === '::1') return true;

  const octets = normalized.split('.').map(Number);
  return (
    octets.length === 4 &&
    octets.every(
      (octet) => Number.isInteger(octet) && octet >= 0 && octet <= 255,
    ) &&
    octets[0] === 127
  );
}

/** 去掉 endpoint 尾部斜杠，避免同一地址产生只差分隔符的 override。 */
function normalizeProviderBaseUrl(rawUrl: string): string {
  return rawUrl.trim().replace(/\/+$/, '');
}

/** 在提交前对齐 backend 对 Base URL 的安全边界。 */
function providerBaseUrlError(rawUrl: string): string | null {
  const candidate = rawUrl.trim();
  if (!candidate) return '请输入 API 地址。';
  if (
    candidate.length > 2048 ||
    /\s/.test(candidate) ||
    candidate.includes('\\') ||
    candidate.includes('?') ||
    candidate.includes('#')
  ) {
    return 'API 地址格式无效。';
  }
  try {
    const url = new URL(candidate);
    if (url.protocol !== 'http:' && url.protocol !== 'https:') {
      return 'API 地址必须使用 http 或 https。';
    }
    if (url.username || url.password) return 'API 地址不能包含账号信息。';
    if (url.protocol === 'http:' && !isLoopbackHostname(url.hostname)) {
      return '公网或局域网服务必须使用 HTTPS；HTTP 仅支持本机 loopback。';
    }
  } catch {
    return '请输入完整的 API 地址。';
  }
  return null;
}

/** 同时校验 custom provider 的名称与 endpoint。 */
function customProviderFormError(draft: CustomProviderDraft): string | null {
  const displayName = draft.displayName.trim();
  if (!displayName) return '请输入供应商名称。';
  if (displayName.length > 80) return '供应商名称不能超过 80 个字符。';
  return providerBaseUrlError(draft.baseUrl);
}

/** 独立模型服务页，保持 provider 列表和当前配置上下文同时可见。 */
export function ProviderPage({
  catalog,
  providers,
  onCreate,
  onSave,
  onDelete,
}: ProviderPageProps) {
  const definitions = providerDefinitions(catalog, providers);
  const statuses = new Map(
    providers.map((provider) => [provider.provider_id, provider]),
  );
  const [selectedProviderId, setSelectedProviderId] = useState('deepseek');
  const [query, setQuery] = useState('');
  const [customDialogOpen, setCustomDialogOpen] = useState(false);
  const [customDraft, setCustomDraft] = useState<CustomProviderDraft>(
    emptyCustomProviderDraft,
  );
  const [customFormNotice, setCustomFormNotice] = useState<string | null>(null);
  const [creatingCustom, setCreatingCustom] = useState(false);
  const [showSecret, setShowSecret] = useState(false);
  const [pendingAction, setPendingAction] = useState<PendingAction>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [noticeTone, setNoticeTone] = useState<'success' | 'error'>('success');
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [discoveredModels, setDiscoveredModels] = useState<
    Record<string, ProviderModelStatus[]>
  >({});
  const [drafts, setDrafts] = useState<Record<string, ProviderDraft>>({});

  const selectedDefinition =
    definitions.find((provider) => provider.id === selectedProviderId) ??
    definitions[0];
  const selectedStatus = statuses.get(selectedProviderId);
  const apiKeyUrl = safeExternalHttpUrl(selectedDefinition?.api_key_url);
  const docsUrl = safeExternalHttpUrl(selectedDefinition?.docs_url);
  const bundledModels = catalogModels(catalog, selectedProviderId);
  const models =
    discoveredModels[selectedProviderId] ??
    (selectedStatus?.models.length ? selectedStatus.models : bundledModels);
  const defaultBaseUrl = selectedDefinition?.baseUrl ?? '';
  const draft =
    drafts[selectedProviderId] ??
    initialDraft(selectedStatus, models, defaultBaseUrl);
  const baseUrlError = selectedDefinition?.isCustom
    ? null
    : providerBaseUrlError(draft.baseUrl);
  const normalizedDraftBaseUrl = normalizeProviderBaseUrl(draft.baseUrl);
  const normalizedDefaultBaseUrl = normalizeProviderBaseUrl(defaultBaseUrl);
  const baseUrlUsesDefault =
    normalizedDraftBaseUrl === normalizedDefaultBaseUrl;
  const baseUrlChanged =
    !selectedDefinition?.isCustom &&
    normalizedDraftBaseUrl !==
      normalizeProviderBaseUrl(
        selectedStatus?.base_url.trim() || defaultBaseUrl,
      );
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const visibleDefinitions = definitions.filter((definition) =>
    definition.display_name.toLocaleLowerCase().includes(normalizedQuery),
  );
  const visiblePresets = visibleDefinitions.filter(
    (definition) => !definition.isCustom,
  );
  const visibleCustomProviders = visibleDefinitions.filter(
    (definition) => definition.isCustom,
  );
  const configured = selectedStatus?.configured === true;
  const healthy = configured && selectedStatus.probe_status === 'succeeded';
  const supportsModelSync =
    selectedStatus?.supports_model_sync ??
    selectedDefinition?.supports_model_sync ??
    false;
  const syncDisabledReason = !supportsModelSync
    ? '该 provider 使用内置模型目录，无需远程同步'
    : baseUrlError !== null
      ? '请先检查 Base URL'
      : !configured && !draft.apiKey.trim()
        ? '请先输入 API Key，再同步模型列表'
        : pendingAction !== null
          ? '请等待当前操作完成'
          : null;

  /** 合并当前 provider 的部分草稿，避免切换 provider 时丢失未保存设置。 */
  function updateDraft(patch: Partial<ProviderDraft>) {
    setDrafts((current) => ({
      ...current,
      [selectedProviderId]: {
        ...(current[selectedProviderId] ??
          initialDraft(selectedStatus, models, defaultBaseUrl)),
        ...patch,
      },
    }));
  }

  /** 切换右侧配置上下文，并重新隐藏上一项尚未保存的密钥。 */
  function selectProvider(providerId: string) {
    setSelectedProviderId(providerId);
    setShowSecret(false);
    setConfirmingDelete(false);
    setNotice(null);
  }

  /** 通过系统默认浏览器打开 provider 外链，并把 opener 失败留在当前页面提示。 */
  async function openProviderExternalPage(
    event: MouseEvent<HTMLAnchorElement>,
    url: string,
  ) {
    event.preventDefault();
    setNotice(null);
    try {
      await openExternalHttpUrl(url);
    } catch (error) {
      setNotice(
        error instanceof Error ? error.message : '系统无法打开外部链接。',
      );
      setNoticeTone('error');
    }
  }

  /** 创建 custom provider 定义，随后直接进入同一套 Key 与模型配置区。 */
  async function createCustom() {
    const validationError = customProviderFormError(customDraft);
    if (validationError !== null) {
      setCustomFormNotice(validationError);
      return;
    }

    setCreatingCustom(true);
    setCustomFormNotice(null);
    try {
      const created = await onCreate({
        display_name: customDraft.displayName.trim(),
        base_url: normalizeProviderBaseUrl(customDraft.baseUrl),
      });
      setCustomDialogOpen(false);
      setCustomDraft(emptyCustomProviderDraft);
      selectProvider(created.provider_id);
      setNotice('供应商已创建，请输入 API Key 并同步模型。');
      setNoticeTone('success');
    } catch (error) {
      setCustomFormNotice(providerErrorMessage(error));
    } finally {
      setCreatingCustom(false);
    }
  }

  /** 更新模型启用集合；关闭默认模型时自动选择下一个已启用模型。 */
  function setModelEnabled(modelId: string, enabled: boolean) {
    const enabledModelIds = enabled
      ? [...new Set([...draft.enabledModelIds, modelId])]
      : draft.enabledModelIds.filter((currentId) => currentId !== modelId);
    updateDraft({
      enabledModelIds,
      defaultModelId:
        draft.defaultModelId === modelId && !enabled
          ? (enabledModelIds.at(0) ?? null)
          : (draft.defaultModelId ?? enabledModelIds.at(0) ?? null),
    });
  }

  /** 使用尚未保存的 Key 或 Keychain 中已有密钥执行 model discovery。 */
  async function discoverModels() {
    setPendingAction('discover');
    setNotice(null);
    try {
      const result = await discoverProviderModels(selectedProviderId, {
        ...(draft.apiKey.trim() ? { api_key: draft.apiKey.trim() } : {}),
        ...(!selectedDefinition?.isCustom
          ? {
              base_url: baseUrlUsesDefault ? null : normalizedDraftBaseUrl,
            }
          : {}),
      });
      setDiscoveredModels((current) => ({
        ...current,
        [selectedProviderId]: mergeDiscoveredModels(
          result.models,
          models,
          draft,
        ),
      }));
      setNotice(
        `已同步 ${result.models.length} 个模型，现有启用与默认选择保持不变。`,
      );
      setNoticeTone('success');
    } catch (error) {
      setNotice(providerErrorMessage(error));
      setNoticeTone('error');
    } finally {
      setPendingAction(null);
    }
  }

  /** 对默认模型执行真实 probe，成功后才提交密钥与模型选择。 */
  async function saveProvider() {
    setPendingAction('save');
    setNotice(null);
    try {
      const next = await onSave(selectedProviderId, {
        enabled_model_ids: draft.enabledModelIds,
        default_model_id: draft.defaultModelId,
        ...(draft.apiKey.trim() ? { api_key: draft.apiKey.trim() } : {}),
        ...(baseUrlChanged
          ? {
              base_url: baseUrlUsesDefault ? null : normalizedDraftBaseUrl,
            }
          : {}),
      });
      setDrafts((current) => ({
        ...current,
        [selectedProviderId]: {
          apiKey: '',
          baseUrl: next.base_url,
          enabledModelIds: next.enabled_model_ids,
          defaultModelId: next.default_model_id,
        },
      }));
      setNotice(
        next.latency_ms === null
          ? '配置已保存，连接正常。'
          : `连接正常 · ${next.latency_ms} ms`,
      );
      setNoticeTone('success');
    } catch (error) {
      setNotice(providerErrorMessage(error));
      setNoticeTone('error');
    } finally {
      setPendingAction(null);
    }
  }

  /** 二次确认后停用 preset，或完整删除 custom provider。 */
  async function removeProvider() {
    if (!confirmingDelete) {
      setConfirmingDelete(true);
      return;
    }
    setPendingAction('delete');
    try {
      await onDelete(selectedProviderId);
      setDrafts((current) => ({
        ...current,
        [selectedProviderId]: initialDraft(undefined, models, defaultBaseUrl),
      }));
      setConfirmingDelete(false);
      if (selectedDefinition?.isCustom) {
        setSelectedProviderId('deepseek');
        setNotice(`${selectedDefinition.display_name} 已删除。`);
      } else {
        setNotice(
          `${selectedDefinition?.display_name ?? selectedProviderId} 已停用。`,
        );
      }
      setNoticeTone('success');
    } catch (error) {
      setNotice(providerErrorMessage(error));
      setNoticeTone('error');
    } finally {
      setPendingAction(null);
    }
  }

  /** 渲染左栏中的一个稳定 provider 入口。 */
  function renderProviderItem(definition: ProviderViewDefinition) {
    const status = statuses.get(definition.id);
    const selected = definition.id === selectedProviderId;
    const stateLabel = providerStateLabel(status);
    return (
      <button
        className={`provider-list-item ${selected ? 'provider-list-item--selected' : ''}`}
        type="button"
        key={definition.id}
        aria-label={`${definition.display_name}，${stateLabel}`}
        aria-pressed={selected}
        onClick={() => selectProvider(definition.id)}
      >
        <ProviderIcon
          providerId={definition.id}
          displayName={definition.display_name}
          size={28}
        />
        <span>
          <strong>{definition.display_name}</strong>
          <small>{stateLabel}</small>
        </span>
        <i
          className={
            status?.configured && status.probe_status === 'succeeded'
              ? 'provider-dot provider-dot--connected'
              : 'provider-dot'
          }
          aria-hidden="true"
        />
      </button>
    );
  }

  return (
    <section className="provider-page" aria-labelledby="provider-page-title">
      <aside className="provider-list-pane" aria-label="模型供应商">
        <header className="provider-list-header">
          <div>
            <h1 id="provider-page-title">模型服务</h1>
            <span>
              {definitions.length} 个供应商 ·{' '}
              {providers.filter((provider) => provider.configured).length}{' '}
              个已激活
            </span>
          </div>
        </header>

        <label className="provider-search">
          <Search aria-hidden="true" size={15} />
          <span className="visually-hidden">搜索模型服务</span>
          <input
            type="search"
            value={query}
            placeholder="搜索供应商"
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>

        <div className="provider-list">
          <section
            className="provider-list-group"
            aria-labelledby="preset-provider-title"
          >
            <h2 id="preset-provider-title">预设供应商</h2>
            {visiblePresets.map(renderProviderItem)}
          </section>

          {visibleCustomProviders.length > 0 ? (
            <section
              className="provider-list-group"
              aria-labelledby="custom-provider-title"
            >
              <h2 id="custom-provider-title">自定义</h2>
              {visibleCustomProviders.map(renderProviderItem)}
            </section>
          ) : null}

          {visibleDefinitions.length === 0 ? (
            <p className="provider-list-empty">没有匹配的供应商</p>
          ) : null}
        </div>

        <Dialog.Root
          open={customDialogOpen}
          onOpenChange={(open) => {
            setCustomDialogOpen(open);
            if (!open) setCustomFormNotice(null);
          }}
        >
          <Dialog.Trigger asChild>
            <button className="add-provider-button" type="button">
              <Plus aria-hidden="true" size={16} />
              添加自定义供应商
            </button>
          </Dialog.Trigger>
          <Dialog.Portal>
            <Dialog.Overlay className="dialog-overlay" />
            <Dialog.Content
              className="provider-custom-dialog"
              aria-describedby="custom-provider-description"
            >
              <header>
                <div>
                  <Dialog.Title>自定义供应商</Dialog.Title>
                  <Dialog.Description id="custom-provider-description">
                    使用 OpenAI-compatible API
                  </Dialog.Description>
                </div>
                <Dialog.Close asChild>
                  <button
                    className="icon-button"
                    type="button"
                    aria-label="关闭"
                  >
                    <X aria-hidden="true" size={17} />
                  </button>
                </Dialog.Close>
              </header>

              <form
                className="provider-custom-form"
                onSubmit={(event) => {
                  event.preventDefault();
                  void createCustom();
                }}
              >
                <label>
                  <span>供应商名称</span>
                  <input
                    autoFocus
                    type="text"
                    value={customDraft.displayName}
                    placeholder="例如：公司内部网关"
                    onChange={(event) =>
                      setCustomDraft((current) => ({
                        ...current,
                        displayName: event.target.value,
                      }))
                    }
                  />
                </label>
                <label>
                  <span>API 地址</span>
                  <input
                    type="url"
                    value={customDraft.baseUrl}
                    placeholder="https://api.example.com/v1"
                    onChange={(event) =>
                      setCustomDraft((current) => ({
                        ...current,
                        baseUrl: event.target.value,
                      }))
                    }
                  />
                </label>
                {customFormNotice ? (
                  <p className="provider-custom-error" role="alert">
                    <AlertCircle aria-hidden="true" size={14} />
                    {customFormNotice}
                  </p>
                ) : null}
                <footer>
                  <Dialog.Close asChild>
                    <button className="provider-custom-cancel" type="button">
                      取消
                    </button>
                  </Dialog.Close>
                  <button
                    className="provider-custom-submit"
                    type="submit"
                    disabled={creatingCustom}
                  >
                    {creatingCustom ? (
                      <LoaderCircle
                        className="spin"
                        aria-hidden="true"
                        size={15}
                      />
                    ) : (
                      <Plus aria-hidden="true" size={15} />
                    )}
                    {creatingCustom ? '正在创建' : '创建并配置'}
                  </button>
                </footer>
              </form>
            </Dialog.Content>
          </Dialog.Portal>
        </Dialog.Root>
      </aside>

      <div className="provider-detail-pane">
        <header className="provider-detail-header">
          <span className="provider-detail-logo">
            <ProviderIcon
              providerId={selectedProviderId}
              displayName={selectedDefinition?.display_name}
              size={38}
            />
          </span>
          <div>
            <h2>{selectedDefinition?.display_name ?? selectedProviderId}</h2>
            <span>{selectedDefinition?.protocol.toUpperCase()} API</span>
          </div>
          <span
            className={`provider-health ${healthy ? 'provider-health--ok' : ''}`}
          >
            {healthy ? <CheckCircle2 aria-hidden="true" size={14} /> : null}
            {providerStateLabel(selectedStatus)}
          </span>
        </header>

        <div className="provider-detail-scroll">
          {selectedDefinition ? (
            <section className="provider-endpoint-section">
              <div className="provider-endpoint-heading">
                <div>
                  <h3>Base URL</h3>
                  <span>
                    {selectedDefinition.isCustom
                      ? 'OpenAI-compatible · 创建后固定'
                      : baseUrlUsesDefault
                        ? '使用预置地址'
                        : '使用自定义覆盖地址'}
                  </span>
                </div>
                {!selectedDefinition.isCustom && !baseUrlUsesDefault ? (
                  <button
                    type="button"
                    onClick={() => updateDraft({ baseUrl: defaultBaseUrl })}
                  >
                    <RotateCcw aria-hidden="true" size={12} />
                    恢复默认
                  </button>
                ) : null}
              </div>

              {selectedDefinition.isCustom ? (
                <div className="provider-endpoint-readonly">
                  <Link2 aria-hidden="true" size={15} />
                  <code title={selectedDefinition.baseUrl}>
                    {selectedDefinition.baseUrl}
                  </code>
                </div>
              ) : (
                <label className="provider-endpoint-input">
                  <Link2 aria-hidden="true" size={15} />
                  <input
                    type="url"
                    aria-label="Base URL"
                    aria-invalid={baseUrlError !== null}
                    autoComplete="off"
                    spellCheck="false"
                    value={draft.baseUrl}
                    placeholder={defaultBaseUrl}
                    onChange={(event) =>
                      updateDraft({ baseUrl: event.target.value })
                    }
                  />
                </label>
              )}
              {baseUrlError ? (
                <p className="provider-endpoint-error" role="alert">
                  {baseUrlError}
                </p>
              ) : null}
            </section>
          ) : null}

          <section className="provider-config-section">
            <div className="provider-section-heading">
              <div>
                <h3 id="credential-title">API Key</h3>
                <span>
                  {configured
                    ? '已安全存入系统 Keychain'
                    : '仅在保存时提交给本地 sidecar'}
                </span>
              </div>
              {apiKeyUrl ? (
                <a
                  href={apiKeyUrl}
                  target="_blank"
                  rel="noreferrer"
                  onClick={(event) =>
                    void openProviderExternalPage(event, apiKeyUrl)
                  }
                >
                  获取密钥
                  <ExternalLink aria-hidden="true" size={12} />
                </a>
              ) : null}
            </div>

            <div className="provider-secret-row">
              <span className="provider-secret-input">
                <KeyRound aria-hidden="true" size={16} />
                <input
                  type={showSecret ? 'text' : 'password'}
                  autoComplete="off"
                  spellCheck="false"
                  aria-label="API Key"
                  value={draft.apiKey}
                  placeholder={
                    configured ? '留空可沿用现有密钥' : '输入 API Key'
                  }
                  onChange={(event) =>
                    updateDraft({ apiKey: event.target.value })
                  }
                />
                <button
                  type="button"
                  aria-label={showSecret ? '隐藏 API Key' : '显示 API Key'}
                  onClick={() => setShowSecret((current) => !current)}
                >
                  {showSecret ? (
                    <EyeOff aria-hidden="true" size={16} />
                  ) : (
                    <Eye aria-hidden="true" size={16} />
                  )}
                </button>
              </span>
            </div>
          </section>

          <section
            className="provider-model-section"
            aria-labelledby="provider-models-title"
          >
            <div className="provider-section-heading provider-model-heading">
              <div>
                <h3 id="provider-models-title">模型</h3>
                <span>
                  {models.length} 个可见，{draft.enabledModelIds.length}{' '}
                  个已启用
                  {!supportsModelSync ? ' · 内置模型目录，无需同步' : ''}
                </span>
              </div>
              <button
                className="sync-model-button"
                type="button"
                disabled={syncDisabledReason !== null}
                title={syncDisabledReason ?? undefined}
                aria-label={
                  syncDisabledReason
                    ? `同步模型：${syncDisabledReason}`
                    : '同步模型'
                }
                onClick={() => void discoverModels()}
              >
                {pendingAction === 'discover' ? (
                  <LoaderCircle className="spin" aria-hidden="true" size={15} />
                ) : (
                  <RefreshCw aria-hidden="true" size={15} />
                )}
                {pendingAction === 'discover' ? '同步中' : '同步模型'}
              </button>
            </div>

            <div className="provider-model-list">
              {models.length === 0 ? (
                <div className="provider-model-empty">
                  <AlertCircle aria-hidden="true" size={17} />
                  <span>输入 API Key 后同步模型列表</span>
                </div>
              ) : null}
              {models.map((model) => {
                const enabled = draft.enabledModelIds.includes(model.id);
                const isDefault = draft.defaultModelId === model.id;
                const available = model.available !== false;
                return (
                  <div
                    className={`provider-model-row ${available ? '' : 'provider-model-row--unavailable'}`}
                    key={model.id}
                  >
                    <ProviderIcon
                      providerId={selectedProviderId}
                      displayName={selectedDefinition?.display_name}
                      size={24}
                    />
                    <div>
                      <strong>{model.display_name ?? model.id}</strong>
                      <span className="provider-model-id">
                        {model.id}
                        {!available ? <em>当前未返回</em> : null}
                      </span>
                    </div>
                    <button
                      className={`model-default-button ${isDefault ? 'model-default-button--active' : ''}`}
                      type="button"
                      disabled={!enabled || !available}
                      aria-label={`将 ${model.display_name ?? model.id} 设为默认模型`}
                      aria-pressed={isDefault}
                      onClick={() => updateDraft({ defaultModelId: model.id })}
                    >
                      <Check aria-hidden="true" size={14} />
                      {isDefault ? '默认' : '设为默认'}
                    </button>
                    <Switch.Root
                      className="model-switch"
                      checked={enabled}
                      aria-label={`启用 ${model.display_name ?? model.id}`}
                      onCheckedChange={(checked) =>
                        setModelEnabled(model.id, checked)
                      }
                    >
                      <Switch.Thumb />
                    </Switch.Root>
                  </div>
                );
              })}
            </div>
          </section>

          {notice ? (
            <p
              className={`provider-notice provider-notice--${noticeTone}`}
              role="status"
            >
              {noticeTone === 'success' ? (
                <CheckCircle2 aria-hidden="true" size={15} />
              ) : (
                <AlertCircle aria-hidden="true" size={15} />
              )}
              {notice}
            </p>
          ) : null}
        </div>

        <footer className="provider-detail-footer">
          <span>
            {docsUrl ? (
              <a
                href={docsUrl}
                target="_blank"
                rel="noreferrer"
                onClick={(event) =>
                  void openProviderExternalPage(event, docsUrl)
                }
              >
                API 文档
                <ExternalLink aria-hidden="true" size={12} />
              </a>
            ) : null}
            {selectedDefinition?.isCustom || configured ? (
              <button
                className={
                  confirmingDelete
                    ? 'provider-delete provider-delete--confirm'
                    : 'provider-delete'
                }
                type="button"
                disabled={pendingAction !== null}
                onClick={() => void removeProvider()}
              >
                {pendingAction === 'delete' ? (
                  <LoaderCircle className="spin" aria-hidden="true" size={14} />
                ) : (
                  <Trash2 aria-hidden="true" size={14} />
                )}
                {selectedDefinition?.isCustom
                  ? confirmingDelete
                    ? '确认删除'
                    : '删除供应商'
                  : confirmingDelete
                    ? '确认停用'
                    : '停用服务'}
              </button>
            ) : null}
          </span>
          <button
            className="provider-save-button"
            type="button"
            disabled={
              pendingAction !== null ||
              baseUrlError !== null ||
              draft.enabledModelIds.length === 0 ||
              draft.defaultModelId === null ||
              (!configured && !draft.apiKey.trim())
            }
            onClick={() => void saveProvider()}
          >
            {pendingAction === 'save' ? (
              <LoaderCircle className="spin" aria-hidden="true" size={15} />
            ) : (
              <Check aria-hidden="true" size={15} />
            )}
            {pendingAction === 'save' ? '正在验证' : '验证并保存'}
          </button>
        </footer>
      </div>
    </section>
  );
}
