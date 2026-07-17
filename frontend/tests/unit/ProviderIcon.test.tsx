/** 验证 provider 品牌图标映射和需要保留的光学校准。 */

import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import '../../src/App.css';
import mimoSvg from '../../src/assets/provider-logos/mimo.svg?raw';
import { ProviderIcon } from '../../src/features/providers/ProviderIcon';

describe('ProviderIcon', () => {
  it.each([
    ['kimi', 'provider-icon-avatar--kimi'],
    ['moonshot', 'provider-icon-avatar--kimi'],
    ['mimo', 'provider-icon-avatar--mimo'],
    ['xiaomi-mimo', 'provider-icon-avatar--mimo'],
  ])('%s 使用对应的品牌图标', (providerId, className) => {
    const { container } = render(
      <ProviderIcon
        providerId={providerId}
        displayName={providerId}
        size={28}
      />,
    );
    const avatar = container.querySelector('.provider-icon-avatar');

    expect(avatar).toHaveClass(className);
    expect(avatar).toHaveStyle({ width: '28px', height: '28px' });
    expect(avatar?.querySelector('img')).not.toBeNull();
    expect(container.querySelector('.provider-icon-fallback')).toBeNull();
  });

  it('Kimi 只缩小内部深色 tile，不改变统一占位', () => {
    const { container } = render(
      <ProviderIcon providerId="kimi" displayName="Kimi" size={28} />,
    );
    const avatar = container.querySelector<HTMLElement>(
      '.provider-icon-avatar--kimi',
    );
    const image = avatar?.querySelector<HTMLImageElement>('img');

    expect(avatar).not.toBeNull();
    expect(image).not.toBeNull();
    expect(getComputedStyle(avatar!).backgroundColor).toBe(
      'rgb(255, 255, 255)',
    );
    expect(getComputedStyle(image!).width).toBe('100%');
    expect(getComputedStyle(image!).padding).toBe('9%');
    expect(getComputedStyle(image!).backgroundColor).toBe('rgb(17, 24, 39)');
  });

  it('MiMo 的 M 由分离轮廓组成，保留官方断口', () => {
    const svg = new DOMParser().parseFromString(mimoSvg, 'image/svg+xml');

    expect(svg.querySelectorAll('g > path')).toHaveLength(2);
    expect(svg.querySelector('[stroke]')).toBeNull();
  });
});
