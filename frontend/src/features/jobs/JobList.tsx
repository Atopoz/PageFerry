/** 展示可复用的翻译任务列表，并通过受限 Tauri command 打开结果。 */

import {
  AlertCircle,
  Ban,
  CheckCircle2,
  Clock,
  FileText,
  FolderOpen,
  FolderSearch,
  MoreHorizontal,
  PanelsTopLeft,
} from 'lucide-react';
import { DropdownMenu } from 'radix-ui';

import { DocumentTypeIcon } from '@/features/documents/DocumentTypeIcon';
import { useI18n, type Translate, type UiLocale } from '@/i18n/i18n';
import { translationLanguageLabel } from '@/i18n/translation-languages';
import type {
  ModelCatalog,
  TranslationArtifact,
  TranslationJob,
} from '@/lib/api';

type OutputAction = 'open' | 'reveal' | 'choose_application';

interface JobListProps {
  jobs: TranslationJob[];
  catalog: ModelCatalog | null;
  emptyMessage?: string;
  showCreatedAt?: boolean;
  onError: (message: string) => void;
}

const jobStages = [
  { id: 'extracting', messageKey: 'jobs.stage.extracting' },
  { id: 'translating', messageKey: 'jobs.stage.translating' },
  { id: 'formatting', messageKey: 'jobs.stage.formatting' },
] as const;

/** 从 bundled catalog 找出模型显示名，未加载时保留稳定 id。 */
function modelDisplayName(
  catalog: ModelCatalog | null,
  modelId: string,
): string {
  return (
    catalog?.models.find((model) => model.id === modelId)?.display_name ??
    modelId
  );
}

/** 将持久化 error code 映射成不暴露内部细节的短文案。 */
function jobErrorLabel(errorCode: string, t: Translate): string {
  const labels: Record<string, Parameters<Translate>[0]> = {
    process_interrupted: 'jobs.error.processInterrupted',
    source_unavailable: 'jobs.error.sourceUnavailable',
    pipeline_failed: 'jobs.error.pipelineFailed',
    provider_key: 'jobs.error.providerKey',
    provider_endpoint: 'jobs.error.providerEndpoint',
    provider_model: 'jobs.error.providerModel',
    provider_rate_limit: 'jobs.error.providerRateLimit',
    provider_network: 'jobs.error.providerNetwork',
    provider_protocol: 'jobs.error.providerProtocol',
    pdf_no_text_layer: 'jobs.error.pdfNoTextLayer',
    pdf_encrypted: 'jobs.error.pdfEncrypted',
    pdf_corrupt: 'jobs.error.pdfCorrupt',
    pdf_layout_model_missing: 'jobs.error.pdfLayoutModelMissing',
    pdf_font_directory_missing: 'jobs.error.pdfFontResourcesMissing',
    pdf_font_resource_missing: 'jobs.error.pdfFontResourcesMissing',
    pdf_font_prepare_failed: 'jobs.error.pdfFontPrepareFailed',
    pdf_unsupported: 'jobs.error.pdfUnsupported',
  };
  const key = labels[errorCode];
  return key ? t(key) : t('jobs.error.incomplete');
}

/** 返回运行中任务的当前阶段标签，翻译阶段附带真实片段计数。 */
function runningLabel(job: TranslationJob, t: Translate): string {
  const stage = jobStages.find((item) => item.id === job.progress_stage);
  const label = stage ? t(stage.messageKey) : t('jobs.stage.processing');
  if (job.progress_stage === 'translating' && job.total_segments > 0) {
    return `${label} ${job.processed_segments} / ${job.total_segments}`;
  }
  return label;
}

/** 将任务状态压缩成状态 pill 中可扫读的一句话。 */
function jobStatusLabel(job: TranslationJob, t: Translate): string {
  if (job.status === 'running') return runningLabel(job, t);
  if (job.status === 'succeeded' && job.fallback_segments > 0) {
    return t('jobs.status.fallback', { count: job.fallback_segments });
  }
  if (job.status === 'failed' && job.error_code) {
    return t('jobs.status.failed', {
      reason: jobErrorLabel(job.error_code, t),
    });
  }
  const keys = {
    queued: 'jobs.status.queued',
    succeeded: 'jobs.status.succeeded',
    failed: 'jobs.status.failedBare',
    cancelled: 'jobs.status.cancelled',
  } as const;
  return t(keys[job.status]);
}

/** 为屏幕阅读器生成运行中任务的进度摘要，与 pill 可见文案互补。 */
function runningAriaLabel(job: TranslationJob, t: Translate): string {
  if (job.progress_stage === 'translating' && job.total_segments > 0) {
    return t('jobs.progress.segments', {
      processed: job.processed_segments,
      total: job.total_segments,
    });
  }
  return t('jobs.progress.stage', { stage: runningLabel(job, t) });
}

/** 把 ISO 时间格式化为历史列表中的紧凑本地时间。 */
function formatJobTime(value: string, locale: UiLocale): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return new Intl.DateTimeFormat(locale, {
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date);
}

/** 判断 renderer 是否运行在 Tauri IPC 环境。 */
function isTauriRuntime(): boolean {
  return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;
}

/** 请求 Rust 校验输出路径，再调用系统默认应用打开文件。 */
async function runOutputAction(
  path: string,
  action: OutputAction,
  desktopOnlyMessage: string,
): Promise<void> {
  if (!isTauriRuntime()) {
    throw new Error(desktopOnlyMessage);
  }
  const { invoke } = await import('@tauri-apps/api/core');
  await invoke('open_output', { path, action });
}

/** 为旧任务补出译文 artifact，避免 migration 前的历史结果失去打开入口。 */
function jobArtifacts(job: TranslationJob): TranslationArtifact[] {
  if (job.artifacts.length > 0) return job.artifacts;
  return job.output_path ? [{ kind: 'translated', path: job.output_path }] : [];
}

/** 渲染当前会话和历史页共用的任务行。 */
export function JobList({
  jobs,
  catalog,
  emptyMessage,
  showCreatedAt = false,
  onError,
}: JobListProps) {
  const { locale, t } = useI18n();

  /** 打开一个已完成任务，并把原生 opener 失败交给所属页面展示。 */
  async function runJobOutputAction(path: string, action: OutputAction) {
    try {
      await runOutputAction(path, action, t('jobs.desktopOnly'));
    } catch (error) {
      onError(error instanceof Error ? error.message : t('jobs.openFailed'));
    }
  }

  if (jobs.length === 0) {
    return emptyMessage ? (
      <div className="jobs-empty">
        <FileText aria-hidden="true" size={18} />
        <span>{emptyMessage}</span>
      </div>
    ) : null;
  }

  return (
    <div className="job-list" aria-live="polite">
      {jobs.map((job) => {
        const artifacts = jobArtifacts(job);
        return (
          <article className="job-row" key={job.id}>
            <span className="file-type">
              <DocumentTypeIcon kind={job.document_type} />
            </span>

            <div className="file-primary">
              <strong>{job.source_name}</strong>
              <span className="job-meta">
                <b>{modelDisplayName(catalog, job.model_id)}</b>
                {showCreatedAt ? (
                  <i>
                    {translationLanguageLabel(job.source_language, t)} →{' '}
                    {translationLanguageLabel(job.target_language, t)} ·{' '}
                    {formatJobTime(job.created_at, locale)}
                  </i>
                ) : null}
              </span>
            </div>

            <div
              className={`job-state job-state--${job.status}`}
              key={`${job.status}:${job.progress_stage}`}
              aria-label={
                job.status === 'running' ? runningAriaLabel(job, t) : undefined
              }
            >
              {job.status === 'running' ? (
                <span className="job-state-dot" aria-hidden="true" />
              ) : job.status === 'queued' ? (
                <Clock aria-hidden="true" size={13} />
              ) : job.status === 'succeeded' ? (
                <CheckCircle2 aria-hidden="true" size={14} />
              ) : job.status === 'cancelled' ? (
                <Ban aria-hidden="true" size={13} />
              ) : (
                <AlertCircle aria-hidden="true" size={14} />
              )}
              <span>{jobStatusLabel(job, t)}</span>
            </div>

            {artifacts.length > 0 ? (
              <div className="artifact-actions">
                {artifacts.map((artifact) => (
                  <div className="artifact-action" key={artifact.kind}>
                    <button
                      className="row-action artifact-open-button"
                      type="button"
                      onClick={() =>
                        void runJobOutputAction(artifact.path, 'open')
                      }
                    >
                      <FolderOpen aria-hidden="true" size={15} />
                      {artifact.kind === 'bilingual'
                        ? t('jobs.openBilingual')
                        : t('jobs.openTranslated')}
                    </button>
                    <DropdownMenu.Root>
                      <DropdownMenu.Trigger asChild>
                        <button
                          className="row-action artifact-menu-trigger"
                          type="button"
                          aria-label={t('jobs.openOptions', {
                            artifact:
                              artifact.kind === 'bilingual'
                                ? t('jobs.artifact.bilingual')
                                : t('jobs.artifact.translated'),
                          })}
                        >
                          <MoreHorizontal aria-hidden="true" size={16} />
                        </button>
                      </DropdownMenu.Trigger>
                      <DropdownMenu.Portal>
                        <DropdownMenu.Content
                          className="artifact-menu"
                          sideOffset={5}
                          align="end"
                        >
                          <DropdownMenu.Item
                            className="artifact-menu-item"
                            onSelect={() =>
                              void runJobOutputAction(artifact.path, 'reveal')
                            }
                          >
                            <FolderSearch aria-hidden="true" size={14} />
                            {t('jobs.revealFile')}
                          </DropdownMenu.Item>
                          <DropdownMenu.Item
                            className="artifact-menu-item"
                            onSelect={() =>
                              void runJobOutputAction(
                                artifact.path,
                                'choose_application',
                              )
                            }
                          >
                            <PanelsTopLeft aria-hidden="true" size={14} />
                            {t('jobs.chooseApplication')}
                          </DropdownMenu.Item>
                        </DropdownMenu.Content>
                      </DropdownMenu.Portal>
                    </DropdownMenu.Root>
                  </div>
                ))}
              </div>
            ) : (
              <button
                className="icon-button"
                type="button"
                aria-label={t('jobs.menu')}
                disabled
              >
                <MoreHorizontal aria-hidden="true" size={18} />
              </button>
            )}
          </article>
        );
      })}
    </div>
  );
}
