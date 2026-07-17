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

export interface CompactSelectGroup {
  id: string;
  label: string;
  icon?: ReactNode;
  options: readonly CompactSelectOption[];
}

interface CompactSelectProps {
  ariaLabel: string;
  value: string;
  options?: readonly CompactSelectOption[];
  groups?: readonly CompactSelectGroup[];
  onValueChange: (value: string) => void;
  className?: string;
  disabled?: boolean;
  leadingIcon?: ReactNode;
  placeholder?: string;
}

/** 渲染一个可选项，group 与非 group 模式共用同一视觉和键盘行为。 */
function compactSelectItem(option: CompactSelectOption) {
  return (
    <Select.Item
      className="compact-select-item"
      key={option.value}
      value={option.value}
      aria-label={option.label}
    >
      {option.icon ? (
        <span className="compact-select-item-icon" aria-hidden="true">
          {option.icon}
        </span>
      ) : null}
      <span className="compact-select-item-copy">
        <Select.ItemText>{option.label}</Select.ItemText>
        {option.description ? <small>{option.description}</small> : null}
      </span>
      <Select.ItemIndicator className="compact-select-check">
        <Check aria-hidden="true" size={14} strokeWidth={2.2} />
      </Select.ItemIndicator>
    </Select.Item>
  );
}

/** 渲染键盘可访问、弹层风格与 trigger 一致的选择控件。 */
export function CompactSelect({
  ariaLabel,
  value,
  options = [],
  groups = [],
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
            {options.map(compactSelectItem)}
            {groups.map((group) => (
              <Select.Group key={group.id}>
                <Select.Label className="compact-select-item compact-select-group-label">
                  {group.icon ? (
                    <span
                      className="compact-select-item-icon"
                      aria-hidden="true"
                    >
                      {group.icon}
                    </span>
                  ) : null}
                  <span className="compact-select-item-copy">
                    <span>
                      <strong>{group.label}</strong>
                    </span>
                  </span>
                </Select.Label>
                {group.options.map(compactSelectItem)}
              </Select.Group>
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
