import { type HTMLAttributes } from 'react';
import { cn } from '@/lib/cn';

/** 语义徽章变体（Notion 风格柔和淡彩胶囊标签） */
export type BadgeVariant = 'neutral' | 'outline' | 'success' | 'warning' | 'danger' | 'info';

export interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  variant?: BadgeVariant;
}

const BADGE_CLASSES: Record<BadgeVariant, string> = {
  neutral: 'bg-surface-muted text-ink-secondary border-transparent',
  outline: 'bg-surface-elevated text-ink-secondary border-line',
  success: 'bg-success-bg text-success border-success-line/60',
  warning: 'bg-warning-bg text-warning border-warning-line/70',
  danger: 'bg-danger-bg text-danger border-danger-line/70',
  info: 'bg-info-bg text-info border-info-line/60',
};

/**
 * COSS UI × Notion 风格柔和胶囊徽章组件。
 * 采用低饱和度淡彩背景，清晰表达状态机阶段、ACL 维度与 L0-L3 敏感等级而不刺眼。
 */
export function Badge({ variant = 'neutral', className, ...props }: BadgeProps) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-[11px] font-medium leading-4 tracking-tight whitespace-nowrap',
        BADGE_CLASSES[variant],
        className,
      )}
      {...props}
    />
  );
}
