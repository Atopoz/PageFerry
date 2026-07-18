/** 展示 PDF resource pack 的安装、进度、取消与原位重试。 */

import { Check, Download, LoaderCircle, RotateCcw, X } from 'lucide-react';
import { Dialog } from 'radix-ui';
import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from 'react';

import { Button } from '@/components/ui/button';
import { useI18n, type Translate } from '@/i18n/i18n';
import type { MessageKey } from '@/i18n/messages';
import {
  cancelPdfResourceInstall,
  getPdfResourceStatus,
  installPdfResources,
  type PdfResourceItem,
  type PdfResourceState,
  type PdfResourceStatus,
} from '@/lib/api';

export type PdfResourceAction = 'check' | 'install' | 'cancel' | null;

interface PdfResourceDialogProps {
  open: boolean;
  status: PdfResourceStatus | null;
  action: PdfResourceAction;
  actionFailed: boolean;
  onOpenChange: (open: boolean) => void;
  onRefresh: () => void;
  onInstall: () => void;
  onCancel: () => void;
}

/** 将资源字节数压缩成短、稳定的可扫读文本。 */
function formatBytes(bytes: number): string {
  const safeBytes = Math.max(0, bytes);
  if (safeBytes < 1024 * 1024) {
    return `${Math.max(0, Math.round(safeBytes / 1024))} KB`;
  }
  return `${(safeBytes / 1024 / 1024).toFixed(1)} MB`;
}

/** 将后端 pack id 映射成产品文案，不把存储细节带进界面。 */
function resourceName(pack: string, t: Translate): string {
  if (pack === 'layout') return t('pdfResources.layout');
  if (pack === 'fonts-common-zh-cn') {
    return t('pdfResources.fontsCommonZhCN');
  }
  return t('pdfResources.unknown');
}

/** 根据总体下载状态与单项进度生成资源行状态。 */
function resourceState(
  resource: PdfResourceItem,
  state: PdfResourceState,
): MessageKey {
  if (resource.ready) return 'pdfResources.status.ready';
  if (state === 'downloading') {
    return resource.completed_bytes > 0
      ? 'pdfResources.status.downloading'
      : 'pdfResources.status.queued';
  }
  if (state === 'cancelling') return 'pdfResources.status.cancelling';
  if (state === 'failed') return 'pdfResources.status.failed';
  if (state === 'cancelled') return 'pdfResources.status.cancelled';
  return 'pdfResources.status.missing';
}

/** 将稳定失败码翻译成可操作提示，不把内部异常正文暴露给用户。 */
function failedMessage(errorCode: string | null, t: Translate): string {
  const messages: Record<string, MessageKey> = {
    insufficient_disk_space: 'pdfResources.error.insufficientDiskSpace',
    download_failed: 'pdfResources.error.downloadFailed',
    integrity_check_failed: 'pdfResources.error.integrityCheckFailed',
    filesystem_error: 'pdfResources.error.filesystem',
  };
  const key = errorCode ? messages[errorCode] : undefined;
  return t(key ?? 'pdfResources.failed');
}

/** 渲染一项确定性的 PDF 运行资源及其字节进度。 */
function ResourceRow({
  resource,
  state,
}: {
  resource: PdfResourceItem;
  state: PdfResourceState;
}) {
  const { t } = useI18n();
  const completed = Math.min(
    Math.max(0, resource.completed_bytes),
    Math.max(0, resource.size_bytes),
  );
  const showPartial =
    !resource.ready && state === 'downloading' && completed > 0;

  return (
    <li className="pdf-resource-row">
      <span
        className={`pdf-resource-state-mark pdf-resource-state-mark--${resource.ready ? 'ready' : state}`}
        aria-hidden="true"
      />
      <span className="pdf-resource-row-copy">
        <strong>{resourceName(resource.pack, t)}</strong>
        <small>
          {showPartial
            ? t('pdfResources.progressValue', {
                completed: formatBytes(completed),
                total: formatBytes(resource.size_bytes),
              })
            : formatBytes(resource.size_bytes)}
        </small>
      </span>
      <span className="pdf-resource-row-status">
        {t(resourceState(resource, state))}
      </span>
    </li>
  );
}

/** 用 Radix Dialog 保持焦点、Escape 与屏幕阅读器语义一致。 */
export function PdfResourceDialog({
  open,
  status,
  action,
  actionFailed,
  onOpenChange,
  onRefresh,
  onInstall,
  onCancel,
}: PdfResourceDialogProps) {
  const { t } = useI18n();
  const contentRef = useRef<HTMLDivElement>(null);
  const state = status?.state;
  const totalBytes = Math.max(0, status?.total_bytes ?? 0);
  const completedBytes = Math.min(
    Math.max(0, status?.completed_bytes ?? 0),
    totalBytes,
  );
  const progress =
    totalBytes === 0 ? 0 : Math.round((completedBytes / totalBytes) * 100);
  const isDownloading = state === 'downloading';
  const isCancelling = state === 'cancelling';
  const isReady = state === 'ready';
  const canInstall =
    state === 'missing' || state === 'failed' || state === 'cancelled';
  const stateMessage = actionFailed
    ? t('pdfResources.actionFailed')
    : state === 'failed'
      ? failedMessage(status?.error_code ?? null, t)
      : state === 'cancelled'
        ? t('pdfResources.cancelled')
        : state === 'ready'
          ? t('pdfResources.done')
          : null;

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-overlay" />
        <Dialog.Content
          ref={contentRef}
          className="pdf-resource-dialog"
          data-state-value={state ?? 'checking'}
          aria-describedby="pdf-resource-description"
          tabIndex={-1}
          onOpenAutoFocus={(event) => {
            // Radix 默认聚焦第一个按钮，会让“稍后再说”在弹窗刚打开时带上键盘 focus 环。
            event.preventDefault();
            contentRef.current?.focus({ preventScroll: true });
          }}
        >
          <header>
            <div>
              <Dialog.Title>{t('pdfResources.title')}</Dialog.Title>
              <Dialog.Description id="pdf-resource-description">
                {t('pdfResources.description')}
              </Dialog.Description>
            </div>
            {isDownloading || isCancelling ? (
              <Dialog.Close asChild>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-sm"
                  aria-label={t('pdfResources.close')}
                >
                  <X aria-hidden="true" />
                </Button>
              </Dialog.Close>
            ) : null}
          </header>

          <div className="pdf-resource-body">
            {status === null && !actionFailed ? (
              <p className="pdf-resource-checking" role="status">
                <LoaderCircle className="spin" aria-hidden="true" />
                {t('pdfResources.loading')}
              </p>
            ) : status !== null ? (
              <>
                <ul className="pdf-resource-list">
                  {status.resources.map((resource) => (
                    <ResourceRow
                      key={resource.pack}
                      resource={resource}
                      state={status.state}
                    />
                  ))}
                </ul>

                <div className="pdf-resource-progress-block">
                  <div
                    className="pdf-resource-progress-track"
                    role="progressbar"
                    aria-label={t('pdfResources.progress')}
                    aria-valuemin={0}
                    aria-valuemax={totalBytes}
                    aria-valuenow={completedBytes}
                    aria-valuetext={t('pdfResources.progressValue', {
                      completed: formatBytes(completedBytes),
                      total: formatBytes(totalBytes),
                    })}
                  >
                    <span style={{ width: `${progress}%` }} />
                  </div>
                  <small aria-hidden="true">
                    {t('pdfResources.progressValue', {
                      completed: formatBytes(completedBytes),
                      total: formatBytes(totalBytes),
                    })}
                  </small>
                </div>
              </>
            ) : null}

            {stateMessage ? (
              <p
                className={`pdf-resource-message pdf-resource-message--${isReady ? 'ready' : 'attention'}`}
                role={isReady ? 'status' : 'alert'}
                aria-live="polite"
              >
                {isReady ? <Check aria-hidden="true" /> : null}
                {stateMessage}
              </p>
            ) : null}
          </div>

          <footer>
            {!isReady && !isDownloading && !isCancelling ? (
              <Dialog.Close asChild>
                <Button
                  key="later"
                  type="button"
                  variant="outline"
                  className="pdf-resource-action"
                >
                  {t('pdfResources.later')}
                </Button>
              </Dialog.Close>
            ) : null}

            {status === null && actionFailed ? (
              <Button
                key={action === 'check' ? 'refresh-pending' : 'refresh-idle'}
                type="button"
                className="pdf-resource-action"
                disabled={action !== null}
                onClick={onRefresh}
              >
                {action === 'check' ? (
                  <LoaderCircle className="spin" aria-hidden="true" />
                ) : (
                  <RotateCcw aria-hidden="true" />
                )}
                {action === 'check'
                  ? t('pdfResources.loading')
                  : t('pdfResources.checkAgain')}
              </Button>
            ) : isDownloading || isCancelling ? (
              <Button
                key={
                  action === 'cancel' || isCancelling
                    ? 'cancel-pending'
                    : 'cancel-idle'
                }
                type="button"
                variant="destructive"
                className="pdf-resource-action"
                disabled={action !== null || isCancelling}
                onClick={onCancel}
              >
                {action === 'cancel' || isCancelling ? (
                  <LoaderCircle className="spin" aria-hidden="true" />
                ) : (
                  <X aria-hidden="true" />
                )}
                {action === 'cancel' || isCancelling
                  ? t('pdfResources.cancelling')
                  : t('pdfResources.cancel')}
              </Button>
            ) : canInstall ? (
              <Button
                key={action === 'install' ? 'install-pending' : 'install-idle'}
                type="button"
                className="pdf-resource-action"
                disabled={action !== null}
                onClick={onInstall}
              >
                {action === 'install' ? (
                  <LoaderCircle className="spin" aria-hidden="true" />
                ) : state === 'failed' || state === 'cancelled' ? (
                  <RotateCcw aria-hidden="true" />
                ) : (
                  <Download aria-hidden="true" />
                )}
                {action === 'install'
                  ? t('pdfResources.starting')
                  : state === 'failed' || state === 'cancelled'
                    ? t('pdfResources.retry')
                    : t('pdfResources.install')}
              </Button>
            ) : null}
          </footer>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

export interface PdfResourceInstallerHandle {
  requireReady: () => boolean;
}

interface PdfResourceInstallerProps {
  initialStatus: PdfResourceStatus | null;
}

/**
 * 在独立 component 内维护高频下载进度，避免每次轮询都重绘整个应用壳。
 */
export const PdfResourceInstaller = forwardRef<
  PdfResourceInstallerHandle,
  PdfResourceInstallerProps
>(function PdfResourceInstaller({ initialStatus }, ref) {
  const [status, setStatus] = useState<PdfResourceStatus | null>(initialStatus);
  const [open, setOpen] = useState(
    () => initialStatus !== null && initialStatus.state !== 'ready',
  );
  const [action, setAction] = useState<PdfResourceAction>(null);
  const [actionFailed, setActionFailed] = useState(false);
  const statusRef = useRef<PdfResourceStatus | null>(initialStatus);
  const autoPromptedRef = useRef(
    initialStatus !== null && initialStatus.state !== 'ready',
  );
  const operationInFlightRef = useRef(false);

  /** 同步更新 render state 与 imperative guard 使用的即时快照。 */
  const updateStatus = useCallback((next: PdfResourceStatus) => {
    statusRef.current = next;
    setStatus(next);
  }, []);

  /** null 既可能是启动请求失败，也可能是用户过早操作；统一允许安全重查。 */
  const refresh = useCallback(async () => {
    if (operationInFlightRef.current) return;
    operationInFlightRef.current = true;
    setAction('check');
    setActionFailed(false);
    try {
      updateStatus(await getPdfResourceStatus());
    } catch {
      setActionFailed(true);
    } finally {
      operationInFlightRef.current = false;
      setAction(null);
    }
  }, [updateStatus]);

  useEffect(() => {
    if (initialStatus === null) return;
    let disposed = false;

    /** 启动快照晚到时异步接收；本地已有动作结果则不再用旧快照覆盖。 */
    queueMicrotask(() => {
      if (disposed || statusRef.current !== null) return;
      updateStatus(initialStatus);
      if (
        initialStatus.state !== 'ready' &&
        autoPromptedRef.current === false
      ) {
        autoPromptedRef.current = true;
        setOpen(true);
      }
    });

    return () => {
      disposed = true;
    };
  }, [initialStatus, updateStatus]);

  useImperativeHandle(
    ref,
    () => ({
      /** 非 ready 时唤起安装入口，并让调用方同步阻止 PDF 任务。 */
      requireReady() {
        const ready = statusRef.current?.state === 'ready';
        if (!ready) {
          setOpen(true);
          if (statusRef.current === null) void refresh();
        }
        return ready;
      },
    }),
    [refresh],
  );

  const isPolling =
    status?.state === 'downloading' || status?.state === 'cancelling';

  useEffect(() => {
    if (!isPolling) return;
    const controller = new AbortController();
    let disposed = false;
    let timer: number | undefined;

    /** 每次 GET 完成后才安排下一次，慢响应不会形成重叠请求。 */
    async function poll() {
      let continuePolling = true;
      try {
        const next = await getPdfResourceStatus(controller.signal);
        if (disposed) return;
        updateStatus(next);
        setActionFailed(false);
        continuePolling =
          next.state === 'downloading' || next.state === 'cancelling';
      } catch (error) {
        if (disposed || (error instanceof Error && error.name === 'AbortError'))
          return;
        setActionFailed(true);
      }
      if (!disposed && continuePolling) {
        timer = window.setTimeout(poll, 700);
      }
    }

    timer = window.setTimeout(poll, 500);
    return () => {
      disposed = true;
      controller.abort();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [isPolling, updateStatus]);

  useEffect(() => {
    if (!open || status?.state !== 'ready') return;
    const timer = window.setTimeout(() => setOpen(false), 900);
    return () => window.clearTimeout(timer);
  }, [open, status?.state]);

  /** 开始或重试安装，后续长任务进度统一交给非重叠轮询。 */
  async function install() {
    if (operationInFlightRef.current) return;
    operationInFlightRef.current = true;
    setAction('install');
    setActionFailed(false);
    try {
      updateStatus(await installPdfResources());
    } catch {
      setActionFailed(true);
    } finally {
      operationInFlightRef.current = false;
      setAction(null);
    }
  }

  /** 请求取消后继续尊重 cancelling 状态，直到 worker 真正退出。 */
  async function cancel() {
    if (operationInFlightRef.current) return;
    operationInFlightRef.current = true;
    setAction('cancel');
    setActionFailed(false);
    try {
      updateStatus(await cancelPdfResourceInstall());
    } catch {
      setActionFailed(true);
    } finally {
      operationInFlightRef.current = false;
      setAction(null);
    }
  }

  return (
    <PdfResourceDialog
      open={open}
      status={status}
      action={action}
      actionFailed={actionFailed}
      onOpenChange={setOpen}
      onRefresh={() => void refresh()}
      onInstall={() => void install()}
      onCancel={() => void cancel()}
    />
  );
});
