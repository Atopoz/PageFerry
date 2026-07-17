/** 维护翻译语种 code、本族语名称与 locale-aware UI label。 */

import type { CompactSelectOption } from '@/components/ui/compact-select';
import type { MessageKey } from '@/i18n/messages';
import type { Translate } from '@/i18n/i18n';

interface TranslationLanguageDefinition {
  value: string;
  messageKey: MessageKey;
  nativeLabel: string;
}

const translationLanguages: readonly TranslationLanguageDefinition[] = [
  {
    value: 'zh-CN',
    messageKey: 'language.zh-CN',
    nativeLabel: '简体中文',
  },
  {
    value: 'zh-TW',
    messageKey: 'language.zh-TW',
    nativeLabel: '繁體中文（台灣）',
  },
  {
    value: 'zh-HK',
    messageKey: 'language.zh-HK',
    nativeLabel: '繁體中文（香港）',
  },
  { value: 'en', messageKey: 'language.en', nativeLabel: 'English' },
  { value: 'ja', messageKey: 'language.ja', nativeLabel: '日本語' },
  { value: 'ko', messageKey: 'language.ko', nativeLabel: '한국어' },
  { value: 'fr', messageKey: 'language.fr', nativeLabel: 'Français' },
  { value: 'de', messageKey: 'language.de', nativeLabel: 'Deutsch' },
  { value: 'es', messageKey: 'language.es', nativeLabel: 'Español' },
  { value: 'ru', messageKey: 'language.ru', nativeLabel: 'Русский' },
];

/** 为翻译 selector 生成“当前 UI 语言为主、本族语名称为辅”的选项。 */
export function translationLanguageOptions(
  t: Translate,
): readonly CompactSelectOption[] {
  return translationLanguages.map((language) => {
    const label = t(language.messageKey);
    return {
      value: language.value,
      label,
      description:
        label === language.nativeLabel ? undefined : language.nativeLabel,
    };
  });
}

/** 返回历史记录等紧凑界面的 locale-aware 语种名称。 */
export function translationLanguageLabel(
  value: string | null,
  t: Translate,
): string {
  if (value === null || value === 'auto') return t('language.auto');
  const definition = translationLanguages.find(
    (language) => language.value === value,
  );
  return definition ? t(definition.messageKey) : value;
}
