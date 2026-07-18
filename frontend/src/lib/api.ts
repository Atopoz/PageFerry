/** 封装 React renderer 与本地 FastAPI sidecar 之间的窄 HTTP contract。 */

import { invoke, isTauri } from '@tauri-apps/api/core';

let apiBaseUrl = import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8765';
let apiBootToken: string | null = null;

interface SidecarConnection {
  baseUrl: string;
  bootToken: string | null;
}

/** 在 renderer 发出首个 HTTP 请求前读取 Tauri 托管的实际 sidecar 连接。 */
export async function initializeApiRuntime(): Promise<void> {
  if (!isTauri()) return;
  try {
    const connection = await invoke<SidecarConnection>('sidecar_connection');
    const endpoint = new URL(connection.baseUrl);
    const port = Number(endpoint.port);
    if (
      endpoint.protocol !== 'http:' ||
      endpoint.hostname !== '127.0.0.1' ||
      endpoint.pathname !== '/' ||
      endpoint.search !== '' ||
      endpoint.hash !== '' ||
      !Number.isInteger(port) ||
      port <= 0 ||
      port > 65535
    ) {
      throw new Error('Invalid sidecar endpoint');
    }
    if (connection.bootToken !== null && connection.bootToken.length < 32) {
      throw new Error('Invalid sidecar token');
    }
    apiBaseUrl = connection.baseUrl.replace(/\/$/, '');
    apiBootToken = connection.bootToken;
  } catch (error) {
    // release runtime 不能静默退回固定端口，否则可能连接到另一个本机进程。
    apiBaseUrl = 'http://127.0.0.1:0';
    apiBootToken = null;
    throw error;
  }
}

interface HealthResponse {
  code: 'success';
  data: {
    service: string;
    version: string;
  };
}

export interface ProviderDefinition {
  id: string;
  display_name: string;
  protocol: 'openai' | 'anthropic' | 'gemini' | 'custom';
  available: boolean;
  base_url_editable: boolean;
  supports_model_sync?: boolean;
  default_base_url?: string | null;
  docs_url?: string | null;
  api_key_url?: string | null;
}

export interface ModelDefinition {
  id: string;
  display_name: string;
  capabilities: string[];
}

export interface ProviderModelDefinition {
  provider_id: string;
  model_id: string;
  upstream_model_id: string;
  enabled_by_default: boolean;
}

export interface ModelCatalog {
  schema_version: number;
  catalog_version: string;
  providers: ProviderDefinition[];
  models: ModelDefinition[];
  provider_models: ProviderModelDefinition[];
}

export type ProbeStatus =
  'not_configured' | 'not_tested' | 'succeeded' | 'failed';

export interface ProviderStatus {
  provider_id: string;
  display_name: string;
  protocol: 'openai' | 'anthropic' | 'gemini' | 'custom';
  is_custom: boolean;
  base_url: string;
  base_url_overridden: boolean;
  base_url_editable: boolean;
  deletable: boolean;
  available: boolean;
  configured: boolean;
  active: boolean;
  supports_model_sync: boolean;
  enabled_model_ids: string[];
  default_model_id: string | null;
  model_count: number;
  models: ProviderModelStatus[];
  probe_status: ProbeStatus;
  probe_error_code: string | null;
  latency_ms: number | null;
  last_probed_at: string | null;
  last_synced_at: string | null;
}

export interface ConfigureProviderInput {
  api_key?: string;
  base_url?: string | null;
  enabled_model_ids?: string[];
  default_model_id: string | null;
  enable_all_models?: boolean;
}

export interface ProbeProviderInput {
  api_key?: string;
  base_url?: string | null;
  model_id?: string;
}

export interface ProviderProbeResult {
  provider_id: string;
  model_id: string;
  display_name: string;
  latency_ms: number;
}

interface ProviderApiKeyResponse {
  api_key: string;
}

export type ReasoningPolicy =
  'provider_default' | 'off' | 'on' | 'low' | 'medium' | 'high' | 'max';

export interface UpdateProviderModelSettingsInput {
  reasoning_policy_override?: ReasoningPolicy | null;
  per_job_concurrency_override?: number | null;
  global_concurrency_override?: number | null;
}

export interface CreateProviderModelInput {
  model_id: string;
  display_name?: string;
}

export interface CreateCustomProviderInput {
  display_name: string;
  base_url: string;
}

export interface ProviderModelStatus {
  id: string;
  display_name?: string;
  source: 'remote' | 'catalog' | 'manual';
  available: boolean;
  enabled?: boolean;
  probe_status?: ProbeStatus;
  probe_error_code?: string | null;
  latency_ms?: number | null;
  last_probed_at?: string | null;
  reasoning_policy: ReasoningPolicy | null;
  reasoning_policy_override: ReasoningPolicy | null;
  supported_reasoning_policies: ReasoningPolicy[];
  per_job_concurrency: number;
  per_job_concurrency_override: number | null;
  global_concurrency: number;
  global_concurrency_override: number | null;
}

export interface ProviderModelDiscovery {
  models: ProviderModelStatus[];
  synced_at?: string | null;
}

export interface ProviderModelSync {
  provider_id: string;
  models: ProviderModelStatus[];
  last_synced_at: string;
  added: number;
  restored: number;
  unavailable: number;
  unchanged: number;
}

export type JobStatus =
  'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled';

export type JobProgressStage = 'extracting' | 'translating' | 'formatting';

export interface TranslationJob {
  id: string;
  source_name: string;
  document_type: 'docx' | 'pptx' | 'xlsx' | 'txt' | 'md' | 'pdf';
  status: JobStatus;
  progress: number;
  progress_stage: JobProgressStage;
  processed_segments: number;
  total_segments: number;
  provider_id: string;
  model_id: string;
  source_language: string | null;
  target_language: string;
  output_path: string | null;
  artifacts: TranslationArtifact[];
  error_code: string | null;
  translated_segments: number;
  fallback_segments: number;
  warning_codes: string[];
  created_at: string;
  updated_at: string;
}

export interface TranslationArtifact {
  kind: 'translated' | 'bilingual';
  path: string;
}

export type PdfResourceState =
  'ready' | 'missing' | 'downloading' | 'cancelling' | 'failed' | 'cancelled';

export interface PdfResourceItem {
  pack: string;
  size_bytes: number;
  completed_bytes: number;
  ready: boolean;
}

export interface PdfResourceStatus {
  pack_revision: string;
  state: PdfResourceState;
  total_bytes: number;
  completed_bytes: number;
  current_asset_id: string | null;
  error_code: string | null;
  resources: PdfResourceItem[];
}

export type DocumentTranslationOptions =
  | {
      kind: 'docx';
      translate_tables: boolean;
      bilingual: boolean;
    }
  | {
      kind: 'pptx';
      translate_tables: boolean;
      translate_notes: boolean;
    }
  | {
      kind: 'pdf';
      bilingual: boolean;
    };

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE';
  body?: BodyInit;
  cache?: RequestCache;
  headers?: HeadersInit;
  signal?: AbortSignal;
}

interface ProviderStatusWire extends Partial<ProviderStatus> {
  provider_id?: string;
  model_id?: string | null;
}

/** 保留稳定错误码，供设置面板把技术错误翻译成可操作的提示。 */
export class ApiError extends Error {
  readonly code: string | null;
  readonly status: number;

  /** 创建一个不包含上游响应正文或密钥信息的 UI 错误。 */
  constructor(message: string, status: number, code: string | null = null) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    this.status = status;
  }
}

/** 判断未知 JSON 是否为可安全索引的对象。 */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** 同时兼容直接 payload 与 `{data: payload}` 两种窄响应。 */
function unwrapData<T>(payload: unknown): T {
  if (isRecord(payload) && 'data' in payload) {
    return payload.data as T;
  }
  return payload as T;
}

/** 从 FastAPI detail 或 provider 顶层错误体中读取稳定、无敏感信息的错误。 */
function readError(payload: unknown): {
  code: string | null;
  message: string | null;
} {
  if (!isRecord(payload)) return { code: null, message: null };
  const detail = payload.detail;
  if (typeof detail === 'string') return { code: null, message: detail };
  const body = isRecord(detail) ? detail : payload;
  return {
    code: typeof body.code === 'string' ? body.code : null,
    message: typeof body.message === 'string' ? body.message : null,
  };
}

/** 请求 JSON，并正确处理 provider 的顶层错误体与 204 空响应。 */
async function requestJson<T>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  let requestOptions = options;
  if (apiBootToken !== null) {
    const headers = new Headers(options.headers);
    headers.set('X-PageFerry-Boot-Token', apiBootToken);
    requestOptions = { ...options, headers };
  }
  const response = await fetch(`${apiBaseUrl}${path}`, requestOptions);
  if (!response.ok) {
    const payload: unknown = await response.json().catch(() => null);
    const error = readError(payload);
    throw new ApiError(
      error.message ?? `本地服务返回 ${response.status}`,
      response.status,
      error.code,
    );
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/** 把 provider 的最小状态补齐成 UI 可直接消费的稳定结构。 */
function normalizeProviderStatus(
  value: ProviderStatusWire,
): ProviderStatus | null {
  if (!value.provider_id) return null;
  const probeStatus = value.probe_status ?? 'not_tested';
  const configured = value.configured ?? probeStatus === 'succeeded';
  const isCustom = value.is_custom ?? value.provider_id.startsWith('custom-');
  const defaultModelId = value.default_model_id ?? value.model_id ?? null;
  const enabledModelIds = Array.isArray(value.enabled_model_ids)
    ? value.enabled_model_ids.filter(
        (modelId): modelId is string => typeof modelId === 'string',
      )
    : defaultModelId
      ? [defaultModelId]
      : [];
  return {
    provider_id: value.provider_id,
    display_name: value.display_name ?? value.provider_id,
    protocol: value.protocol ?? 'openai',
    is_custom: isCustom,
    base_url: value.base_url ?? '',
    base_url_overridden: value.base_url_overridden ?? false,
    base_url_editable: value.base_url_editable ?? false,
    deletable: value.deletable ?? isCustom,
    available: value.available ?? true,
    configured,
    active: value.active ?? (configured && probeStatus === 'succeeded'),
    supports_model_sync: value.supports_model_sync ?? false,
    enabled_model_ids: enabledModelIds,
    default_model_id: defaultModelId,
    model_count: value.model_count ?? enabledModelIds.length,
    models: Array.isArray(value.models) ? value.models : [],
    probe_status: probeStatus,
    probe_error_code: value.probe_error_code ?? null,
    latency_ms: value.latency_ms ?? null,
    last_probed_at: value.last_probed_at ?? null,
    last_synced_at: value.last_synced_at ?? null,
  };
}

/** 创建一个只使用 OpenAI-compatible 协议的自定义 provider 定义。 */
export async function createCustomProvider(
  input: CreateCustomProviderInput,
): Promise<ProviderStatus> {
  const payload = await requestJson<unknown>('/api/v1/providers/custom', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });
  const normalized = normalizeProviderStatus(
    unwrapData<ProviderStatusWire>(payload),
  );
  if (normalized === null) {
    throw new ApiError('自定义供应商返回了无效状态。', 502, 'protocol');
  }
  return normalized;
}

/** 读取 sidecar 健康状态与版本。 */
export function getHealth(signal?: AbortSignal): Promise<HealthResponse> {
  return requestJson<HealthResponse>('/healthz', { signal });
}

/** 读取随当前应用版本发布的 provider/model catalog。 */
export async function getModelCatalog(
  signal?: AbortSignal,
): Promise<ModelCatalog> {
  const payload = await requestJson<unknown>('/api/v1/model-catalog', {
    signal,
  });
  return unwrapData<ModelCatalog>(payload);
}

/** 读取 provider 的 Keychain 配置与最近 probe 状态，不返回密钥。 */
export async function getProviderStatuses(
  signal?: AbortSignal,
): Promise<ProviderStatus[]> {
  const payload = await requestJson<unknown>('/api/v1/providers', { signal });
  const values = unwrapData<ProviderStatusWire[]>(payload);
  if (!Array.isArray(values)) return [];
  return values.flatMap((value) => {
    const normalized = normalizeProviderStatus(value);
    return normalized === null ? [] : [normalized];
  });
}

/** 从系统 Keychain 读取已配置 provider 的原始 API Key，仅在设置页内存中短暂使用。 */
export async function getProviderApiKey(
  providerId: string,
  signal?: AbortSignal,
): Promise<string> {
  const payload = await requestJson<unknown>(
    `/api/v1/providers/${encodeURIComponent(providerId)}/api-key`,
    { cache: 'no-store', signal },
  );
  const response = unwrapData<ProviderApiKeyResponse>(payload);
  if (typeof response?.api_key !== 'string' || response.api_key.length === 0) {
    throw new ApiError('模型服务返回了无效密钥。', 502, 'protocol');
  }
  return response.api_key;
}

/** 使用当前输入执行一次不持久化的最小推理检测。 */
export async function probeProvider(
  providerId: string,
  input: ProbeProviderInput,
): Promise<ProviderProbeResult> {
  const payload = await requestJson<unknown>(
    `/api/v1/providers/${encodeURIComponent(providerId)}/probe`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(input),
    },
  );
  const response = unwrapData<ProviderProbeResult>(payload);
  if (
    typeof response?.provider_id !== 'string' ||
    typeof response.model_id !== 'string' ||
    typeof response.display_name !== 'string' ||
    typeof response.latency_ms !== 'number'
  ) {
    throw new ApiError('模型服务返回了无效检测结果。', 502, 'protocol');
  }
  return response;
}

/** 运行真实最小推理检测，成功后保存 provider 配置。 */
export async function configureProvider(
  providerId: string,
  input: ConfigureProviderInput,
): Promise<ProviderStatus> {
  const payload = await requestJson<unknown>(
    `/api/v1/providers/${providerId}`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(input),
    },
  );
  const normalized = normalizeProviderStatus(
    unwrapData<ProviderStatusWire>(payload),
  );
  if (normalized === null) {
    throw new ApiError('模型服务返回了无效状态。', 502, 'protocol');
  }
  return normalized;
}

/** 非破坏地启用或停用 provider；API Key、模型 inventory 与 runtime settings 均保留。 */
export async function setProviderActive(
  providerId: string,
  active: boolean,
): Promise<ProviderStatus> {
  const payload = await requestJson<unknown>(
    `/api/v1/providers/${encodeURIComponent(providerId)}/active`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ active }),
    },
  );
  const normalized = normalizeProviderStatus(
    unwrapData<ProviderStatusWire>(payload),
  );
  if (normalized === null) {
    throw new ApiError('模型服务返回了无效状态。', 502, 'protocol');
  }
  return normalized;
}

/** 使用新输入的密钥或已有 Keychain 密钥读取 provider 模型列表，不持久化密钥。 */
export async function discoverProviderModels(
  providerId: string,
  input: { api_key?: string; base_url?: string | null } = {},
): Promise<ProviderModelDiscovery> {
  const payload = await requestJson<unknown>(
    `/api/v1/providers/${providerId}/models/discover`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(input),
    },
  );
  return unwrapData<ProviderModelDiscovery>(payload);
}

/** 把远端 inventory 幂等合并进已配置 provider 的持久化模型目录。 */
export async function syncProviderModels(
  providerId: string,
): Promise<ProviderModelSync> {
  const payload = await requestJson<unknown>(
    `/api/v1/providers/${encodeURIComponent(providerId)}/models/sync`,
    { method: 'POST' },
  );
  return unwrapData<ProviderModelSync>(payload);
}

/** 登记一个手动填写的上游 model；启用仍由后续 provider 配置完成。 */
export async function createProviderModel(
  providerId: string,
  input: CreateProviderModelInput,
): Promise<ProviderModelStatus> {
  const payload = await requestJson<unknown>(
    `/api/v1/providers/${encodeURIComponent(providerId)}/models`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(input),
    },
  );
  return unwrapData<ProviderModelStatus>(payload);
}

/** 即时启停一个模型；启用会先做最小推理检测，禁用不会删除模型配置。 */
export async function setProviderModelEnabled(
  providerId: string,
  modelId: string,
  enabled: boolean,
): Promise<ProviderStatus> {
  const payload = await requestJson<unknown>(
    `/api/v1/providers/${encodeURIComponent(providerId)}/models/${encodeURIComponent(modelId)}/enabled`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    },
  );
  const normalized = normalizeProviderStatus(
    unwrapData<ProviderStatusWire>(payload),
  );
  if (normalized === null) {
    throw new ApiError('模型服务返回了无效状态。', 502, 'protocol');
  }
  return normalized;
}

/** 更新一个已启用模型的 runtime settings；null 表示移除对应 override。 */
export async function updateProviderModelSettings(
  providerId: string,
  modelId: string,
  input: UpdateProviderModelSettingsInput,
): Promise<ProviderModelStatus> {
  const payload = await requestJson<unknown>(
    `/api/v1/providers/${encodeURIComponent(providerId)}/models/${encodeURIComponent(modelId)}/settings`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(input),
    },
  );
  return unwrapData<ProviderModelStatus>(payload);
}

/** 删除 provider metadata，并同步删除对应 Keychain secret。 */
export function deleteProvider(providerId: string): Promise<void> {
  return requestJson<void>(`/api/v1/providers/${providerId}`, {
    method: 'DELETE',
  });
}

/** 读取最近任务，供工作台与短轮询刷新使用。 */
export async function getJobs(signal?: AbortSignal): Promise<TranslationJob[]> {
  const payload = await requestJson<unknown>('/api/v1/jobs', { signal });
  const jobs = unwrapData<TranslationJob[]>(payload);
  return Array.isArray(jobs) ? jobs : [];
}

/** 读取 PDF resource pack 的本地安装与下载进度。 */
export async function getPdfResourceStatus(
  signal?: AbortSignal,
): Promise<PdfResourceStatus> {
  const payload = await requestJson<unknown>('/api/v1/pdf-resources', {
    cache: 'no-store',
    signal,
  });
  return unwrapData<PdfResourceStatus>(payload);
}

/** 显式开始或重试 PDF resource pack 安装。 */
export async function installPdfResources(): Promise<PdfResourceStatus> {
  const payload = await requestJson<unknown>('/api/v1/pdf-resources/install', {
    method: 'POST',
  });
  return unwrapData<PdfResourceStatus>(payload);
}

/** 请求取消当前 PDF resource pack 下载，并返回后端收敛后的状态。 */
export async function cancelPdfResourceInstall(): Promise<PdfResourceStatus> {
  const payload = await requestJson<unknown>('/api/v1/pdf-resources/cancel', {
    method: 'POST',
  });
  return unwrapData<PdfResourceStatus>(payload);
}

/** 使用 Tauri 文件对话框返回的本地路径创建任务。 */
export async function createPathJob(input: {
  source_path: string;
  source_language: string | null;
  target_language: string;
  provider_id: string;
  model_id: string;
  options?: DocumentTranslationOptions;
}): Promise<TranslationJob> {
  const payload = await requestJson<unknown>('/api/v1/jobs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });
  return unwrapData<TranslationJob>(payload);
}

/** 在普通浏览器开发模式下，经 localhost 上传文件并创建任务。 */
export async function createUploadJob(input: {
  file: File;
  source_language: string | null;
  target_language: string;
  provider_id: string;
  model_id: string;
  options?: DocumentTranslationOptions;
}): Promise<TranslationJob> {
  const body = new FormData();
  body.append('file', input.file);
  body.append('source_language', input.source_language ?? 'auto');
  body.append('target_language', input.target_language);
  body.append('provider_id', input.provider_id);
  body.append('model_id', input.model_id);
  if (input.options) body.append('options', JSON.stringify(input.options));
  const payload = await requestJson<unknown>('/api/v1/jobs/upload', {
    method: 'POST',
    body,
  });
  return unwrapData<TranslationJob>(payload);
}
