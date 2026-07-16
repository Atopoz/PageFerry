import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { App } from '../../src/App';

describe('App', () => {
  beforeEach(() => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith('/healthz')) {
        return {
          ok: true,
          json: async () => ({
            code: 'success',
            data: { service: 'pageferry-api', version: '0.1.0' },
          }),
        } as Response;
      }
      return {
        ok: true,
        json: async () => ({
          schema_version: 1,
          catalog_version: '0.1.0-dev',
          providers: [
            { id: 'openai', display_name: 'OpenAI', protocol: 'openai' },
            {
              id: 'custom_openai',
              display_name: 'OpenAI-compatible',
              protocol: 'custom',
            },
          ],
        }),
      } as Response;
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('loads the local service and bundled provider catalog in parallel', async () => {
    render(<App />);

    expect(
      await screen.findByText('本地服务已连接 · v0.1.0'),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('option', { name: 'OpenAI-compatible' }),
    ).toBeInTheDocument();
    expect(globalThis.fetch).toHaveBeenCalledTimes(2);
  });

  it('accepts a supported local document without pretending to translate it', async () => {
    render(<App />);
    const input = document.querySelector('input[type="file"]');
    const file = new File(['content'], 'sample.docx', {
      type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    });

    expect(input).not.toBeNull();
    fireEvent.change(input as HTMLInputElement, { target: { files: [file] } });

    expect(screen.getByText('sample.docx')).toBeInTheDocument();
    expect(screen.getByText(/尚未接入翻译 pipeline/)).toBeInTheDocument();
  });
});
