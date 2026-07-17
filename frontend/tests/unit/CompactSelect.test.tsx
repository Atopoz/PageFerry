/** 验证紧凑选择器会区分鼠标与键盘焦点。 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { CompactSelect } from '../../src/components/ui/compact-select';

const options = [
  { value: 'auto', label: '自动识别' },
  { value: 'en', label: 'English' },
] as const;

describe('CompactSelect', () => {
  it('鼠标完成选择后移走 trigger 焦点', async () => {
    const onValueChange = vi.fn();
    render(
      <CompactSelect
        ariaLabel="源语言"
        value="auto"
        options={options}
        onValueChange={onValueChange}
      />,
    );

    const trigger = screen.getByRole('combobox', { name: '源语言' });
    trigger.focus();
    fireEvent.pointerDown(trigger, {
      button: 0,
      buttons: 1,
      ctrlKey: false,
      pointerType: 'mouse',
    });
    const option = await screen.findByRole('option', { name: 'English' });
    fireEvent.click(option);

    await waitFor(() => expect(trigger).not.toHaveFocus());
    expect(onValueChange).toHaveBeenCalledWith('en');
  });

  it('键盘完成选择后保留 trigger 焦点', async () => {
    render(
      <CompactSelect
        ariaLabel="源语言"
        value="auto"
        options={options}
        onValueChange={() => undefined}
      />,
    );

    const trigger = screen.getByRole('combobox', { name: '源语言' });
    trigger.focus();
    fireEvent.keyDown(trigger, { key: 'ArrowDown' });
    const option = await screen.findByRole('option', { name: 'English' });
    fireEvent.keyDown(option, { key: 'Enter' });

    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it('鼠标打开后改用键盘选择时仍把焦点还给 trigger', async () => {
    render(
      <CompactSelect
        ariaLabel="源语言"
        value="auto"
        options={options}
        onValueChange={() => undefined}
      />,
    );

    const trigger = screen.getByRole('combobox', { name: '源语言' });
    trigger.focus();
    fireEvent.pointerDown(trigger, {
      button: 0,
      buttons: 1,
      ctrlKey: false,
      pointerType: 'mouse',
    });
    const option = await screen.findByRole('option', { name: 'English' });
    fireEvent.keyDown(option, { key: 'Enter' });

    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it('鼠标打开后按 Escape 取消时仍把焦点还给 trigger', async () => {
    render(
      <CompactSelect
        ariaLabel="源语言"
        value="auto"
        options={options}
        onValueChange={() => undefined}
      />,
    );

    const trigger = screen.getByRole('combobox', { name: '源语言' });
    trigger.focus();
    fireEvent.pointerDown(trigger, {
      button: 0,
      buttons: 1,
      ctrlKey: false,
      pointerType: 'mouse',
    });
    const option = await screen.findByRole('option', { name: 'English' });
    fireEvent.keyDown(option, { key: 'Escape' });

    await waitFor(() => expect(trigger).toHaveFocus());
  });
});
