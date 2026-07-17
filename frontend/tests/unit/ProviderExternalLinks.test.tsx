/** 验证 provider 的密钥与文档入口都走受限的系统外链能力。 */

import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ProviderPage } from '../../src/features/providers/ProviderPage';
import type { ModelCatalog, ProviderStatus } from '../../src/lib/api';

const providerCatalog: ModelCatalog = {
  schema_version: 1,
  catalog_version: 'test',
  providers: [
    {
      id: 'deepseek',
      display_name: 'DeepSeek',
      protocol: 'openai',
      available: true,
      base_url_editable: true,
      supports_model_sync: true,
      default_base_url: 'https://api.deepseek.com',
      docs_url: 'https://api-docs.deepseek.com/',
      api_key_url: 'https://platform.deepseek.com/api_keys',
    },
  ],
  models: [],
  provider_models: [],
};

/** 渲染只包含外链所需 catalog 的 provider 页面。 */
function renderProviderPage(catalog: ModelCatalog = providerCatalog): void {
  render(
    <ProviderPage
      catalog={catalog}
      providers={[]}
      onCreate={async () => {
        throw new Error('本测试不创建 provider。');
      }}
      onSave={async () => ({}) as ProviderStatus}
      onDelete={async () => undefined}
    />,
  );
}

describe('Provider external links', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('获取密钥和 API 文档均交给浏览器 fallback', () => {
    const open = vi.spyOn(window, 'open').mockReturnValue({} as Window);
    renderProviderPage();

    fireEvent.click(screen.getByRole('link', { name: '获取密钥' }));
    fireEvent.click(screen.getByRole('link', { name: 'API 文档' }));

    expect(open).toHaveBeenNthCalledWith(
      1,
      'https://platform.deepseek.com/api_keys',
      '_blank',
      'noopener,noreferrer',
    );
    expect(open).toHaveBeenNthCalledWith(
      2,
      'https://api-docs.deepseek.com/',
      '_blank',
      'noopener,noreferrer',
    );
  });

  it('非法 scheme 不会渲染成可点击入口', () => {
    renderProviderPage({
      ...providerCatalog,
      providers: [
        {
          ...providerCatalog.providers[0],
          docs_url: 'file:///tmp/docs',
          api_key_url: 'javascript:alert(1)',
        },
      ],
    });

    expect(screen.queryByRole('link', { name: '获取密钥' })).toBeNull();
    expect(screen.queryByRole('link', { name: 'API 文档' })).toBeNull();
  });
});
