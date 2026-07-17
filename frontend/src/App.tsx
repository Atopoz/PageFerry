/** 组装 PageFerry 桌面壳、三个一级页面与本地任务轮询。 */

import { AlertTriangle } from 'lucide-react';
import { useEffect, useState } from 'react';

import { HistoryPage } from './features/history/HistoryPage';
import {
  AppSidebar,
  type AppRoute,
  type ServiceState,
} from './features/navigation/AppSidebar';
import { AppTitlebar } from './features/navigation/AppTitlebar';
import { detectDesktopPlatform } from './features/navigation/desktop-platform';
import { ProviderPage } from './features/providers/ProviderPage';
import {
  type StartTranslationInput,
  TranslationWorkspace,
} from './features/translation/TranslationWorkspace';
import {
  configureProvider,
  createProviderModel,
  createCustomProvider,
  createPathJob,
  createUploadJob,
  deleteProvider,
  getHealth,
  getJobs,
  getModelCatalog,
  getProviderApiKey,
  getProviderStatuses,
  probeProvider,
  setProviderActive,
  setProviderModelEnabled,
  syncProviderModels,
  updateProviderModelSettings,
  type ConfigureProviderInput,
  type CreateProviderModelInput,
  type CreateCustomProviderInput,
  type ModelCatalog,
  type ProviderModelStatus,
  type ProviderModelSync,
  type ProviderProbeResult,
  type ProbeProviderInput,
  type ProviderStatus,
  type TranslationJob,
  type UpdateProviderModelSettingsInput,
} from './lib/api';
import './App.css';

/** 把新任务插到列表顶部，并替换后端刷新返回的同 id 快照。 */
function upsertJob(
  jobs: TranslationJob[],
  next: TranslationJob,
): TranslationJob[] {
  return [next, ...jobs.filter((job) => job.id !== next.id)];
}

/** 把 provider 状态按 id 合并，保留 catalog 顺序和未来扩展空间。 */
function upsertProvider(
  providers: ProviderStatus[],
  next: ProviderStatus,
): ProviderStatus[] {
  const exists = providers.some(
    (provider) => provider.provider_id === next.provider_id,
  );
  if (!exists) return [...providers, next];
  return providers.map((provider) =>
    provider.provider_id === next.provider_id ? next : provider,
  );
}

/**
 * 在 DELETE 已成功而列表刷新失败时，本地完成同一状态转换。
 *
 * custom provider 从列表移除；preset 则保留入口但清空凭据相关状态，确保翻译页不会
 * 继续提供已经移除配置的模型。
 */
function applyDeletedProvider(
  providers: ProviderStatus[],
  providerId: string,
): ProviderStatus[] {
  const deleted = providers.find(
    (provider) => provider.provider_id === providerId,
  );
  if (deleted?.is_custom) {
    return providers.filter((provider) => provider.provider_id !== providerId);
  }
  return providers.map((provider) =>
    provider.provider_id === providerId
      ? {
          ...provider,
          configured: false,
          active: false,
          enabled_model_ids: [],
          default_model_id: null,
          model_count: provider.models.length,
          models: provider.models.map((model) => ({
            ...model,
            enabled: false,
          })),
          probe_status: 'not_configured',
          probe_error_code: null,
          latency_ms: null,
          last_probed_at: null,
          last_synced_at: null,
        }
      : provider,
  );
}

/** 将初始化阶段的未知异常压缩成一条不泄露本地路径的提示。 */
function loadErrorMessage(error: unknown): string {
  if (error instanceof Error && error.name === 'AbortError') return '';
  return '部分本地数据暂时不可用，请确认 sidecar 已启动。';
}

/** PageFerry 的唯一主窗口。 */
export function App() {
  const [activeRoute, setActiveRoute] = useState<AppRoute>('translate');
  const [serviceState, setServiceState] = useState<ServiceState>('checking');
  const [serviceVersion, setServiceVersion] = useState('');
  const [catalog, setCatalog] = useState<ModelCatalog | null>(null);
  const [providers, setProviders] = useState<ProviderStatus[]>([]);
  const [jobs, setJobs] = useState<TranslationJob[]>([]);
  const [sessionJobIds, setSessionJobIds] = useState<string[]>([]);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();

    /** 并行读取四个独立 endpoint，避免模型和任务请求形成启动瀑布。 */
    async function loadInitialState() {
      const results = await Promise.allSettled([
        getHealth(controller.signal),
        getModelCatalog(controller.signal),
        getProviderStatuses(controller.signal),
        getJobs(controller.signal),
      ]);
      if (controller.signal.aborted) return;

      const [healthResult, catalogResult, providerResult, jobsResult] = results;
      if (healthResult.status === 'fulfilled') {
        setServiceState('connected');
        setServiceVersion(healthResult.value.data.version);
      } else {
        setServiceState('offline');
      }
      if (catalogResult.status === 'fulfilled') setCatalog(catalogResult.value);
      if (providerResult.status === 'fulfilled') {
        setProviders(providerResult.value);
      }
      if (jobsResult.status === 'fulfilled') setJobs(jobsResult.value);

      const firstFailure = results.find(
        (result) => result.status === 'rejected',
      );
      if (firstFailure?.status === 'rejected') {
        const message = loadErrorMessage(firstFailure.reason);
        if (message) setLoadError(message);
      }
    }

    void loadInitialState();
    return () => controller.abort();
  }, []);

  const hasActiveJobs = jobs.some(
    (job) => job.status === 'queued' || job.status === 'running',
  );

  useEffect(() => {
    if (!hasActiveJobs) return;
    const controller = new AbortController();
    let timer: number | undefined;
    let disposed = false;

    /** 完成一次请求后再排下一次，避免慢请求在 interval 中重叠。 */
    async function pollJobs() {
      try {
        const next = await getJobs(controller.signal);
        if (!disposed) setJobs(next);
      } catch (error) {
        if (!disposed && loadErrorMessage(error)) {
          setLoadError('任务状态暂时无法刷新。');
        }
      } finally {
        if (!disposed) timer = window.setTimeout(pollJobs, 1400);
      }
    }

    timer = window.setTimeout(pollJobs, 700);
    return () => {
      disposed = true;
      controller.abort();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [hasActiveJobs]);

  /** 根据来源是 Tauri path 还是浏览器 File，调用对应创建 endpoint。 */
  async function startTranslation(input: StartTranslationInput) {
    const common = {
      source_language: input.sourceLanguage,
      target_language: input.targetLanguage,
      provider_id: input.providerId,
      model_id: input.modelId,
      ...(input.options ? { options: input.options } : {}),
    };
    const job =
      input.document.source === 'path'
        ? await createPathJob({
            ...common,
            source_path: input.document.path,
          })
        : await createUploadJob({
            ...common,
            file: input.document.file,
          });
    setJobs((current) => upsertJob(current, job));
    setSessionJobIds((current) =>
      current.includes(job.id) ? current : [job.id, ...current],
    );
    setLoadError(null);
  }

  /** 保存任意 provider 的启用模型集合，并合并后端权威状态。 */
  async function saveProvider(
    providerId: string,
    input: ConfigureProviderInput,
  ) {
    const next = await configureProvider(providerId, input);
    setProviders((current) => upsertProvider(current, next));
    return next;
  }

  /** 使用当前输入执行一次不持久化的 provider 连接检测。 */
  function checkProvider(
    providerId: string,
    input: ProbeProviderInput,
  ): Promise<ProviderProbeResult> {
    return probeProvider(providerId, input);
  }

  /** 非破坏地切换 provider 是否进入翻译 runtime，并合并后端权威状态。 */
  async function changeProviderActive(providerId: string, active: boolean) {
    const next = await setProviderActive(providerId, active);
    setProviders((current) => upsertProvider(current, next));
    return next;
  }

  /** 即时启停模型，并接收后端可能同时调整的默认模型。 */
  async function changeModelEnabled(
    providerId: string,
    modelId: string,
    enabled: boolean,
  ) {
    const next = await setProviderModelEnabled(providerId, modelId, enabled);
    setProviders((current) => upsertProvider(current, next));
    return next;
  }

  /** 幂等同步已配置 provider 的模型 inventory，并直接合并响应中的权威列表。 */
  async function syncModels(providerId: string): Promise<ProviderModelSync> {
    const result = await syncProviderModels(providerId);
    setProviders((current) =>
      current.map((provider) =>
        provider.provider_id === providerId
          ? {
              ...provider,
              models: result.models,
              model_count: result.models.length,
              last_synced_at: result.last_synced_at,
            }
          : provider,
      ),
    );
    return result;
  }

  /** 登记手动模型并只更新 inventory，不提前把未 probe 模型暴露给翻译页。 */
  async function addProviderModel(
    providerId: string,
    input: CreateProviderModelInput,
  ): Promise<ProviderModelStatus> {
    const next = await createProviderModel(providerId, input);
    setProviders((current) =>
      current.map((provider) => {
        if (provider.provider_id !== providerId) return provider;
        const models = provider.models.some((model) => model.id === next.id)
          ? provider.models.map((model) =>
              model.id === next.id ? next : model,
            )
          : [...provider.models, next];
        return { ...provider, models, model_count: models.length };
      }),
    );
    return next;
  }

  /** 保存模型 runtime settings，并只替换对应模型，避免刷新整页配置。 */
  async function saveModelSettings(
    providerId: string,
    modelId: string,
    input: UpdateProviderModelSettingsInput,
  ): Promise<ProviderModelStatus> {
    const next = await updateProviderModelSettings(providerId, modelId, input);
    setProviders((current) =>
      current.map((provider) =>
        provider.provider_id === providerId
          ? {
              ...provider,
              models: provider.models.map((model) =>
                model.id === modelId ? next : model,
              ),
            }
          : provider,
      ),
    );
    return next;
  }

  /** 创建 custom provider，并让翻译页与设置页共享同一份状态。 */
  async function addCustomProvider(input: CreateCustomProviderInput) {
    const next = await createCustomProvider(input);
    setProviders((current) => upsertProvider(current, next));
    return next;
  }

  /** 删除成功后优先刷新权威列表；刷新失败则使用等价的本地状态收敛。 */
  async function removeProvider(providerId: string) {
    await deleteProvider(providerId);
    try {
      const next = await getProviderStatuses();
      setProviders(next);
    } catch {
      // DELETE 已落地，不能把后续 GET 故障误报成删除失败或继续展示 stale 配置。
      setProviders((current) => applyDeletedProvider(current, providerId));
    }
  }

  /** 在品牌行的固定锚点切换侧栏宽度。 */
  function toggleSidebar() {
    setSidebarCollapsed((current) => !current);
  }

  const sessionJobIdSet = new Set(sessionJobIds);
  const sessionJobs = jobs.filter((job) => sessionJobIdSet.has(job.id));
  const activeProviderCount = providers.filter(
    (provider) => provider.active,
  ).length;
  const serviceLabel = {
    checking: '正在连接',
    connected: serviceVersion ? `v${serviceVersion}` : 'PageFerry',
    offline: '服务离线',
  }[serviceState];
  const desktopPlatform = detectDesktopPlatform(window.navigator.userAgent);

  return (
    <div
      className={`app-shell ${sidebarCollapsed ? 'app-shell--sidebar-collapsed' : ''}`}
      data-platform={desktopPlatform}
    >
      <AppTitlebar platform={desktopPlatform} />

      <AppSidebar
        activeRoute={activeRoute}
        collapsed={sidebarCollapsed}
        activeProviderCount={activeProviderCount}
        serviceState={serviceState}
        serviceLabel={serviceLabel}
        onNavigate={setActiveRoute}
        onToggleCollapsed={toggleSidebar}
      />

      <div className="app-pane">
        {loadError ? (
          <div className="app-alert" role="alert">
            <AlertTriangle aria-hidden="true" size={15} />
            <span>{loadError}</span>
            <button type="button" onClick={() => setLoadError(null)}>
              关闭
            </button>
          </div>
        ) : null}

        <main
          className={`app-main ${activeRoute === 'providers' ? 'app-main--providers' : ''}`}
        >
          <div className="route-view" hidden={activeRoute !== 'translate'}>
            <TranslationWorkspace
              active={activeRoute === 'translate'}
              catalog={catalog}
              providers={providers}
              jobs={sessionJobs}
              onOpenModelSettings={() => setActiveRoute('providers')}
              onStart={startTranslation}
            />
          </div>
          <div className="route-view" hidden={activeRoute !== 'history'}>
            <HistoryPage catalog={catalog} jobs={jobs} />
          </div>
          <div className="route-view" hidden={activeRoute !== 'providers'}>
            <ProviderPage
              catalog={catalog}
              providers={providers}
              onCreate={addCustomProvider}
              onAddModel={addProviderModel}
              onLoadApiKey={getProviderApiKey}
              onProbe={checkProvider}
              onSave={saveProvider}
              onProviderActiveChange={changeProviderActive}
              onModelEnabledChange={changeModelEnabled}
              onSyncModels={syncModels}
              onSaveModelSettings={saveModelSettings}
              onDelete={removeProvider}
            />
          </div>
        </main>
      </div>
    </div>
  );
}
