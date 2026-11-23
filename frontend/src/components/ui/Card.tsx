import { type HTMLAttributes, type Ref } from 'react';
import { cn } from '@/lib/cn';

export interface CardProps extends HTMLAttributes<HTMLDivElement> {
  ref?: Ref<HTMLDivElement>;
}

/**
 * COSS UI × Notion 风格卡片容器：
 * 去除厚重阴影，采用纯白背景、1px 极细暖灰边框（#E9E9E7）与柔和悬停过渡。
 */
export function Card({ className, ref, ...props }: CardProps) {
  return (
    <div
      ref={ref}
      className={cn(
        'rounded-lg border border-line bg-surface-elevated text-ink transition-colors duration-150',
        className,
      )}
      {...props}
    />
  );
}

/** 卡片头部容器（承载标题、副标题与右侧操作区） */
export function CardHeader({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn('flex flex-col gap-1 border-b border-line/80 px-5 py-3.5', className)}
      {...props}
    />
  );
}

/** 卡片主标题 */
export function CardTitle({ className, ...props }: HTMLAttributes<HTMLHeadingElement>) {
  return (
    <h3
      className={cn('text-sm font-semibold tracking-tight text-ink', className)}
      {...props}
    />
  );
}

/** 卡片辅助描述文本 */
export function CardDescription({ className, ...props }: HTMLAttributes<HTMLParagraphElement>) {
  return (
    <p className={cn('text-xs leading-relaxed text-ink-muted', className)} {...props} />
  );
}

/** 卡片正文内容区 */
export function CardContent({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('px-5 py-4', className)} {...props} />;
}

/** 卡片底部操作或元信息栏 */
export function CardFooter({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        'flex items-center justify-between gap-2 rounded-b-lg border-t border-line/80 bg-surface-subtle px-5 py-3',
        className,
      )}
      {...props}
    />
  );
}
