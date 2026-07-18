/** 验证系统 locale 解析顺序与不支持语言的稳定 fallback。 */

import { describe, expect, it } from 'vitest';

import {
  localeStorageKey,
  resolveInitialUiLocale,
  resolveSystemLocale,
  resolveUiLocale,
} from '../../src/i18n/i18n';

describe('resolveSystemLocale', () => {
  it('按 navigator.languages 的优先顺序选择第一个受支持语言', () => {
    expect(resolveSystemLocale(['en-US', 'zh-CN'])).toBe('en');
    expect(resolveSystemLocale(['zh-HK', 'en-US'])).toBe('zh-CN');
    expect(resolveSystemLocale(['fr-FR', 'zh-CN', 'en-US'])).toBe('zh-CN');
  });

  it('没有匹配语言时退回 English', () => {
    expect(resolveSystemLocale(['fr-FR', 'de-DE'])).toBe('en');
    expect(resolveSystemLocale([])).toBe('en');
  });
});

describe('resolveUiLocale', () => {
  it('手动选择的界面语言优先于系统语言', () => {
    expect(resolveUiLocale('zh-CN', 'en')).toBe('zh-CN');
    expect(resolveUiLocale('en', 'zh-CN')).toBe('en');
  });

  it('跟随系统时使用已经解析过的系统语言', () => {
    expect(resolveUiLocale('system', 'zh-CN')).toBe('zh-CN');
    expect(resolveUiLocale('system', 'en')).toBe('en');
  });
});

describe('resolveInitialUiLocale', () => {
  it('启动阶段优先使用 localStorage 中保存的界面语言', () => {
    window.localStorage.setItem(localeStorageKey, 'zh-CN');
    expect(resolveInitialUiLocale(['en-US'])).toBe('zh-CN');

    window.localStorage.setItem(localeStorageKey, 'en');
    expect(resolveInitialUiLocale(['zh-CN'])).toBe('en');
  });

  it('未保存偏好或保存值无效时跟随系统语言', () => {
    expect(resolveInitialUiLocale(['zh-HK'])).toBe('zh-CN');

    window.localStorage.setItem(localeStorageKey, 'unsupported');
    expect(resolveInitialUiLocale(['en-US'])).toBe('en');
  });
});
