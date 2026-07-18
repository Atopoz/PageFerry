/** 持有 renderer 唯一的 UI locale，并向组件树提供稳定 context。 */

import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';

import {
  I18nContext,
  type I18nContextValue,
  type LocalePreference,
  localeStorageKey,
  readLocalePreference,
  resolveSystemLocale,
  resolveUiLocale,
  translator,
} from '@/i18n/i18n';

interface I18nProviderProps {
  children: ReactNode;
}

/** 系统语言变化无需重启即可生效，手动选择则保持当前用户偏好。 */
export function I18nProvider({ children }: I18nProviderProps) {
  const [preference, setPreferenceState] =
    useState<LocalePreference>(readLocalePreference);
  const [systemLocale, setSystemLocale] = useState(resolveSystemLocale);
  const locale = resolveUiLocale(preference, systemLocale);

  useEffect(() => {
    /** 仅在系统偏好改变时重新解析，手动选择的 locale 不受影响。 */
    function handleSystemLanguageChange() {
      setSystemLocale(resolveSystemLocale());
    }

    window.addEventListener('languagechange', handleSystemLanguageChange);
    return () =>
      window.removeEventListener('languagechange', handleSystemLanguageChange);
  }, []);

  useEffect(() => {
    document.documentElement.lang = locale;
  }, [locale]);

  const setPreference = useCallback((next: LocalePreference) => {
    setPreferenceState(next);
    try {
      if (next === 'system') window.localStorage.removeItem(localeStorageKey);
      else window.localStorage.setItem(localeStorageKey, next);
    } catch {
      // storage 不可用不影响当前 session 的即时切换。
    }
  }, []);

  const value = useMemo<I18nContextValue>(
    () => ({
      locale,
      systemLocale,
      preference,
      setPreference,
      t: translator(locale),
    }),
    [locale, preference, setPreference, systemLocale],
  );

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}
