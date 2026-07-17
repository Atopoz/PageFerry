/** 展示可复用的翻译任务列表，并通过受限 Tauri command 打开结果。 */

import {
  AlertCircle,
  Check,
  CheckCircle2,
  FileText,
  FolderOpen,
  LoaderCircle,
  MoreHorizontal,
} from 'lucide-react';

import { DocumentTypeIcon } from '@/features/documents/DocumentTypeIcon';
import type { ModelCatalog, TranslationJob } from '@/lib/api';

interface JobListProps {
  jobs: TranslationJob[];
  catalog: ModelCatalog | null;
  emptyMessage?: string;
  showCreatedAt?: boolean;
  onError: (message: string) => void;
}

const jobStages = [
  { id: 'extracting', label: '提取内容' },
  { id: 'translating', label: '翻译文本' },
  { id: 'formatting', label: '生成文档' },
] as const;

type JobStageState = 'pending' | 'active' | 'complete' | 'error';

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
function jobErrorLabel(errorCode: string): string {
  const labels: Record<string, string> = {
    process_interrupted: '应用上次意外退出',
    source_unavailable: '源文件不可读取',
    pipeline_failed: '文档处理失败',
    provider_key: '模型密钥不可用',
    provider_endpoint: '模型 endpoint 不可用',
    provider_model: '模型不可用',
    provider_rate_limit: '请求过于频繁',
    provider_network: '模型网络异常',
    provider_protocol: '模型响应异常',
  };
  return labels[errorCode] ?? '任务未完成';
}

/** 将任务状态压缩成任务行中可扫读的一句话。 */
function jobStatusLabel(job: TranslationJob): string {
  if (job.status === 'running') return '处理中';
  if (job.status === 'succeeded' && job.fallback_segments > 0) {
    return `完成 · ${job.fallback_segments} 处回退`;
  }
  if (job.status === 'failed' && job.error_code) {
    return `失败 · ${jobErrorLabel(job.error_code)}`;
  }
  return {
    queued: '等待中',
    succeeded: '已完成',
    failed: '失败',
    cancelled: '已取消',
  }[job.status];
}

/** 返回当前 stage 在三阶段时间线中的稳定序号。 */
function currentStageIndex(job: TranslationJob): number {
  const index = jobStages.findIndex((stage) => stage.id === job.progress_stage);
  return index < 0 ? 0 : index;
}

/** 根据 job 状态判断单个阶段应显示为等待、运行、完成还是失败。 */
function jobStageState(job: TranslationJob, stageIndex: number): JobStageState {
  if (job.status === 'succeeded') return 'complete';
  const current = currentStageIndex(job);
  if (stageIndex < current) return 'complete';
  if (stageIndex > current) return 'pending';
  if (job.status === 'failed' || job.status === 'cancelled') return 'error';
  return 'active';
}

/** 为屏幕阅读器生成不依赖视觉圆点的真实阶段进度摘要。 */
function jobStageAriaLabel(job: TranslationJob): string {
  const current = jobStages[currentStageIndex(job)];
  if (job.status === 'succeeded') return '任务进度：三个阶段均已完成';
  if (job.status === 'queued') return '任务进度：等待提取内容';
  if (job.progress_stage === 'translating' && job.total_segments > 0) {
    return `任务进度：${current.label}，已处理 ${job.processed_segments} / ${job.total_segments} 个片段`;
  }
  return `任务进度：${current.label}`;
}

/** 渲染提取、翻译、生成三个离散阶段，中段只展示 backend 的真实片段计数。 */
function JobStageTimeline({ job }: { job: TranslationJob }) {
  return (
    <div className="job-stage-timeline" aria-label={jobStageAriaLabel(job)}>
      {jobStages.map((stage, index) => {
        const state = jobStageState(job, index);
        const showCounter =
          stage.id === 'translating' &&
          job.total_segments > 0 &&
          (job.status === 'succeeded' || job.progress_stage === 'translating');
        return (
          <span className="job-stage" data-state={state} key={stage.id}>
            <span className="job-stage-marker" aria-hidden="true">
              {state === 'complete' ? <Check size={9} strokeWidth={3} /> : null}
            </span>
            <span className="job-stage-copy">
              <strong>{stage.label}</strong>
              {showCounter ? (
                <small>
                  {job.status === 'succeeded'
                    ? `${job.total_segments} 个片段`
                    : `${job.processed_segments} / ${job.total_segments}`}
                </small>
              ) : null}
            </span>
          </span>
        );
      })}
    </div>
  );
}

/** 把 ISO 时间格式化为历史列表中的紧凑本地时间。 */
function formatJobTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date);
}

/** 将 job 中的语言代码转换为历史页中的短标签。 */
function languageLabel(value: string | null): string {
  const labels: Record<string, string> = {
    'zh-CN': '简体中文',
    'zh-TW': '繁體中文（台湾）',
    'zh-HK': '繁體中文（香港）',
    en: 'English',
    ja: '日本語',
    ko: '한국어',
    fr: 'Français',
    de: 'Deutsch',
    es: 'Español',
    ru: 'Русский',
  };
  if (value === null || value === 'auto') return '自动识别';
  return labels[value] ?? value;
}

/** 判断 renderer 是否运行在 Tauri IPC 环境。 */
function isTauriRuntime(): boolean {
  return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;
}

/** 请求 Rust 校验输出路径，再调用系统默认应用打开文件。 */
async function openOutput(path: string): Promise<void> {
  if (!isTauriRuntime()) {
    throw new Error('请在 PageFerry 桌面版中打开输出文件。');
  }
  const { invoke } = await import('@tauri-apps/api/core');
  await invoke('open_output', { path });
}

/** 渲染当前会话和历史页共用的任务行。 */
export function JobList({
  jobs,
  catalog,
  emptyMessage,
  showCreatedAt = false,
  onError,
}: JobListProps) {
  /** 打开一个已完成任务，并把原生 opener 失败交给所属页面展示。 */
  async function openJobOutput(path: string) {
    try {
      await openOutput(path);
    } catch (error) {
      onError(error instanceof Error ? error.message : '无法打开输出文件。');
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
        const outputPath = job.output_path;
        const active = job.status === 'running' || job.status === 'queued';
        return (
          <article className="job-row" key={job.id}>
            <span className={`file-type file-type--${job.document_type}`}>
              <DocumentTypeIcon kind={job.document_type} />
            </span>

            <div className="file-primary">
              <strong>{job.source_name}</strong>
              <span>
                {modelDisplayName(catalog, job.model_id)}
                {showCreatedAt
                  ? ` · ${languageLabel(job.source_language)} → ${languageLabel(job.target_language)} · ${formatJobTime(job.created_at)}`
                  : ''}
              </span>
            </div>

            <JobStageTimeline job={job} />

            <div className={`job-state job-state--${job.status}`}>
              {active ? (
                <LoaderCircle className="spin" aria-hidden="true" size={15} />
              ) : job.status === 'succeeded' ? (
                <CheckCircle2 aria-hidden="true" size={16} />
              ) : (
                <AlertCircle aria-hidden="true" size={16} />
              )}
              {jobStatusLabel(job)}
            </div>

            {outputPath ? (
              <button
                className="row-action"
                type="button"
                onClick={() => void openJobOutput(outputPath)}
              >
                <FolderOpen aria-hidden="true" size={15} />
                打开文件
              </button>
            ) : (
              <button
                className="icon-button"
                type="button"
                aria-label="任务菜单"
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
