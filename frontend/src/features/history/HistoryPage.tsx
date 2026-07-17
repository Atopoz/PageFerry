/** 提供独立的翻译历史页与本地筛选能力。 */

import { AlertCircle, Search } from 'lucide-react';
import { useState } from 'react';

import {
  CompactSelect,
  type CompactSelectOption,
} from '@/components/ui/compact-select';
import { JobList } from '@/features/jobs/JobList';
import type { JobStatus, ModelCatalog, TranslationJob } from '@/lib/api';

interface HistoryPageProps {
  catalog: ModelCatalog | null;
  jobs: TranslationJob[];
}

const statusOptions: readonly CompactSelectOption[] = [
  { value: 'all', label: '全部状态' },
  { value: 'running', label: '进行中' },
  { value: 'succeeded', label: '已完成' },
  { value: 'failed', label: '失败' },
  { value: 'cancelled', label: '已取消' },
];

/** 渲染持久化历史，且不把这些任务混入文件翻译工作台。 */
export function HistoryPage({ catalog, jobs }: HistoryPageProps) {
  const [query, setQuery] = useState('');
  const [statusFilter, setStatusFilter] = useState<'all' | JobStatus>('all');
  const [notice, setNotice] = useState<string | null>(null);
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const visibleJobs = jobs.filter((job) => {
    const matchesStatus =
      statusFilter === 'all' ||
      job.status === statusFilter ||
      (statusFilter === 'running' && job.status === 'queued');
    const matchesQuery =
      normalizedQuery.length === 0 ||
      job.source_name.toLocaleLowerCase().includes(normalizedQuery) ||
      job.model_id.toLocaleLowerCase().includes(normalizedQuery);
    return matchesStatus && matchesQuery;
  });

  return (
    <section className="page history-page" aria-labelledby="history-title">
      <header className="page-heading page-heading--compact">
        <div>
          <h1 id="history-title">历史记录</h1>
          <p>查看已创建任务、处理状态与输出文件</p>
        </div>
        <span className="page-count">{jobs.length} 个任务</span>
      </header>

      <div className="history-toolbar">
        <label className="history-search">
          <Search aria-hidden="true" size={16} />
          <span className="visually-hidden">搜索历史任务</span>
          <input
            type="search"
            value={query}
            placeholder="搜索文件或模型"
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
        <CompactSelect
          ariaLabel="筛选任务状态"
          value={statusFilter}
          options={statusOptions}
          onValueChange={(value) => setStatusFilter(value as 'all' | JobStatus)}
        />
      </div>

      {notice ? (
        <p className="page-notice" role="alert">
          <AlertCircle aria-hidden="true" size={15} />
          {notice}
        </p>
      ) : null}

      <JobList
        jobs={visibleJobs}
        catalog={catalog}
        emptyMessage={jobs.length === 0 ? '还没有翻译记录' : '没有匹配的任务'}
        showCreatedAt
        onError={setNotice}
      />
    </section>
  );
}
