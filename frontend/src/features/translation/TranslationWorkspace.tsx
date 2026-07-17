/** 组装单文件翻译：紧凑语言/模型选择、文件选项与本次任务。 */

import {
  AlertCircle,
  ArrowLeftRight,
  CloudUpload,
  LoaderCircle,
  Play,
  Settings2,
  X,
} from 'lucide-react';
import { Switch } from 'radix-ui';
import { useEffect, useRef, useState } from 'react';

import {
  CompactSelect,
  type CompactSelectGroup,
  type CompactSelectOption,
} from '@/components/ui/compact-select';
import {
  DocumentTypeIcon,
  type DocumentTypeKind,
} from '@/features/documents/DocumentTypeIcon';
import { JobList } from '@/features/jobs/JobList';
import { ProviderIcon } from '@/features/providers/ProviderIcon';
import type {
  DocumentTranslationOptions,
  ModelCatalog,
  ProviderStatus,
  TranslationJob,
} from '@/lib/api';

type SupportedDocumentKind = Exclude<DocumentTypeKind, 'pdf'>;

export type PendingDocument =
  | {
      source: 'path';
      name: string;
      path: string;
      kind: SupportedDocumentKind;
    }
  | {
      source: 'file';
      file: File;
      name: string;
      kind: SupportedDocumentKind;
    };

export interface StartTranslationInput {
  document: PendingDocument;
  sourceLanguage: string | null;
  targetLanguage: string;
  providerId: string;
  modelId: string;
  options: DocumentTranslationOptions | null;
}

interface TranslationWorkspaceProps {
  active: boolean;
  catalog: ModelCatalog | null;
  providers: ProviderStatus[];
  jobs: TranslationJob[];
  onOpenModelSettings: () => void;
  onStart: (input: StartTranslationInput) => Promise<void>;
}

interface ModelChoice {
  key: string;
  providerId: string;
  providerName: string;
  modelId: string;
  modelName: string;
}

interface ModelChoiceGroup {
  providerId: string;
  providerName: string;
  choices: ModelChoice[];
}

const supportedExtensions = new Set<SupportedDocumentKind>([
  'docx',
  'pptx',
  'txt',
  'md',
]);

const languageOptions: readonly CompactSelectOption[] = [
  { value: 'zh-CN', label: '简体中文' },
  { value: 'zh-TW', label: '繁體中文（台湾）' },
  { value: 'zh-HK', label: '繁體中文（香港）' },
  { value: 'en', label: 'English' },
  { value: 'ja', label: '日本語' },
  { value: 'ko', label: '한국어' },
  { value: 'fr', label: 'Français' },
  { value: 'de', label: 'Deutsch' },
  { value: 'es', label: 'Español' },
  { value: 'ru', label: 'Русский' },
];

const sourceLanguageOptions: readonly CompactSelectOption[] = [
  { value: 'auto', label: '自动识别' },
  ...languageOptions,
];

/** 返回文件名的小写扩展名。 */
function extensionOf(fileName: string): string {
  return fileName.split('.').at(-1)?.toLowerCase() ?? '';
}

/** 将文件名映射为已经接通的文档 runtime。 */
function documentKindOf(fileName: string): SupportedDocumentKind | null {
  const extension = extensionOf(fileName);
  return supportedExtensions.has(extension as SupportedDocumentKind)
    ? (extension as SupportedDocumentKind)
    : null;
}

/** 从 macOS、Windows 或 Linux 路径中提取显示文件名。 */
function nameFromPath(path: string): string {
  return path.split(/[\\/]/).at(-1) ?? path;
}

/** 判断 renderer 是否运行在 Tauri IPC 环境。 */
function isTauriRuntime(): boolean {
  return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;
}

/** 把字节数压缩成文件行中可扫读的短文本。 */
function formatBytes(bytes: number): string {
  if (bytes === 0) return '0 B';
  if (bytes < 1024 * 1024) {
    return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  }
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/** 返回某类文档不会产生意外遗漏的安全默认选项。 */
function defaultDocumentOptions(
  kind: SupportedDocumentKind,
): DocumentTranslationOptions | null {
  if (kind === 'docx') return { kind, translate_tables: true };
  if (kind === 'pptx') {
    return { kind, translate_tables: true, translate_notes: true };
  }
  return null;
}

/** 把可运行的 provider/model 投影成顺序稳定的供应商分组。 */
function modelChoiceGroups(
  catalog: ModelCatalog | null,
  providers: ProviderStatus[],
): ModelChoiceGroup[] {
  const catalogModelNames = new Map(
    (catalog?.models ?? []).map((model) => [model.id, model.display_name]),
  );
  return providers.flatMap((provider) => {
    if (
      provider.active !== true ||
      !provider.available ||
      !provider.configured ||
      provider.probe_status !== 'succeeded'
    ) {
      return [];
    }
    const enabledModels = provider.enabled_model_ids ?? [];
    const enabledModelIds = new Set(enabledModels);
    const runtimeModels = new Map(
      provider.models.map((model) => [model.id, model]),
    );
    const orderedModelIds = provider.default_model_id
      ? [
          provider.default_model_id,
          ...enabledModels.filter(
            (modelId) => modelId !== provider.default_model_id,
          ),
        ]
      : enabledModels;
    const choices = orderedModelIds.flatMap((modelId) => {
      const model = runtimeModels.get(modelId);
      if (
        model === undefined ||
        !enabledModelIds.has(modelId) ||
        model.enabled !== true ||
        model.available !== true ||
        (model.probe_status ?? provider.probe_status) !== 'succeeded'
      ) {
        return [];
      }
      return [
        {
          key: `${provider.provider_id}::${modelId}`,
          providerId: provider.provider_id,
          providerName: provider.display_name,
          modelId,
          modelName:
            model.display_name ?? catalogModelNames.get(modelId) ?? modelId,
        },
      ];
    });
    return choices.length === 0
      ? []
      : [
          {
            providerId: provider.provider_id,
            providerName: provider.display_name,
            choices,
          },
        ];
  });
}

interface AdvancedOptionsProps {
  options: DocumentTranslationOptions | null;
  onChange: (options: DocumentTranslationOptions) => void;
}

/** 直接铺开当前 Office runtime 的文件选项，让用户无需猜测隐藏入口。 */
function AdvancedOptions({ options, onChange }: AdvancedOptionsProps) {
  if (options === null) return null;

  return (
    <section className="file-options" aria-labelledby="file-options-title">
      <header className="file-options-heading">
        <span>
          <Settings2 aria-hidden="true" size={13} />
          <strong id="file-options-title">文件选项</strong>
        </span>
        <small>{options.kind.toUpperCase()}</small>
      </header>
      <div className="file-options-controls">
        <label className="advanced-option-row">
          <span className="advanced-option-copy">
            <strong>翻译表格</strong>
            <small>同时翻译表格内的文字内容</small>
          </span>
          <Switch.Root
            className="option-switch"
            checked={options.translate_tables}
            aria-label="翻译表格"
            onCheckedChange={(checked) =>
              onChange({ ...options, translate_tables: checked })
            }
          >
            <Switch.Thumb />
          </Switch.Root>
        </label>

        {options.kind === 'pptx' ? (
          <label className="advanced-option-row">
            <span className="advanced-option-copy">
              <strong>翻译演讲者备注</strong>
              <small>包含每页幻灯片下方的备注</small>
            </span>
            <Switch.Root
              className="option-switch"
              checked={options.translate_notes}
              aria-label="翻译演讲者备注"
              onCheckedChange={(checked) =>
                onChange({ ...options, translate_notes: checked })
              }
            >
              <Switch.Thumb />
            </Switch.Root>
          </label>
        ) : null}
      </div>
    </section>
  );
}

/** 主翻译页，只展示本次打开应用后创建的任务。 */
export function TranslationWorkspace({
  active,
  catalog,
  providers,
  jobs,
  onOpenModelSettings,
  onStart,
}: TranslationWorkspaceProps) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [document, setDocument] = useState<PendingDocument | null>(null);
  const [documentOptions, setDocumentOptions] =
    useState<DocumentTranslationOptions | null>(null);
  const [sourceLanguage, setSourceLanguage] = useState('auto');
  const [targetLanguage, setTargetLanguage] = useState('zh-CN');
  const [selectedModelKey, setSelectedModelKey] = useState('');
  const [isDragging, setIsDragging] = useState(false);
  const [isStarting, setIsStarting] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    if (!active || !isTauriRuntime()) return;
    let disposed = false;
    let unlisten: (() => void) | undefined;

    /** 使用 Tauri 原生 file drop 事件，避免 Windows WebView 吞掉 HTML5 drop。 */
    async function subscribeNativeFileDrop() {
      try {
        const { getCurrentWebview } = await import('@tauri-apps/api/webview');
        const nextUnlisten = await getCurrentWebview().onDragDropEvent(
          (event) => {
            if (
              event.payload.type === 'enter' ||
              event.payload.type === 'over'
            ) {
              setIsDragging(true);
              return;
            }
            setIsDragging(false);
            if (event.payload.type !== 'drop') return;
            const path = event.payload.paths[0];
            if (!path) return;
            const name = nameFromPath(path);
            const kind = documentKindOf(name);
            if (kind === null) {
              setNotice('仅支持 DOCX、PPTX、TXT 与 Markdown。');
              return;
            }
            setDocument({ source: 'path', path, name, kind });
            setDocumentOptions(defaultDocumentOptions(kind));
            setNotice(null);
          },
        );
        if (disposed) nextUnlisten();
        else unlisten = nextUnlisten;
      } catch {
        if (!disposed) {
          setNotice('原生拖放暂时不可用，请点击选择文件。');
        }
      }
    }

    void subscribeNativeFileDrop();
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, [active]);

  const choiceGroups = modelChoiceGroups(catalog, providers);
  const choices = choiceGroups.flatMap((group) => group.choices);
  const selectedModel =
    choices.find((choice) => choice.key === selectedModelKey) ?? choices[0];
  const modelGroups: readonly CompactSelectGroup[] = choiceGroups.map(
    (group) => ({
      id: group.providerId,
      label: group.providerName,
      icon: (
        <ProviderIcon
          providerId={group.providerId}
          displayName={group.providerName}
          size={20}
        />
      ),
      options: group.choices.map((choice) => ({
        value: choice.key,
        label: choice.modelName,
      })),
    }),
  );

  /** 接住浏览器 File，校验格式并为它初始化对应的安全选项。 */
  function selectBrowserFile(file: File | undefined) {
    if (file === undefined) return;
    const kind = documentKindOf(file.name);
    if (kind === null) {
      setNotice('仅支持 DOCX、PPTX、TXT 与 Markdown。');
      return;
    }
    setDocument({ source: 'file', file, name: file.name, kind });
    setDocumentOptions(defaultDocumentOptions(kind));
    setNotice(null);
  }

  /** 在 Tauri 中使用原生 dialog，普通浏览器则触发隐藏 file input。 */
  async function chooseDocument() {
    if (!isTauriRuntime()) {
      fileInputRef.current?.click();
      return;
    }
    const { open } = await import('@tauri-apps/plugin-dialog');
    const selected = await open({
      multiple: false,
      directory: false,
      filters: [
        {
          name: 'PageFerry 文档',
          extensions: ['docx', 'pptx', 'txt', 'md'],
        },
      ],
    });
    if (typeof selected !== 'string') return;
    const name = nameFromPath(selected);
    const kind = documentKindOf(name);
    if (kind === null) return;
    setDocument({ source: 'path', path: selected, name, kind });
    setDocumentOptions(defaultDocumentOptions(kind));
    setNotice(null);
  }

  /** 提交当前单文件任务，并只在成功创建后清空 composer。 */
  async function startTranslation() {
    if (document === null || selectedModel === undefined) return;
    setIsStarting(true);
    setNotice(null);
    try {
      await onStart({
        document,
        sourceLanguage: sourceLanguage === 'auto' ? null : sourceLanguage,
        targetLanguage,
        providerId: selectedModel.providerId,
        modelId: selectedModel.modelId,
        options: documentOptions,
      });
      setDocument(null);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '任务创建失败。');
    } finally {
      setIsStarting(false);
    }
  }

  /** 在离开 drop zone 本身时收起 drag 高亮，忽略子元素之间的移动。 */
  function leaveDropZone(event: React.DragEvent<HTMLDivElement>) {
    const nextTarget = event.relatedTarget;
    if (nextTarget instanceof Node && event.currentTarget.contains(nextTarget))
      return;
    setIsDragging(false);
  }

  /** 只有源语言明确时才交换，避免把自动识别写进目标语言。 */
  function swapLanguages() {
    if (sourceLanguage === 'auto') return;
    setSourceLanguage(targetLanguage);
    setTargetLanguage(sourceLanguage);
  }

  /** 移除当前文件并清理与它绑定的高级选项展示状态。 */
  function removeDocument() {
    setDocument(null);
    setNotice(null);
  }

  return (
    <section
      className="page translation-page"
      aria-labelledby="translation-title"
    >
      <header className="page-heading page-heading--compact">
        <div>
          <h1 id="translation-title">文件翻译</h1>
          <p>选择文件和目标语言，PageFerry 会保留原格式输出新文件</p>
        </div>
      </header>

      <div className="translation-toolbar">
        <div className="language-route" aria-label="翻译语言">
          <div className="toolbar-control">
            <span>源语言</span>
            <CompactSelect
              ariaLabel="源语言"
              value={sourceLanguage}
              options={sourceLanguageOptions}
              onValueChange={setSourceLanguage}
            />
          </div>

          <button
            className="swap-language-button"
            type="button"
            aria-label="交换源语言和目标语言"
            disabled={sourceLanguage === 'auto'}
            onClick={swapLanguages}
          >
            <ArrowLeftRight aria-hidden="true" size={16} />
          </button>

          <div className="toolbar-control">
            <span>目标语言</span>
            <CompactSelect
              ariaLabel="目标语言"
              value={targetLanguage}
              options={languageOptions}
              onValueChange={setTargetLanguage}
            />
          </div>
        </div>

        <div className="toolbar-control model-toolbar-control">
          <span>翻译模型</span>
          {selectedModel === undefined ? (
            <button
              className="model-required"
              type="button"
              onClick={onOpenModelSettings}
            >
              <Settings2 aria-hidden="true" size={15} />
              配置模型
            </button>
          ) : (
            <CompactSelect
              ariaLabel="翻译模型"
              className="model-select-trigger"
              value={selectedModel.key}
              groups={modelGroups}
              leadingIcon={
                <ProviderIcon
                  providerId={selectedModel.providerId}
                  displayName={selectedModel.providerName}
                  size={20}
                />
              }
              onValueChange={setSelectedModelKey}
            />
          )}
        </div>
      </div>

      <input
        ref={fileInputRef}
        className="visually-hidden"
        type="file"
        tabIndex={-1}
        aria-hidden="true"
        accept=".docx,.pptx,.txt,.md"
        onChange={(event) => {
          selectBrowserFile(event.target.files?.[0]);
          // 清空 DOM value，移除任务后仍可再次选择同一个文件。
          event.currentTarget.value = '';
        }}
      />

      {document === null ? (
        <div
          className={`file-dropzone ${isDragging ? 'file-dropzone--dragging' : ''}`}
          onDragEnter={(event) => {
            event.preventDefault();
            setIsDragging(true);
          }}
          onDragOver={(event) => event.preventDefault()}
          onDragLeave={leaveDropZone}
          onDrop={(event) => {
            event.preventDefault();
            setIsDragging(false);
            selectBrowserFile(event.dataTransfer.files[0]);
          }}
        >
          <button type="button" onClick={chooseDocument}>
            <span className="dropzone-icon" aria-hidden="true">
              <CloudUpload size={29} strokeWidth={1.7} />
            </span>
            <strong>拖放文件到这里</strong>
            <span>或点击选择文件</span>
            <small>DOCX · PPTX · TXT · MD</small>
          </button>
        </div>
      ) : (
        <div className="new-task-composer file-row-enter">
          <div className="selected-file-row">
            <span className="file-type">
              <DocumentTypeIcon kind={document.kind} />
            </span>
            <div className="file-primary">
              <strong>{document.name}</strong>
              <span>
                {document.source === 'file'
                  ? formatBytes(document.file.size)
                  : `本地文件 · ${document.kind.toUpperCase()}`}
              </span>
            </div>
            <span className="file-ready">准备就绪</span>
            <button
              className="icon-button"
              type="button"
              aria-label="移除文件"
              onClick={removeDocument}
            >
              <X aria-hidden="true" size={17} />
            </button>
          </div>

          <AdvancedOptions
            options={documentOptions}
            onChange={setDocumentOptions}
          />

          <footer className="new-task-footer">
            <button
              className="start-button"
              type="button"
              disabled={selectedModel === undefined || isStarting}
              onClick={() => void startTranslation()}
            >
              {isStarting ? (
                <LoaderCircle className="spin" aria-hidden="true" size={16} />
              ) : (
                <Play aria-hidden="true" size={15} fill="currentColor" />
              )}
              {isStarting ? '正在创建' : '开始翻译'}
            </button>
          </footer>
        </div>
      )}

      {notice ? (
        <p className="page-notice" role="alert">
          <AlertCircle aria-hidden="true" size={15} />
          {notice}
        </p>
      ) : null}

      {jobs.length > 0 ? (
        <section className="session-jobs" aria-labelledby="session-jobs-title">
          <div className="section-line-heading">
            <h2 id="session-jobs-title">本次任务</h2>
            <span>{jobs.length} 个任务</span>
          </div>
          <JobList jobs={jobs} catalog={catalog} onError={setNotice} />
        </section>
      ) : null}
    </section>
  );
}
