import { type ButtonHTMLAttributes, type Ref } from 'react';
import { cn } from '@/lib/cn';

/** COSS UI 风格按钮变体 */
export type ButtonVariant = 'primary' | 'secondary' | 'outline' | 'ghost' | 'destructive';

/** COSS UI 风格按钮尺寸 */
export type ButtonSize = 'sm' | 'md' | 'lg' | 'icon';

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** 视觉样式变体，默认 primary */
  variant?: ButtonVariant;
  /** 控件尺寸，默认 md */
  size?: ButtonSize;
  /** 是否处于提交/加载态，自动禁用交互并暴露 aria-busy */
  loading?: boolean;
  /** React 19 原生 ref 透传 */
  ref?: Ref<HTMLButtonElement>;
}

const VARIANT_CLASSES: Record<ButtonVariant, string> = {
  primary:
    'bg-accent text-ink-inverted border border-accent shadow-xs hover:bg-accent-hover active:translate-y-[0.5px]',
  secondary:
    'bg-surface-muted text-ink border border-transparent hover:bg-accent-subtle/80 active:translate-y-[0.5px]',
  outline:
    'bg-surface-elevated text-ink border border-line shadow-xs hover:bg-surface-muted/70 hover:border-line-strong active:translate-y-[0.5px]',
  ghost: 'bg-transparent text-ink-secondary border border-transparent hover:bg-surface-muted hover:text-ink',
  destructive:
    'bg-danger text-white border border-danger shadow-xs hover:bg-danger/90 active:translate-y-[0.5px]',
};

const SIZE_CLASSES: Record<ButtonSize, string> = {
  sm: 'h-8 px-2.5 text-xs gap-1.5 rounded-md',
  md: 'h-9 px-3.5 text-sm gap-2 rounded-lg',
  lg: 'h-10 px-4 text-sm gap-2 rounded-lg',
  icon: 'h-9 w-9 p-0 rounded-lg',
};

/**
 * COSS UI 企业级按钮组件。
 * 支持键盘聚焦环、加载态无障碍提示（aria-busy）与克制的微交互反馈。
 */
export function Button({
  variant = 'primary',
  size = 'md',
  loading = false,
  disabled,
  type = 'button',
  className,
  children,
  ref,
  ...props
}: ButtonProps) {
  const isDisabled = Boolean(disabled || loading);

  return (
    <button
      ref={ref}
      type={type}
      disabled={isDisabled}
      aria-busy={loading || undefined}
      className={cn(
        'inline-flex items-center justify-center font-medium whitespace-nowrap transition-colors duration-150 select-none',
        'cursor-pointer disabled:pointer-events-none disabled:opacity-50',
        'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring',
        VARIANT_CLASSES[variant],
        SIZE_CLASSES[size],
        className,
      )}
      {...props}
    >
      {loading ? (
        <span
          aria-hidden="true"
          className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-current border-t-transparent"
        />
      ) : null}
      {children}
    </button>
  );
}
