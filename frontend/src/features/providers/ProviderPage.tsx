/** 提供模型服务的添加、密钥配置、模型同步与启用管理页面。 */

import {
  Activity,
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
  Save,
  Search,
  Settings2,
  Trash2,
  X,
} from 'lucide-react';
import { Dialog, Switch } from 'radix-ui';
import { useEffect, useRef, useState, type MouseEvent } from 'react';

import { ProviderIcon } from '@/features/providers/ProviderIcon';
import {
  ApiError,
  type ConfigureProviderInput,
  type CreateProviderModelInput,
  type CreateCustomProviderInput,
  discoverProviderModels,
  type ModelCatalog,
  type ProbeProviderInput,
  type ProviderDefinition,
  type ProviderModelStatus,
  type ProviderModelSync,
  type ProviderProbeResult,
  type ProviderStatus,
  type ReasoningPolicy,
  type UpdateProviderModelSettingsInput,
} from '@/lib/api';
import { openExternalHttpUrl, safeExternalHttpUrl } from '@/lib/external-url';

export type ProviderSaveInput = Omit<
  ConfigureProviderInput,
  'enabled_model_ids'
> & {
  enabled_model_ids?: string[];
  enable_all_models?: boolean;
};

interface ProviderPageProps {
  catalog: ModelCatalog | null;
  providers: ProviderStatus[];
  onCreate: (input: CreateCustomProviderInput) => Promise<ProviderStatus>;
  onAddModel: (
    providerId: string,
    input: CreateProviderModelInput,
  ) => Promise<ProviderModelStatus>;
  onLoadApiKey: (providerId: string, signal?: AbortSignal) => Promise<string>;
  onProbe: (
    providerId: string,
    input: ProbeProviderInput,
  ) => Promise<ProviderProbeResult>;
  onSave: (
    providerId: string,
    input: ProviderSaveInput,
  ) => Promise<ProviderStatus>;
  onProviderActiveChange: (
    providerId: string,
    active: boolean,
  ) => Promise<ProviderStatus>;
  onModelEnabledChange: (
    providerId: string,
    modelId: string,
    enabled: boolean,
  ) => Promise<ProviderStatus>;
  onSyncModels: (providerId: string) => Promise<ProviderModelSync>;
  onSaveModelSettings: (
    providerId: string,
    modelId: string,
    input: UpdateProviderModelSettingsInput,
  ) => Promise<ProviderModelStatus>;
  onDelete: (providerId: string) => Promise<void>;
}

interface ProviderDraft {
  baseUrl: string;
  enabledModelIds: string[];
  defaultModelId: string | null;
}

interface CredentialState {
  providerId: string;
  value: string;
  baseline: string | null;
  status: 'empty' | 'loading' | 'ready' | 'error';
  notice: string | null;
  noticeTone: 'success' | 'error';
}

interface CustomProviderDraft {
  displayName: string;
  baseUrl: string;
}

interface ManualModelDraft {
  modelId: string;
  displayName: string;
}

interface ModelSettingsDraft {
  reasoningPolicy: ReasoningPolicy | null;
  perJobConcurrency: string;
  globalConcurrency: string;
}

interface ProviderViewDefinition extends ProviderDefinition {
  isCustom: boolean;
  baseUrl: string;
  deletable: boolean;
}

type PendingAction =
  | 'discover'
  | 'sync'
  | 'add_model'
  | 'provider_active'
  | 'model_enabled'
  | 'probe'
  | 'save'
  | 'settings'
  | 'delete'
  | null;

const DEFAULT_PER_JOB_CONCURRENCY = 6;
const DEFAULT_GLOBAL_CONCURRENCY = 15;

const reasoningPolicyLabels: Record<ReasoningPolicy, string> = {
  provider_default: '供应商默认',
  off: '关闭思考',
  on: '开启思考',
  low: '低',
  medium: '中',
  high: '高',
  max: '最高',
};

const emptyCustomProviderDraft: CustomProviderDraft = {
  displayName: '',
  baseUrl: '',
};

const emptyManualModelDraft: ManualModelDraft = {
  modelId: '',
  displayName: '',
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
      reasoning_policy: 'provider_default' as const,
      reasoning_policy_override: null,
      supported_reasoning_policies: ['provider_default' as const],
      per_job_concurrency: DEFAULT_PER_JOB_CONCURRENCY,
      per_job_concurrency_override: null,
      global_concurrency: DEFAULT_GLOBAL_CONCURRENCY,
      global_concurrency_override: null,
    }));
}

/** 从公开状态和 catalog 默认值构造不含已保存密钥的表单草稿。 */
function initialDraft(
  status: ProviderStatus | undefined,
  models: ProviderModelStatus[],
  defaultBaseUrl: string,
  bundledDefaultModelIds: string[] = [],
): ProviderDraft {
  const visibleModelIds = new Set(models.map((model) => model.id));
  const catalogDefaults = bundledDefaultModelIds.filter((modelId) =>
    visibleModelIds.has(modelId),
  );
  const enabledModelIds = status?.enabled_model_ids.length
    ? status.enabled_model_ids
    : status?.configured !== true && catalogDefaults.length
      ? catalogDefaults
      : models.filter((model) => model.enabled).map((model) => model.id);
  return {
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
          reasoning_policy: 'provider_default' as const,
          reasoning_policy_override: null,
          supported_reasoning_policies: ['provider_default' as const],
          per_job_concurrency: DEFAULT_PER_JOB_CONCURRENCY,
          per_job_concurrency_override: null,
          global_concurrency: DEFAULT_GLOBAL_CONCURRENCY,
          global_concurrency_override: null,
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

/** 从后端有效值构造可编辑表单；override 是否存在仍由原模型状态判断。 */
function initialModelSettingsDraft(
  model: ProviderModelStatus,
): ModelSettingsDraft {
  return {
    reasoningPolicy:
      model.reasoning_policy ??
      model.supported_reasoning_policies.at(0) ??
      null,
    perJobConcurrency: String(model.per_job_concurrency),
    globalConcurrency: String(model.global_concurrency),
  };
}

/** 校验模型并发上限，保持与 backend 的窄 contract 一致。 */
function modelSettingsError(draft: ModelSettingsDraft | null): string | null {
  if (draft === null) return null;
  const perJobRaw = draft.perJobConcurrency.trim();
  const globalRaw = draft.globalConcurrency.trim();
  if (!/^\d+$/.test(perJobRaw) || !/^\d+$/.test(globalRaw)) {
    return '并发上限必须是 1 到 32 的整数。';
  }
  const perJob = Number(perJobRaw);
  const global = Number(globalRaw);
  if (perJob < 1 || global < 1 || perJob > 32 || global > 32) {
    return '并发上限必须在 1 到 32 之间。';
  }
  if (perJob > global) return '单任务并发不能超过跨任务总并发。';
  return null;
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
    model_required: '至少保留一个已启用且验证通过的模型。',
    rate_limit: '请求过于频繁，请稍后再试。',
    network: '无法连接模型服务，请检查网络。',
    protocol: '模型服务返回了无法识别的响应。',
    conflict: '模型服务配置已变化，请重试当前操作。',
  };
  return (error.code && messages[error.code]) || error.message;
}

/** 只有已配置且明确 active 的 provider 才能参与新任务。 */
function providerIsActive(status: ProviderStatus | undefined): boolean {
  return status?.configured === true && status.active;
}

/** 返回 provider 的短运行状态，左栏不重复放置开关。 */
function providerStateLabel(status: ProviderStatus | undefined): string {
  if (status?.configured !== true) return '未配置';
  if (status.probe_status === 'failed') return '异常';
  return providerIsActive(status) ? '已启用' : '已停用';
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

/** 识别 ASCII control 与 DEL，避免不可见字符进入持久化 model identity。 */
function containsControlCharacter(value: string): boolean {
  return [...value].some((character) => {
    const codePoint = character.codePointAt(0) ?? 0;
    return codePoint <= 0x1f || codePoint === 0x7f;
  });
}

/** 校验手动 model 身份，并提前拦住当前列表中的完全重复项。 */
function manualModelFormError(
  draft: ManualModelDraft,
  models: ProviderModelStatus[],
): string | null {
  const modelId = draft.modelId.trim();
  const displayName = draft.displayName.trim();
  if (!modelId) return '请输入模型 ID。';
  if (modelId.length > 256) return '模型 ID 不能超过 256 个字符。';
  if (/\s/u.test(modelId) || containsControlCharacter(modelId)) {
    return '模型 ID 不能包含空白字符或控制字符。';
  }
  if (displayName.length > 120) return '显示名称不能超过 120 个字符。';
  if (containsControlCharacter(draft.displayName)) {
    return '显示名称不能包含控制字符。';
  }
  if (models.some((model) => model.id === modelId)) {
    return '该模型已在当前列表中。';
  }
  return null;
}

/** 把手动添加的重复竞态转换成明确提示，其余错误沿用 provider 安全文案。 */
function manualModelRequestError(error: unknown): string {
  if (error instanceof ApiError && error.status === 409) {
    return '该模型已存在。';
  }
  return providerErrorMessage(error);
}

/** 独立模型服务页，保持 provider 列表和当前配置上下文同时可见。 */
export function ProviderPage({
  catalog,
  providers,
  onCreate,
  onAddModel,
  onLoadApiKey,
  onProbe,
  onSave,
  onProviderActiveChange,
  onModelEnabledChange,
  onSyncModels,
  onSaveModelSettings,
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
  const [manualModelDialogOpen, setManualModelDialogOpen] = useState(false);
  const [manualModelDraft, setManualModelDraft] = useState<ManualModelDraft>(
    emptyManualModelDraft,
  );
  const [manualModelFormNotice, setManualModelFormNotice] = useState<
    string | null
  >(null);
  const [showSecret, setShowSecret] = useState(false);
  const [credential, setCredential] = useState<CredentialState>({
    providerId: 'deepseek',
    value: '',
    baseline: null,
    status: 'empty',
    notice: null,
    noticeTone: 'success',
  });
  const skipNextSecretLoadRef = useRef<string | null>(null);
  const [pendingAction, setPendingAction] = useState<PendingAction>(null);
  const pendingActionRef = useRef<Exclude<PendingAction, null> | null>(null);
  const [pendingModelId, setPendingModelId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [noticeTone, setNoticeTone] = useState<'success' | 'error'>('success');
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [settingsModelId, setSettingsModelId] = useState<string | null>(null);
  const [modelSettingsDraft, setModelSettingsDraft] =
    useState<ModelSettingsDraft | null>(null);
  const [settingsRequestError, setSettingsRequestError] = useState<
    string | null
  >(null);
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
  const bundledDefaultModelIds = bundledModels
    .filter((model) => model.enabled)
    .map((model) => model.id);
  const models =
    discoveredModels[selectedProviderId] ??
    (selectedStatus?.models.length ? selectedStatus.models : bundledModels);
  const settingsModel =
    settingsModelId === null
      ? undefined
      : models.find((model) => model.id === settingsModelId);
  const settingsValidationError = modelSettingsError(modelSettingsDraft);
  const defaultBaseUrl = selectedDefinition?.baseUrl ?? '';
  const draft =
    drafts[selectedProviderId] ??
    initialDraft(
      selectedStatus,
      models,
      defaultBaseUrl,
      bundledDefaultModelIds,
    );
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
  const providerActive = providerIsActive(selectedStatus);
  const healthy =
    configured && providerActive && selectedStatus.probe_status === 'succeeded';
  const supportsModelSync =
    selectedStatus?.supports_model_sync ??
    selectedDefinition?.supports_model_sync ??
    false;
  const previewEnabledModelIds = models
    .filter((model) => model.available !== false)
    .map((model) => model.id);
  const effectiveEnabledModelIds = configured
    ? (selectedStatus?.enabled_model_ids ?? [])
    : previewEnabledModelIds;
  const apiKey =
    credential.providerId === selectedProviderId ? credential.value : '';
  const credentialLoading =
    configured &&
    (credential.providerId !== selectedProviderId ||
      credential.status === 'loading');
  const apiKeyChanged =
    configured &&
    !credentialLoading &&
    (credential.baseline === null ||
      apiKey.trim() !== credential.baseline.trim());
  const apiKeyDirty =
    !credentialLoading && apiKey.trim() !== (credential.baseline ?? '').trim();
  const defaultModelChanged =
    configured && draft.defaultModelId !== selectedStatus?.default_model_id;
  const connectionDirty = apiKeyDirty || baseUrlChanged || defaultModelChanged;
  const credentialFeedback = credentialLoading
    ? { message: '正在读取已保存的 API Key…', tone: 'loading' as const }
    : pendingAction === 'save'
      ? {
          message: '正在验证并保存模型服务配置…',
          tone: 'loading' as const,
        }
      : pendingAction === 'probe'
        ? {
            message: '正在检测 API Key 与模型连接…',
            tone: 'loading' as const,
          }
        : credential.providerId === selectedProviderId && credential.notice
          ? {
              message: credential.notice,
              tone: credential.noticeTone,
            }
          : null;

  useEffect(() => {
    const providerId = selectedProviderId;
    if (!configured) {
      setCredential((current) =>
        current.providerId === providerId && current.status !== 'loading'
          ? current
          : {
              providerId,
              value: '',
              baseline: null,
              status: 'empty',
              notice: null,
              noticeTone: 'success',
            },
      );
      return;
    }
    if (skipNextSecretLoadRef.current === providerId) {
      skipNextSecretLoadRef.current = null;
      return;
    }

    const controller = new AbortController();
    setCredential({
      providerId,
      value: '',
      baseline: null,
      status: 'loading',
      notice: null,
      noticeTone: 'success',
    });

    void onLoadApiKey(providerId, controller.signal)
      .then((savedApiKey) => {
        if (controller.signal.aborted) return;
        setCredential((current) =>
          current.providerId === providerId
            ? {
                providerId,
                value: savedApiKey,
                baseline: savedApiKey,
                status: 'ready',
                notice: null,
                noticeTone: 'success',
              }
            : current,
        );
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setCredential((current) =>
          current.providerId === providerId
            ? {
                ...current,
                status: 'error',
                notice: providerErrorMessage(error),
                noticeTone: 'error',
              }
            : current,
        );
      });

    return () => controller.abort();
  }, [configured, onLoadApiKey, selectedProviderId]);

  const syncDisabledReason = !supportsModelSync
    ? '该 provider 使用内置模型目录，无需远程同步'
    : baseUrlError !== null
      ? '请先检查 Base URL'
      : credentialLoading
        ? '请等待已保存的 API Key 读取完成'
        : configured && baseUrlChanged
          ? '请先保存新的 Base URL，再同步模型'
          : apiKeyChanged
            ? '请先保存新 API Key，再同步模型'
            : defaultModelChanged
              ? '请先保存新的默认模型，再同步模型'
              : !configured && !apiKey.trim()
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
          initialDraft(
            selectedStatus,
            models,
            defaultBaseUrl,
            bundledDefaultModelIds,
          )),
        ...patch,
      },
    }));
  }

  /** 修改 endpoint 时撤掉旧检测结果，避免把上一地址的成功状态挂在新输入旁。 */
  function updateBaseUrl(baseUrl: string) {
    updateDraft({ baseUrl });
    setCredential((current) =>
      current.providerId === selectedProviderId
        ? { ...current, notice: null }
        : current,
    );
  }

  /** 原子占用页面级异步操作槽，防止双击或不同按钮同时覆盖 pending 状态。 */
  function beginAction(action: Exclude<PendingAction, null>): boolean {
    if (pendingActionRef.current !== null) return false;
    pendingActionRef.current = action;
    setPendingAction(action);
    return true;
  }

  /** 释放页面级异步操作槽。 */
  function endAction() {
    pendingActionRef.current = null;
    setPendingAction(null);
  }

  /** 切换右侧配置上下文，立即从内存清掉上一项密钥并重新隐藏输入。 */
  function selectProvider(providerId: string) {
    if (pendingActionRef.current !== null) return;
    setSelectedProviderId(providerId);
    setCredential({
      providerId,
      value: '',
      baseline: null,
      status: 'empty',
      notice: null,
      noticeTone: 'success',
    });
    setShowSecret(false);
    setConfirmingDelete(false);
    setSettingsModelId(null);
    setModelSettingsDraft(null);
    setSettingsRequestError(null);
    setManualModelDialogOpen(false);
    setManualModelDraft(emptyManualModelDraft);
    setManualModelFormNotice(null);
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
      setNotice('供应商已创建，请输入 API Key；可先检测，再保存配置。');
      setNoticeTone('success');
    } catch (error) {
      setCustomFormNotice(providerErrorMessage(error));
    } finally {
      setCreatingCustom(false);
    }
  }

  /** 登记手动 model，并只在当前配置草稿中勾选，等待统一 probe 后再真正启用。 */
  async function createManualModel() {
    const validationError = manualModelFormError(manualModelDraft, models);
    if (validationError !== null) {
      setManualModelFormNotice(validationError);
      return;
    }
    if (!beginAction('add_model')) return;

    const providerId = selectedProviderId;
    const modelId = manualModelDraft.modelId.trim();
    const displayName = manualModelDraft.displayName.trim();
    setManualModelFormNotice(null);
    try {
      const next = await onAddModel(providerId, {
        model_id: modelId,
        ...(displayName ? { display_name: displayName } : {}),
      });
      setDiscoveredModels((current) => {
        const visibleModels = current[providerId] ?? models;
        const nextModels = visibleModels.some((model) => model.id === next.id)
          ? visibleModels.map((model) => (model.id === next.id ? next : model))
          : [...visibleModels, next];
        return { ...current, [providerId]: nextModels };
      });
      setDrafts((current) => {
        const currentDraft =
          current[providerId] ??
          initialDraft(
            selectedStatus,
            models,
            defaultBaseUrl,
            bundledDefaultModelIds,
          );
        return {
          ...current,
          [providerId]: {
            ...currentDraft,
            enabledModelIds: configured
              ? currentDraft.enabledModelIds
              : [...new Set([...currentDraft.enabledModelIds, next.id])],
            defaultModelId: currentDraft.defaultModelId ?? next.id,
          },
        };
      });
      setManualModelDialogOpen(false);
      setManualModelDraft(emptyManualModelDraft);
      setNotice(
        configured
          ? `${next.display_name ?? next.id} 已添加；开启后将验证并启用。`
          : `${next.display_name ?? next.id} 已添加；验证后默认启用。`,
      );
      setNoticeTone('success');
    } catch (error) {
      setManualModelFormNotice(manualModelRequestError(error));
    } finally {
      endAction();
    }
  }

  /** 用 mutation 返回的权威状态收敛已存在的本地草稿，并移除 discovery overlay。 */
  function convergeProviderSnapshot(next: ProviderStatus) {
    setDrafts((current) => {
      const currentDraft = current[next.provider_id];
      if (currentDraft === undefined) return current;
      return {
        ...current,
        [next.provider_id]: {
          ...currentDraft,
          enabledModelIds: next.enabled_model_ids,
          defaultModelId: next.default_model_id,
        },
      };
    });
    setDiscoveredModels((current) => {
      if (!(next.provider_id in current)) return current;
      const nextModels = { ...current };
      delete nextModels[next.provider_id];
      return nextModels;
    });
  }

  /** 已配置 provider 的 active 开关立即持久化，失败时保持原状态。 */
  async function setProviderActive(active: boolean) {
    if (!configured || !beginAction('provider_active')) return;
    setNotice(null);
    try {
      const next = await onProviderActiveChange(selectedProviderId, active);
      convergeProviderSnapshot(next);
      setNotice(
        `${selectedDefinition?.display_name ?? selectedProviderId} 已${active ? '启用' : '停用'}。`,
      );
      setNoticeTone('success');
    } catch (error) {
      setNotice(providerErrorMessage(error));
      setNoticeTone('error');
    } finally {
      endAction();
    }
  }

  /** 已配置 model 的 enabled 开关立即持久化；default 由 backend transaction 收敛。 */
  async function setModelEnabled(modelId: string, enabled: boolean) {
    const model = models.find((candidate) => candidate.id === modelId);
    if (
      !configured ||
      pendingActionRef.current !== null ||
      (enabled && model?.available === false)
    ) {
      return;
    }
    if (!beginAction('model_enabled')) return;
    setPendingModelId(modelId);
    setNotice(null);
    try {
      const next = await onModelEnabledChange(
        selectedProviderId,
        modelId,
        enabled,
      );
      convergeProviderSnapshot(next);
      setNotice(
        `${model?.display_name ?? modelId} 已${enabled ? '启用' : '停用'}。`,
      );
      setNoticeTone('success');
    } catch (error) {
      setNotice(providerErrorMessage(error));
      setNoticeTone('error');
    } finally {
      setPendingModelId(null);
      endAction();
    }
  }

  /** 只为已经持久化启用的模型打开 runtime settings，避免保存到幽灵模型。 */
  function openModelSettings(model: ProviderModelStatus) {
    if (
      pendingActionRef.current !== null ||
      !configured ||
      model.enabled !== true
    ) {
      return;
    }
    setSettingsModelId(model.id);
    setModelSettingsDraft(initialModelSettingsDraft(model));
    setSettingsRequestError(null);
  }

  /** 合并模型设置草稿，不把输入中的暂态字符串写进 provider 主表单。 */
  function updateModelSettingsDraft(patch: Partial<ModelSettingsDraft>) {
    setModelSettingsDraft((current) =>
      current === null ? current : { ...current, ...patch },
    );
  }

  /** 使用尚未保存的 Key 或 Keychain 中已有密钥执行 model discovery。 */
  async function discoverModels() {
    if (!beginAction(configured ? 'sync' : 'discover')) return;
    setNotice(null);
    try {
      if (configured) {
        const result = await onSyncModels(selectedProviderId);
        setNotice(
          `模型同步完成：新增 ${result.added} 个，恢复 ${result.restored} 个，标记不可用 ${result.unavailable} 个，未变化 ${result.unchanged} 个。`,
        );
      } else {
        const result = await discoverProviderModels(selectedProviderId, {
          ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
          ...(!selectedDefinition?.isCustom
            ? {
                base_url: baseUrlUsesDefault ? null : normalizedDraftBaseUrl,
              }
            : {}),
        });
        const mergedModels = mergeDiscoveredModels(
          result.models,
          models,
          draft,
        );
        setDiscoveredModels((current) => ({
          ...current,
          [selectedProviderId]: mergedModels,
        }));
        if (draft.defaultModelId === null) {
          const firstAvailableModelId = mergedModels.find(
            (model) => model.available !== false,
          )?.id;
          if (firstAvailableModelId) {
            setDrafts((current) => ({
              ...current,
              [selectedProviderId]: {
                ...(current[selectedProviderId] ?? draft),
                defaultModelId: firstAvailableModelId,
              },
            }));
          }
        }
        setNotice(
          `已发现 ${result.models.length} 个模型；保存配置后才会写入模型目录。`,
        );
      }
      setNoticeTone('success');
    } catch (error) {
      setNotice(providerErrorMessage(error));
      setNoticeTone('error');
    } finally {
      endAction();
    }
  }

  /** 使用当前输入执行一次临时 probe，不修改 Keychain、配置 baseline 或模型选择。 */
  async function probeProvider() {
    const providerId = selectedProviderId;
    const submittedApiKey = apiKey.trim();
    if (!submittedApiKey || !beginAction('probe')) return;
    setNotice(null);
    setCredential((current) =>
      current.providerId === providerId
        ? { ...current, notice: null, noticeTone: 'success' }
        : current,
    );
    try {
      const result = await onProbe(providerId, {
        api_key: submittedApiKey,
        ...(!selectedDefinition?.isCustom
          ? {
              base_url: baseUrlUsesDefault ? null : normalizedDraftBaseUrl,
            }
          : {}),
        ...(draft.defaultModelId ? { model_id: draft.defaultModelId } : {}),
      });
      const modelName = result.display_name.trim() || result.model_id;
      const unsavedSuffix = connectionDirty ? '，更改尚未保存' : '';
      setCredential((current) =>
        current.providerId === providerId
          ? {
              ...current,
              notice: `检测通过 · ${modelName} · ${result.latency_ms} ms${unsavedSuffix}`,
              noticeTone: 'success',
            }
          : current,
      );
    } catch (error) {
      setCredential((current) =>
        current.providerId === providerId
          ? {
              ...current,
              notice: providerErrorMessage(error),
              noticeTone: 'error',
            }
          : current,
      );
    } finally {
      endAction();
    }
  }

  /** 验证当前启用模型后保存 Key、endpoint 与模型选择。 */
  async function saveProviderConfiguration() {
    const providerId = selectedProviderId;
    const submittedApiKey = apiKey.trim();
    if (!submittedApiKey || !beginAction('save')) return;
    if (!configured) skipNextSecretLoadRef.current = providerId;
    setNotice(null);
    setCredential((current) =>
      current.providerId === providerId
        ? { ...current, notice: null, noticeTone: 'success' }
        : current,
    );
    try {
      const next = await onSave(providerId, {
        default_model_id: draft.defaultModelId,
        ...(configured
          ? { enabled_model_ids: effectiveEnabledModelIds }
          : { enable_all_models: true }),
        ...(!configured || apiKeyDirty ? { api_key: submittedApiKey } : {}),
        ...(baseUrlChanged
          ? {
              base_url: baseUrlUsesDefault ? null : normalizedDraftBaseUrl,
            }
          : {}),
      });
      setDrafts((current) => ({
        ...current,
        [providerId]: {
          baseUrl: next.base_url,
          enabledModelIds: next.enabled_model_ids,
          defaultModelId: next.default_model_id,
        },
      }));
      setDiscoveredModels((current) => {
        const nextModels = { ...current };
        delete nextModels[providerId];
        return nextModels;
      });
      setCredential((current) =>
        current.providerId === providerId
          ? {
              providerId,
              value: submittedApiKey,
              baseline: submittedApiKey,
              status: 'ready',
              notice:
                next.latency_ms === null
                  ? '配置已保存。'
                  : `配置已保存 · 连接延迟 ${next.latency_ms} ms`,
              noticeTone: 'success',
            }
          : current,
      );
    } catch (error) {
      if (skipNextSecretLoadRef.current === providerId) {
        skipNextSecretLoadRef.current = null;
      }
      setCredential((current) =>
        current.providerId === providerId
          ? {
              ...current,
              status: 'ready',
              notice: providerErrorMessage(error),
              noticeTone: 'error',
            }
          : current,
      );
    } finally {
      endAction();
    }
  }

  /** 持久化模型级 override，成功后使用后端返回的有效值收敛表单。 */
  async function persistModelSettings(
    input: UpdateProviderModelSettingsInput,
    successMessage: string,
  ) {
    if (settingsModel === undefined || !beginAction('settings')) return;
    setSettingsRequestError(null);
    try {
      const next = await onSaveModelSettings(
        selectedProviderId,
        settingsModel.id,
        input,
      );
      setModelSettingsDraft(initialModelSettingsDraft(next));
      setSettingsModelId(null);
      setModelSettingsDraft(null);
      setNotice(successMessage);
      setNoticeTone('success');
    } catch (error) {
      setSettingsRequestError(providerErrorMessage(error));
    } finally {
      endAction();
    }
  }

  /** 保存当前表单；未修改的继承值继续提交 null，避免制造无意义 override。 */
  async function saveModelSettings() {
    if (
      settingsModel === undefined ||
      modelSettingsDraft === null ||
      settingsValidationError !== null
    ) {
      return;
    }
    const perJobConcurrency = Number(modelSettingsDraft.perJobConcurrency);
    const globalConcurrency = Number(modelSettingsDraft.globalConcurrency);
    await persistModelSettings(
      {
        reasoning_policy_override:
          modelSettingsDraft.reasoningPolicy === null ||
          (settingsModel.reasoning_policy_override === null &&
            modelSettingsDraft.reasoningPolicy ===
              settingsModel.reasoning_policy)
            ? null
            : modelSettingsDraft.reasoningPolicy,
        per_job_concurrency_override:
          settingsModel.per_job_concurrency_override === null &&
          perJobConcurrency === settingsModel.per_job_concurrency
            ? null
            : perJobConcurrency,
        global_concurrency_override:
          settingsModel.global_concurrency_override === null &&
          globalConcurrency === settingsModel.global_concurrency
            ? null
            : globalConcurrency,
      },
      `${settingsModel.display_name ?? settingsModel.id} 的运行设置已保存。`,
    );
  }

  /** 一次移除三个 override，让应用默认值重新成为唯一来源。 */
  async function restoreModelSettingsDefaults() {
    if (settingsModel === undefined) return;
    await persistModelSettings(
      {
        reasoning_policy_override: null,
        per_job_concurrency_override: null,
        global_concurrency_override: null,
      },
      `${settingsModel.display_name ?? settingsModel.id} 已恢复应用默认设置。`,
    );
  }

  /** 二次确认后移除 provider 配置，不与轻量 active 开关混用语义。 */
  async function removeProvider() {
    if (!confirmingDelete) {
      setConfirmingDelete(true);
      return;
    }
    if (!beginAction('delete')) return;
    try {
      await onDelete(selectedProviderId);
      setDrafts((current) => ({
        ...current,
        [selectedProviderId]: initialDraft(
          undefined,
          models,
          defaultBaseUrl,
          bundledDefaultModelIds,
        ),
      }));
      const nextProviderId = selectedDefinition?.isCustom
        ? 'deepseek'
        : selectedProviderId;
      skipNextSecretLoadRef.current = null;
      setCredential({
        providerId: nextProviderId,
        value: '',
        baseline: null,
        status: 'empty',
        notice: null,
        noticeTone: 'success',
      });
      setShowSecret(false);
      setConfirmingDelete(false);
      if (selectedDefinition?.isCustom) {
        setSelectedProviderId(nextProviderId);
      }
      setNotice(
        `${selectedDefinition?.display_name ?? selectedProviderId} 配置已移除。`,
      );
      setNoticeTone('success');
    } catch (error) {
      setNotice(providerErrorMessage(error));
      setNoticeTone('error');
    } finally {
      endAction();
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
        disabled={pendingAction !== null}
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
            stateLabel === '已启用'
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
              {
                providers.filter((provider) => providerIsActive(provider))
                  .length
              }{' '}
              个已启用
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
            aria-busy={pendingAction === 'provider_active'}
          >
            {pendingAction === 'provider_active' ? (
              <LoaderCircle className="spin" aria-hidden="true" size={14} />
            ) : healthy ? (
              <CheckCircle2 aria-hidden="true" size={14} />
            ) : null}
            <span>{providerStateLabel(selectedStatus)}</span>
            <Switch.Root
              className="model-switch"
              checked={providerActive}
              disabled={!configured || pendingAction !== null}
              aria-label={`启用 ${selectedDefinition?.display_name ?? selectedProviderId} 供应商`}
              onCheckedChange={(checked) => void setProviderActive(checked)}
            >
              <Switch.Thumb />
            </Switch.Root>
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
                    disabled={pendingAction !== null}
                    onClick={() => updateBaseUrl(defaultBaseUrl)}
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
                    disabled={pendingAction !== null}
                    value={draft.baseUrl}
                    placeholder={defaultBaseUrl}
                    onChange={(event) => updateBaseUrl(event.target.value)}
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
                  {apiKeyDirty
                    ? '更改尚未保存'
                    : configured
                      ? '已安全存入系统 Keychain'
                      : '保存后存入系统 Keychain'}
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
              <div className="provider-secret-input">
                <KeyRound aria-hidden="true" size={16} />
                <input
                  type={showSecret ? 'text' : 'password'}
                  autoComplete="off"
                  spellCheck="false"
                  aria-label="API Key"
                  aria-describedby={
                    credentialFeedback ? 'credential-feedback' : undefined
                  }
                  disabled={pendingAction !== null || credentialLoading}
                  value={apiKey}
                  placeholder="输入 API Key"
                  onChange={(event) => {
                    const value = event.target.value;
                    setCredential((current) =>
                      current.providerId === selectedProviderId
                        ? {
                            ...current,
                            value,
                            status: 'ready',
                            notice: null,
                          }
                        : current,
                    );
                  }}
                />
                <button
                  className="provider-secret-visibility"
                  type="button"
                  aria-label={showSecret ? '隐藏 API Key' : '显示 API Key'}
                  disabled={
                    pendingAction !== null || credentialLoading || !apiKey
                  }
                  onClick={() => setShowSecret((current) => !current)}
                >
                  {showSecret ? (
                    <EyeOff aria-hidden="true" size={16} />
                  ) : (
                    <Eye aria-hidden="true" size={16} />
                  )}
                </button>
                <button
                  className="provider-secret-probe"
                  type="button"
                  aria-label={
                    pendingAction === 'probe'
                      ? '正在检测 API Key'
                      : '检测 API Key'
                  }
                  disabled={
                    pendingAction !== null ||
                    credentialLoading ||
                    baseUrlError !== null ||
                    !apiKey.trim()
                  }
                  onClick={() => void probeProvider()}
                >
                  {pendingAction === 'probe' ? (
                    <LoaderCircle
                      className="spin"
                      aria-hidden="true"
                      size={14}
                    />
                  ) : (
                    <Activity aria-hidden="true" size={14} />
                  )}
                  {pendingAction === 'probe' ? '检测中' : '检测'}
                </button>
              </div>
            </div>
            {credentialFeedback ? (
              <p
                id="credential-feedback"
                className={`provider-credential-feedback provider-credential-feedback--${credentialFeedback.tone}`}
                role={credentialFeedback.tone === 'error' ? 'alert' : 'status'}
                aria-live="polite"
              >
                {credentialFeedback.tone === 'loading' ? (
                  <LoaderCircle className="spin" aria-hidden="true" size={13} />
                ) : credentialFeedback.tone === 'success' ? (
                  <CheckCircle2 aria-hidden="true" size={13} />
                ) : (
                  <AlertCircle aria-hidden="true" size={13} />
                )}
                {credentialFeedback.message}
              </p>
            ) : null}
          </section>

          <section
            className="provider-model-section"
            aria-labelledby="provider-models-title"
          >
            <div className="provider-section-heading provider-model-heading">
              <div>
                <h3 id="provider-models-title">模型</h3>
                <span>
                  {models.length} 个可见，{effectiveEnabledModelIds.length}{' '}
                  个已启用
                  {!configured && models.length > 0
                    ? ' · 验证后默认全部启用'
                    : ''}
                  {!supportsModelSync ? ' · 内置模型目录，无需同步' : ''}
                </span>
              </div>
              <div className="provider-model-actions">
                <Dialog.Root
                  open={manualModelDialogOpen}
                  onOpenChange={(open) => {
                    if (pendingActionRef.current !== null) return;
                    setManualModelDialogOpen(open);
                    if (!open) {
                      setManualModelDraft(emptyManualModelDraft);
                      setManualModelFormNotice(null);
                    }
                  }}
                >
                  <Dialog.Trigger asChild>
                    <button
                      className="sync-model-button add-model-button"
                      type="button"
                      disabled={pendingAction !== null}
                    >
                      <Plus aria-hidden="true" size={15} />
                      添加模型
                    </button>
                  </Dialog.Trigger>
                  <Dialog.Portal>
                    <Dialog.Overlay className="dialog-overlay" />
                    <Dialog.Content
                      className="provider-custom-dialog model-add-dialog"
                      aria-describedby="manual-model-description"
                    >
                      <header>
                        <div>
                          <Dialog.Title>手动添加模型</Dialog.Title>
                          <Dialog.Description id="manual-model-description">
                            填写上游 API 请求使用的准确 model ID
                          </Dialog.Description>
                        </div>
                        <Dialog.Close asChild>
                          <button
                            className="icon-button"
                            type="button"
                            aria-label="关闭添加模型"
                            disabled={pendingAction !== null}
                          >
                            <X aria-hidden="true" size={17} />
                          </button>
                        </Dialog.Close>
                      </header>

                      <form
                        className="provider-custom-form"
                        onSubmit={(event) => {
                          event.preventDefault();
                          void createManualModel();
                        }}
                      >
                        <label>
                          <span>模型 ID</span>
                          <input
                            autoFocus
                            type="text"
                            maxLength={256}
                            autoComplete="off"
                            spellCheck="false"
                            disabled={pendingAction !== null}
                            value={manualModelDraft.modelId}
                            placeholder="例如：org/model-v2"
                            onChange={(event) =>
                              setManualModelDraft((current) => ({
                                ...current,
                                modelId: event.target.value,
                              }))
                            }
                          />
                        </label>
                        <label>
                          <span>显示名称（可选）</span>
                          <input
                            type="text"
                            maxLength={120}
                            autoComplete="off"
                            disabled={pendingAction !== null}
                            value={manualModelDraft.displayName}
                            placeholder="留空时使用模型 ID"
                            onChange={(event) =>
                              setManualModelDraft((current) => ({
                                ...current,
                                displayName: event.target.value,
                              }))
                            }
                          />
                        </label>
                        {manualModelFormNotice ? (
                          <p className="provider-custom-error" role="alert">
                            <AlertCircle aria-hidden="true" size={14} />
                            {manualModelFormNotice}
                          </p>
                        ) : null}
                        <footer>
                          <Dialog.Close asChild>
                            <button
                              className="provider-custom-cancel"
                              type="button"
                              disabled={pendingAction !== null}
                            >
                              取消
                            </button>
                          </Dialog.Close>
                          <button
                            className="provider-custom-submit"
                            type="submit"
                            disabled={pendingAction !== null}
                          >
                            {pendingAction === 'add_model' ? (
                              <LoaderCircle
                                className="spin"
                                aria-hidden="true"
                                size={15}
                              />
                            ) : (
                              <Plus aria-hidden="true" size={15} />
                            )}
                            {pendingAction === 'add_model'
                              ? '正在添加'
                              : '添加模型'}
                          </button>
                        </footer>
                      </form>
                    </Dialog.Content>
                  </Dialog.Portal>
                </Dialog.Root>

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
                  {pendingAction === 'discover' || pendingAction === 'sync' ? (
                    <LoaderCircle
                      className="spin"
                      aria-hidden="true"
                      size={15}
                    />
                  ) : (
                    <RefreshCw aria-hidden="true" size={15} />
                  )}
                  {pendingAction === 'discover' || pendingAction === 'sync'
                    ? '同步中'
                    : '同步模型'}
                </button>
              </div>
            </div>

            <div className="provider-model-list">
              {models.length === 0 ? (
                <div className="provider-model-empty">
                  <AlertCircle aria-hidden="true" size={17} />
                  <span>
                    {supportsModelSync
                      ? '同步模型，或手动添加模型'
                      : '手动添加模型'}
                  </span>
                </div>
              ) : null}
              {models.map((model) => {
                const enabled = configured
                  ? model.enabled === true
                  : model.available !== false;
                const isDefault = draft.defaultModelId === model.id;
                const available = model.available !== false;
                return (
                  <div
                    className={`provider-model-row ${available ? '' : 'provider-model-row--unavailable'}`}
                    key={model.id}
                    aria-busy={
                      pendingAction === 'model_enabled' &&
                      pendingModelId === model.id
                    }
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
                        {model.source === 'manual' ? <em>手动</em> : null}
                        {!available ? <em>当前未返回</em> : null}
                        {model.probe_status === 'failed' ? (
                          <em className="provider-model-state--error">
                            验证失败
                          </em>
                        ) : null}
                      </span>
                    </div>
                    <button
                      className={`model-default-button ${isDefault ? 'model-default-button--active' : ''}`}
                      type="button"
                      disabled={
                        pendingAction !== null || !enabled || !available
                      }
                      aria-label={`将 ${model.display_name ?? model.id} 设为默认模型`}
                      aria-pressed={isDefault}
                      onClick={() => {
                        setCredential((current) =>
                          current.providerId === selectedProviderId
                            ? { ...current, notice: null }
                            : current,
                        );
                        updateDraft({ defaultModelId: model.id });
                      }}
                    >
                      <Check aria-hidden="true" size={14} />
                      {isDefault ? '默认' : '设为默认'}
                    </button>
                    <span className="model-settings-slot">
                      {configured && enabled && model.enabled === true ? (
                        <button
                          className="model-settings-button"
                          type="button"
                          aria-label={`设置 ${model.display_name ?? model.id}`}
                          disabled={pendingAction !== null}
                          onClick={() => openModelSettings(model)}
                        >
                          <Settings2 aria-hidden="true" size={14} />
                          设置
                        </button>
                      ) : null}
                    </span>
                    <Switch.Root
                      className="model-switch"
                      checked={enabled}
                      disabled={
                        !configured ||
                        pendingAction !== null ||
                        (!available && !enabled)
                      }
                      aria-label={`启用 ${model.display_name ?? model.id}`}
                      onCheckedChange={(checked) =>
                        void setModelEnabled(model.id, checked)
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
              role={noticeTone === 'error' ? 'alert' : 'status'}
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
                {confirmingDelete ? '确认移除' : '移除配置'}
              </button>
            ) : null}
          </span>
          <button
            className="provider-save-button"
            type="button"
            disabled={
              pendingAction !== null ||
              credentialLoading ||
              baseUrlError !== null ||
              !apiKey.trim() ||
              (configured &&
                (effectiveEnabledModelIds.length === 0 ||
                  draft.defaultModelId === null))
            }
            onClick={() => void saveProviderConfiguration()}
          >
            {pendingAction === 'save' ? (
              <LoaderCircle className="spin" aria-hidden="true" size={15} />
            ) : (
              <Save aria-hidden="true" size={15} />
            )}
            {pendingAction === 'save' ? '正在保存' : '保存配置'}
          </button>
        </footer>
      </div>

      <Dialog.Root
        open={settingsModel !== undefined && modelSettingsDraft !== null}
        onOpenChange={(open) => {
          if (open || pendingAction !== null) return;
          setSettingsModelId(null);
          setModelSettingsDraft(null);
          setSettingsRequestError(null);
        }}
      >
        <Dialog.Portal>
          <Dialog.Overlay className="dialog-overlay" />
          {settingsModel && modelSettingsDraft ? (
            <Dialog.Content
              className="model-settings-dialog"
              aria-describedby="model-settings-description"
            >
              <header>
                <div>
                  <Dialog.Title>
                    {settingsModel.display_name ?? settingsModel.id}
                  </Dialog.Title>
                  <Dialog.Description id="model-settings-description">
                    模型运行设置 · {settingsModel.id}
                  </Dialog.Description>
                </div>
                <Dialog.Close asChild>
                  <button
                    className="icon-button"
                    type="button"
                    aria-label="关闭模型设置"
                    disabled={pendingAction !== null}
                  >
                    <X aria-hidden="true" size={17} />
                  </button>
                </Dialog.Close>
              </header>

              <form
                onSubmit={(event) => {
                  event.preventDefault();
                  void saveModelSettings();
                }}
              >
                <div className="model-settings-fields">
                  {settingsModel.supported_reasoning_policies.length > 1 &&
                  modelSettingsDraft.reasoningPolicy !== null ? (
                    <label className="model-settings-field">
                      <span>
                        <strong>思考模式</strong>
                        <small>
                          {settingsModel.reasoning_policy_override === null
                            ? '继承模型目录默认值'
                            : '使用当前模型 override'}
                        </small>
                      </span>
                      <select
                        aria-label="思考模式"
                        disabled={pendingAction !== null}
                        value={modelSettingsDraft.reasoningPolicy}
                        onChange={(event) =>
                          updateModelSettingsDraft({
                            reasoningPolicy: event.target
                              .value as ReasoningPolicy,
                          })
                        }
                      >
                        {settingsModel.supported_reasoning_policies.map(
                          (policy) => (
                            <option key={policy} value={policy}>
                              {reasoningPolicyLabels[policy] ?? policy}
                            </option>
                          ),
                        )}
                      </select>
                    </label>
                  ) : null}

                  <div className="model-concurrency-grid">
                    <label className="model-settings-field">
                      <span>
                        <strong>单任务并发</strong>
                        <small>
                          {settingsModel.per_job_concurrency_override === null
                            ? '继承应用默认值'
                            : '使用当前模型 override'}
                        </small>
                      </span>
                      <input
                        type="number"
                        min="1"
                        max="32"
                        step="1"
                        inputMode="numeric"
                        aria-label="单任务并发"
                        aria-invalid={settingsValidationError !== null}
                        disabled={pendingAction !== null}
                        value={modelSettingsDraft.perJobConcurrency}
                        onChange={(event) =>
                          updateModelSettingsDraft({
                            perJobConcurrency: event.target.value,
                          })
                        }
                      />
                    </label>
                    <label className="model-settings-field">
                      <span>
                        <strong>跨任务总并发</strong>
                        <small>
                          {settingsModel.global_concurrency_override === null
                            ? '继承应用默认值'
                            : '使用当前模型 override'}
                        </small>
                      </span>
                      <input
                        type="number"
                        min="1"
                        max="32"
                        step="1"
                        inputMode="numeric"
                        aria-label="跨任务总并发"
                        aria-invalid={settingsValidationError !== null}
                        disabled={pendingAction !== null}
                        value={modelSettingsDraft.globalConcurrency}
                        onChange={(event) =>
                          updateModelSettingsDraft({
                            globalConcurrency: event.target.value,
                          })
                        }
                      />
                    </label>
                  </div>

                  {settingsValidationError ? (
                    <p className="model-settings-error" role="alert">
                      <AlertCircle aria-hidden="true" size={14} />
                      {settingsValidationError}
                    </p>
                  ) : null}
                  {settingsRequestError ? (
                    <p className="model-settings-error" role="alert">
                      <AlertCircle aria-hidden="true" size={14} />
                      {settingsRequestError}
                    </p>
                  ) : null}
                </div>

                <footer>
                  <button
                    className="model-settings-reset"
                    type="button"
                    disabled={pendingAction !== null}
                    onClick={() => void restoreModelSettingsDefaults()}
                  >
                    <RotateCcw aria-hidden="true" size={13} />
                    恢复应用默认
                  </button>
                  <span>
                    <Dialog.Close asChild>
                      <button
                        className="model-settings-cancel"
                        type="button"
                        disabled={pendingAction !== null}
                      >
                        取消
                      </button>
                    </Dialog.Close>
                    <button
                      className="model-settings-save"
                      type="submit"
                      disabled={
                        pendingAction !== null ||
                        settingsValidationError !== null
                      }
                    >
                      {pendingAction === 'settings' ? (
                        <LoaderCircle
                          className="spin"
                          aria-hidden="true"
                          size={14}
                        />
                      ) : (
                        <Check aria-hidden="true" size={14} />
                      )}
                      {pendingAction === 'settings' ? '保存中' : '保存设置'}
                    </button>
                  </span>
                </footer>
              </form>
            </Dialog.Content>
          ) : null}
        </Dialog.Portal>
      </Dialog.Root>
    </section>
  );
}
