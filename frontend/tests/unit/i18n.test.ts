/** 验证系统 locale 解析顺序与不支持语言的稳定 fallback。 */

import { describe, expect, it } from 'vitest';

import { resolveSystemLocale } from '../../src/i18n/i18n';

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
