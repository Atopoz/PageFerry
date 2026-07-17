/** 提供不属于翻译任务或模型服务的应用级偏好设置。 */

import { Info, Languages } from 'lucide-react';

import {
  CompactSelect,
  type CompactSelectOption,
} from '@/components/ui/compact-select';
import { type LocalePreference, type UiLocale, useI18n } from '@/i18n/i18n';
import { APP_VERSION } from '@/lib/app-metadata';

/** 渲染通用设置；应用语言即时生效且不重置工作区状态。 */
export function SettingsPage() {
  const { preference, setPreference, systemLocale, t } = useI18n();
  const localeName = {
    'zh-CN': t('settings.language.zhCN'),
    en: t('settings.language.en'),
  } satisfies Record<UiLocale, string>;
  const languageOptions: readonly CompactSelectOption[] = [
    {
      value: 'system',
      label: t('settings.language.system'),
      description: t('settings.language.systemResolved', {
        language: localeName[systemLocale],
      }),
    },
    { value: 'zh-CN', label: '简体中文' },
    { value: 'en', label: 'English' },
  ];

  return (
    <section className="page settings-page" aria-labelledby="settings-title">
      <header className="page-heading page-heading--compact">
        <div>
          <h1 id="settings-title">{t('settings.title')}</h1>
          <p>{t('settings.description')}</p>
        </div>
      </header>

      <section
        className="settings-section"
        aria-labelledby="general-settings-title"
      >
        <h2 id="general-settings-title">{t('settings.general')}</h2>
        <div className="settings-list">
          <div className="settings-row">
            <span className="settings-row-icon" aria-hidden="true">
              <Languages size={18} strokeWidth={1.7} />
            </span>
            <span className="settings-row-copy">
              <strong>{t('settings.language.title')}</strong>
              <small>{t('settings.language.description')}</small>
            </span>
            <CompactSelect
              ariaLabel={t('settings.language.aria')}
              className="settings-language-select"
              value={preference}
              options={languageOptions}
              onValueChange={(value) =>
                setPreference(value as LocalePreference)
              }
            />
          </div>
        </div>
      </section>

      <section
        className="settings-section"
        aria-labelledby="about-settings-title"
      >
        <h2 id="about-settings-title">{t('settings.about')}</h2>
        <div className="settings-list">
          <div className="settings-row">
            <span className="settings-row-icon" aria-hidden="true">
              <Info size={18} strokeWidth={1.7} />
            </span>
            <span className="settings-row-copy">
              <strong>PageFerry</strong>
              <small>{t('settings.about.description')}</small>
            </span>
            <span className="settings-version">
              {t('settings.about.version', { version: APP_VERSION })}
            </span>
          </div>
        </div>
      </section>
    </section>
  );
}
