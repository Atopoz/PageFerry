/** 封装与 PageFerry 紧凑工具栏一致的 Radix Select。 */

import { Check, ChevronDown, ChevronUp } from 'lucide-react';
import { Select } from 'radix-ui';
import { useRef, type KeyboardEvent, type ReactNode } from 'react';

export interface CompactSelectOption {
  value: string;
  label: string;
  icon?: ReactNode;
  description?: string;
}

interface CompactSelectProps {
  ariaLabel: string;
  value: string;
  options: readonly CompactSelectOption[];
  onValueChange: (value: string) => void;
  className?: string;
  disabled?: boolean;
  leadingIcon?: ReactNode;
  placeholder?: string;
}

/** 渲染键盘可访问、弹层风格与 trigger 一致的选择控件。 */
export function CompactSelect({
  ariaLabel,
  value,
  options,
  onValueChange,
  className = '',
  disabled = false,
  leadingIcon,
  placeholder,
}: CompactSelectProps) {
  const triggerRef = useRef<HTMLButtonElement>(null);
  const pointerInteractionRef = useRef(false);

  /** 记录本次弹层由鼠标或触控打开，关闭后不保留伪激活焦点。 */
  function markPointerInteraction() {
    pointerInteractionRef.current = true;
  }

  /** 键盘打开时保留焦点环，不能被鼠标交互的状态污染。 */
  function markKeyboardInteraction(event: KeyboardEvent<HTMLButtonElement>) {
    if (['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(event.key)) {
      pointerInteractionRef.current = false;
    }
  }

  /** 弹层内一旦收到键盘事件，就按键盘路径恢复 trigger 焦点。 */
  function markContentKeyboardInteraction() {
    pointerInteractionRef.current = false;
  }

  /** 关闭弹层时阻止 Radix 把鼠标焦点还给 trigger，键盘路径仍使用默认恢复。 */
  function handleCloseAutoFocus(event: Event) {
    if (!pointerInteractionRef.current) {
      return;
    }

    event.preventDefault();
    pointerInteractionRef.current = false;
    triggerRef.current?.blur();
  }

  return (
    <Select.Root
      value={value}
      disabled={disabled}
      onValueChange={onValueChange}
    >
      <Select.Trigger
        ref={triggerRef}
        className={`compact-select-trigger ${className}`.trim()}
        aria-label={ariaLabel}
        onKeyDown={markKeyboardInteraction}
        onPointerDown={markPointerInteraction}
      >
        {leadingIcon ? (
          <span className="compact-select-leading" aria-hidden="true">
            {leadingIcon}
          </span>
        ) : null}
        <Select.Value
          className="compact-select-value"
          placeholder={placeholder}
        />
        <Select.Icon className="compact-select-chevron">
          <ChevronDown aria-hidden="true" size={14} />
        </Select.Icon>
      </Select.Trigger>

      <Select.Portal>
        <Select.Content
          className="compact-select-content"
          position="popper"
          sideOffset={6}
          collisionPadding={12}
          onCloseAutoFocus={handleCloseAutoFocus}
          onKeyDownCapture={markContentKeyboardInteraction}
        >
          <Select.ScrollUpButton className="compact-select-scroll">
            <ChevronUp aria-hidden="true" size={14} />
          </Select.ScrollUpButton>
          <Select.Viewport className="compact-select-viewport">
            {options.map((option) => (
              <Select.Item
                className="compact-select-item"
                key={option.value}
                value={option.value}
              >
                <span className="compact-select-item-icon" aria-hidden="true">
                  {option.icon}
                </span>
                <span className="compact-select-item-copy">
                  <Select.ItemText>{option.label}</Select.ItemText>
                  {option.description ? (
                    <small>{option.description}</small>
                  ) : null}
                </span>
                <Select.ItemIndicator className="compact-select-check">
                  <Check aria-hidden="true" size={14} strokeWidth={2.2} />
                </Select.ItemIndicator>
              </Select.Item>
            ))}
          </Select.Viewport>
          <Select.ScrollDownButton className="compact-select-scroll">
            <ChevronDown aria-hidden="true" size={14} />
          </Select.ScrollDownButton>
        </Select.Content>
      </Select.Portal>
    </Select.Root>
  );
}
