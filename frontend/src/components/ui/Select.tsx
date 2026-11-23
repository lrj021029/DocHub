import { type ComponentPropsWithoutRef } from 'react';
import { Select as BaseSelect } from '@base-ui/react/select';
import { Check, ChevronDown } from 'lucide-react';
import { cn } from '@/lib/cn';

/**
 * COSS UI 下拉选择器（底层基于 Base UI @base-ui/react/select 原语）。
 * 支持上下方向键导航、回车选中、Esc 关闭与屏幕阅读器完整播报。
 */
export const Select = BaseSelect.Root;
export const SelectGroup = BaseSelect.Group;
export const SelectValue = BaseSelect.Value;

/** 下拉触发按钮 */
export function SelectTrigger({
  className,
  children,
  ...props
}: ComponentPropsWithoutRef<typeof BaseSelect.Trigger>) {
  return (
    <BaseSelect.Trigger
      className={cn(
        'inline-flex h-9 w-full items-center justify-between gap-2 rounded-lg border border-line bg-surface-elevated px-3 text-sm text-ink shadow-xs transition-colors',
        'hover:border-line-strong focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-ring',
        'disabled:cursor-not-allowed disabled:bg-surface-muted disabled:opacity-60',
        className,
      )}
      {...props}
    >
      <span className="truncate">{children}</span>
      <BaseSelect.Icon className="shrink-0 text-ink-muted">
        <ChevronDown className="h-4 w-4" aria-hidden="true" />
      </BaseSelect.Icon>
    </BaseSelect.Trigger>
  );
}

/** 下拉选项浮层容器 */
export function SelectContent({
  className,
  children,
  ...props
}: ComponentPropsWithoutRef<typeof BaseSelect.Popup>) {
  return (
    <BaseSelect.Portal>
      <BaseSelect.Positioner sideOffset={6} alignItemWithTrigger={false} className="z-50">
        <BaseSelect.Popup
          className={cn(
            'min-w-[var(--anchor-width)] max-h-72 overflow-y-auto rounded-lg border border-line bg-surface-elevated p-1 text-ink shadow-md outline-none',
            'transition-all duration-120 data-[starting-style]:scale-98 data-[starting-style]:opacity-0',
            'data-[ending-style]:scale-98 data-[ending-style]:opacity-0',
            className,
          )}
          {...props}
        >
          <BaseSelect.List>{children}</BaseSelect.List>
        </BaseSelect.Popup>
      </BaseSelect.Positioner>
    </BaseSelect.Portal>
  );
}

/** 下拉单条选项 */
export function SelectItem({
  className,
  children,
  ...props
}: ComponentPropsWithoutRef<typeof BaseSelect.Item>) {
  return (
    <BaseSelect.Item
      className={cn(
        'relative flex w-full cursor-pointer items-center justify-between gap-2 rounded-md py-1.5 pr-8 pl-2.5 text-sm text-ink outline-none select-none',
        'data-[highlighted]:bg-surface-muted data-[selected]:font-medium',
        'data-[disabled]:pointer-events-none data-[disabled]:opacity-50',
        className,
      )}
      {...props}
    >
      <BaseSelect.ItemText className="truncate">{children}</BaseSelect.ItemText>
      <BaseSelect.ItemIndicator className="absolute right-2 inline-flex items-center text-accent">
        <Check className="h-3.5 w-3.5" aria-hidden="true" />
      </BaseSelect.ItemIndicator>
    </BaseSelect.Item>
  );
}

/** 下拉分组标题 */
export function SelectGroupLabel({
  className,
  ...props
}: ComponentPropsWithoutRef<typeof BaseSelect.GroupLabel>) {
  return (
    <BaseSelect.GroupLabel
      className={cn('px-2.5 py-1 text-[11px] font-medium text-ink-muted', className)}
      {...props}
    />
  );
}
