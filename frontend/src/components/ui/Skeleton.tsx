import { type HTMLAttributes } from 'react';
import { cn } from '@/lib/cn';

export interface SkeletonProps extends HTMLAttributes<HTMLDivElement> {
  /** 可选预设形状：文本行、卡片块或圆形头像 */
  shape?: 'line' | 'block' | 'circle';
}

/**
 * COSS UI 骨架屏占位组件。
 * 用于显式表达真实数据加载态，杜绝虚构进度条或空白闪烁。
 */
export function Skeleton({ shape = 'block', className, ...props }: SkeletonProps) {
  return (
    <div
      aria-hidden="true"
      className={cn(
        'animate-pulse bg-surface-muted',
        shape === 'line' && 'h-4 rounded-md',
        shape === 'block' && 'rounded-lg',
        shape === 'circle' && 'rounded-full',
        className,
      )}
      {...props}
    />
  );
}
