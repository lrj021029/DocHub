import { type InputHTMLAttributes, type Ref } from 'react';
import { cn } from '@/lib/cn';

export interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  /** 是否处于校验错误状态，自动标记 aria-invalid 并切换危险态边框 */
  invalid?: boolean;
  /** React 19 原生 ref 透传 */
  ref?: Ref<HTMLInputElement>;
}

/**
 * COSS UI 企业级单行文本输入框。
 * 遵循高可读性、清晰占位符对比度与标准键盘焦点提示规范。
 */
export function Input({ invalid = false, className, type = 'text', ref, ...props }: InputProps) {
  return (
    <input
      ref={ref}
      type={type}
      aria-invalid={invalid || undefined}
      className={cn(
        'flex h-9 w-full rounded-lg border bg-surface-elevated px-3 py-1.5 text-sm text-ink shadow-xs transition-colors',
        'placeholder:text-ink-muted/80',
        'focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-ring focus-visible:border-ring',
        'disabled:cursor-not-allowed disabled:bg-surface-muted disabled:opacity-60',
        invalid ? 'border-danger focus-visible:outline-danger' : 'border-line hover:border-line-strong',
        className,
      )}
      {...props}
    />
  );
}
