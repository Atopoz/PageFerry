import { useEffect, useId, useState } from 'react';

import { Button } from '@/components/ui/button';

import logoUrl from './assets/logo.svg';
import { getHealth, getModelCatalog, type ProviderDefinition } from './lib/api';
import './App.css';

type ServiceState = 'checking' | 'connected' | 'offline';

const supportedExtensions = new Set(['docx', 'pptx', 'pdf']);

function extensionOf(fileName: string): string {
  return fileName.split('.').at(-1)?.toLowerCase() ?? '';
}

export function App() {
  const fileInputId = useId();
  const [serviceState, setServiceState] = useState<ServiceState>('checking');
  const [serviceVersion, setServiceVersion] = useState('');
  const [providers, setProviders] = useState<ProviderDefinition[]>([]);
  const [selectedProvider, setSelectedProvider] = useState('');
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [notice, setNotice] = useState(
    '文档只在本机处理，不会上传到 PageFerry 服务器。',
  );

  useEffect(() => {
    const controller = new AbortController();

    Promise.all([
      getHealth(controller.signal),
      getModelCatalog(controller.signal),
    ])
      .then(([health, catalog]) => {
        setServiceState('connected');
        setServiceVersion(health.data.version);
        setProviders(catalog.providers);
        setSelectedProvider(
          (current) => current || catalog.providers.at(0)?.id || '',
        );
      })
      .catch(() => {
        if (!controller.signal.aborted) {
          setServiceState('offline');
        }
      });

    return () => controller.abort();
  }, []);

  function selectFile(file: File | undefined) {
    if (file === undefined) {
      return;
    }
    if (!supportedExtensions.has(extensionOf(file.name))) {
      setSelectedFile(null);
      setNotice('暂时只接受 DOCX、PPTX 和文本型 PDF。');
      return;
    }
    setSelectedFile(file);
    setNotice('文件已就位。当前骨架尚未接入翻译 pipeline，不会改写原文件。');
  }

  const serviceLabel = {
    checking: '正在连接本地服务',
    connected: `本地服务已连接${serviceVersion ? ` · v${serviceVersion}` : ''}`,
    offline: '本地服务未启动',
  }[serviceState];

  return (
    <div className="app-frame">
      <header className="topbar">
        <div className="brand" aria-label="PageFerry">
          <img className="brand-mark" src={logoUrl} alt="" aria-hidden="true" />
          <span>PageFerry</span>
        </div>
        <div
          className={`service-state service-state--${serviceState}`}
          role="status"
        >
          <span className="service-dot" aria-hidden="true" />
          {serviceLabel}
        </div>
      </header>

      <main className="main-content">
        <section className="intro" aria-labelledby="page-title">
          <p className="eyebrow">LOCAL DOCUMENT TRANSLATION</p>
          <h1 id="page-title">把版式留在原处，只让语言过河。</h1>
          <p>
            面向个人的本地文档翻译工具。首版聚焦 DOCX、PPTX 与文本型
            PDF，文件进，文件出。
          </p>
        </section>

        <section className="translation-workspace" aria-label="新建翻译任务">
          <div className="document-column">
            <div className="section-heading">
              <div>
                <p className="step-label">01 / 文档</p>
                <h2>选择要翻译的文件</h2>
              </div>
              <span>最大体积将在 pipeline 接入时确定</span>
            </div>

            <label
              className={`drop-zone ${isDragging ? 'drop-zone--active' : ''}`}
              htmlFor={fileInputId}
              onDragEnter={(event) => {
                event.preventDefault();
                setIsDragging(true);
              }}
              onDragOver={(event) => event.preventDefault()}
              onDragLeave={() => setIsDragging(false)}
              onDrop={(event) => {
                event.preventDefault();
                setIsDragging(false);
                selectFile(event.dataTransfer.files[0]);
              }}
            >
              <input
                id={fileInputId}
                type="file"
                accept=".docx,.pptx,.pdf"
                onChange={(event) => selectFile(event.target.files?.[0])}
              />
              <span className="file-symbol" aria-hidden="true" />
              {selectedFile === null ? (
                <>
                  <strong>拖入文件，或点击浏览</strong>
                  <span>DOCX · PPTX · TEXT PDF</span>
                </>
              ) : (
                <>
                  <strong>{selectedFile.name}</strong>
                  <span>
                    {(selectedFile.size / 1024 / 1024).toFixed(2)} MB ·
                    点击可重新选择
                  </span>
                </>
              )}
            </label>

            <p className="notice" role="status">
              {notice}
            </p>
          </div>

          <aside className="configuration-column" aria-label="翻译设置">
            <div className="section-heading section-heading--compact">
              <div>
                <p className="step-label">02 / 设置</p>
                <h2>翻译配置</h2>
              </div>
            </div>

            <label className="field">
              <span>目标语言</span>
              <select defaultValue="zh-CN">
                <option value="zh-CN">简体中文</option>
                <option value="en">English</option>
                <option value="ja">日本語</option>
              </select>
            </label>

            <label className="field">
              <span>模型服务</span>
              <select
                value={selectedProvider}
                onChange={(event) => setSelectedProvider(event.target.value)}
                disabled={providers.length === 0}
              >
                {providers.length === 0 ? (
                  <option value="">等待本地目录</option>
                ) : null}
                {providers.map((provider) => (
                  <option key={provider.id} value={provider.id}>
                    {provider.display_name}
                  </option>
                ))}
              </select>
            </label>

            <label className="field">
              <span>模型</span>
              <select disabled defaultValue="pending">
                <option value="pending">待核验后随版本发布</option>
              </select>
            </label>

            <dl className="task-facts">
              <div>
                <dt>输出</dt>
                <dd>软件专属数据目录</dd>
              </div>
              <div>
                <dt>预览</dt>
                <dd>首版关闭</dd>
              </div>
            </dl>

            <Button
              type="button"
              className="min-h-11 w-full rounded-[7px] text-[13px] font-semibold disabled:pointer-events-auto disabled:cursor-not-allowed disabled:bg-[#dfe3df] disabled:text-[#758079] disabled:opacity-100"
              disabled
            >
              核心 pipeline 接入后启用
            </Button>
          </aside>
        </section>
      </main>
    </div>
  );
}
