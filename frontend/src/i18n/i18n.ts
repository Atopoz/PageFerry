/** 提供轻量应用级 i18n 的类型、context 与系统语言解析。 */

import { createContext, useContext } from 'react';

import { enMessages, type MessageKey, zhCNMessages } from '@/i18n/messages';

export type UiLocale = 'zh-CN' | 'en';
export type LocalePreference = 'system' | UiLocale;
export type MessageValues = Record<string, string | number>;
export type Translate = (key: MessageKey, values?: MessageValues) => string;

export interface I18nContextValue {
  locale: UiLocale;
  systemLocale: UiLocale;
  preference: LocalePreference;
  setPreference: (preference: LocalePreference) => void;
  t: Translate;
}

export const localeStorageKey = 'pageferry.ui-locale.v1';

/** 将系统 locale 按声明顺序收敛到应用已经完整支持的界面语言。 */
export function resolveSystemLocale(
  languages: readonly string[] = navigator.languages,
): UiLocale {
  for (const language of languages) {
    const normalized = language.toLowerCase();
    if (normalized.startsWith('zh')) return 'zh-CN';
    if (normalized.startsWith('en')) return 'en';
  }
  return 'en';
}

/** 读取已保存偏好；未知旧值直接退回跟随系统，避免卡在半支持状态。 */
export function readLocalePreference(): LocalePreference {
  try {
    const stored = window.localStorage.getItem(localeStorageKey);
    if (stored === 'zh-CN' || stored === 'en') return stored;
  } catch {
    // 隐私模式或禁用 storage 时仍可在当前 session 内切换语言。
  }
  return 'system';
}

/** 用命名占位符替换动态值，避免组件自己拼接双语句子。 */
function interpolate(template: string, values: MessageValues = {}): string {
  return template.replace(/\{(\w+)\}/g, (match, key: string) =>
    Object.hasOwn(values, key) ? String(values[key]) : match,
  );
}

/** 创建指定 locale 的稳定翻译函数。 */
export function translator(locale: UiLocale): Translate {
  const messages = locale === 'zh-CN' ? zhCNMessages : enMessages;
  return (key, values) => interpolate(messages[key], values);
}

const defaultContext: I18nContextValue = {
  locale: 'zh-CN',
  systemLocale: 'zh-CN',
  preference: 'system',
  setPreference: () => undefined,
  t: translator('zh-CN'),
};

export const I18nContext = createContext<I18nContextValue>(defaultContext);

/** 读取当前应用 locale 与翻译函数。 */
export function useI18n(): I18nContextValue {
  return useContext(I18nContext);
}
