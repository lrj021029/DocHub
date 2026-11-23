import { type HTMLAttributes } from 'react';
import { cn } from '@/lib/cn';

/** 语义徽章变体（覆盖常规状态、敏感等级 L0-L3 与健康状态） */
export type BadgeVariant = 'neutral' | 'outline' | 'success' | 'warning' | 'danger' | 'info';

export interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  variant?: BadgeVariant;
}

const BADGE_CLASSES: Record<BadgeVariant, string> = {
  neutral: 'bg-surface-muted text-ink-secondary border-line',
  outline: 'bg-surface-elevated text-ink border-line',
  success: 'bg-success-bg text-success border-success-line',
  warning: 'bg-warning-bg text-warning border-warning-line',
  danger: 'bg-danger-bg text-danger border-danger-line',
  info: 'bg-info-bg text-info border-info-line',
};

/**
 * COSS UI 状态徽章组件。
 * 用于清晰呈现状态机阶段、ACL 维度、FAQ 三轴状态与敏感级别。
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
