/** 提供独立的翻译历史页与本地筛选能力。 */

import { AlertCircle, Search } from 'lucide-react';
import { useState } from 'react';

import {
  CompactSelect,
  type CompactSelectOption,
} from '@/components/ui/compact-select';
import { JobList } from '@/features/jobs/JobList';
import { useI18n } from '@/i18n/i18n';
import type { JobStatus, ModelCatalog, TranslationJob } from '@/lib/api';

interface HistoryPageProps {
  catalog: ModelCatalog | null;
  jobs: TranslationJob[];
}

/** 渲染持久化历史，且不把这些任务混入文件翻译工作台。 */
export function HistoryPage({ catalog, jobs }: HistoryPageProps) {
  const { t } = useI18n();
  const [query, setQuery] = useState('');
  const [statusFilter, setStatusFilter] = useState<'all' | JobStatus>('all');
  const [notice, setNotice] = useState<string | null>(null);
  const statusOptions: readonly CompactSelectOption[] = [
    { value: 'all', label: t('history.filter.all') },
    { value: 'running', label: t('history.filter.running') },
    { value: 'succeeded', label: t('history.filter.succeeded') },
    { value: 'failed', label: t('history.filter.failed') },
    { value: 'cancelled', label: t('history.filter.cancelled') },
  ];
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
          <h1 id="history-title">{t('history.title')}</h1>
          <p>{t('history.description')}</p>
        </div>
        <span className="page-count">
          {t('history.jobCount', { count: jobs.length })}
        </span>
      </header>

      <div className="history-toolbar">
        <label className="history-search">
          <Search aria-hidden="true" size={16} />
          <span className="visually-hidden">{t('history.searchLabel')}</span>
          <input
            type="search"
            value={query}
            placeholder={t('history.searchPlaceholder')}
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
        <CompactSelect
          ariaLabel={t('history.filterAria')}
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
        emptyMessage={
          jobs.length === 0 ? t('history.empty') : t('history.noMatch')
        }
        showCreatedAt
        onError={setNotice}
      />
    </section>
  );
}
