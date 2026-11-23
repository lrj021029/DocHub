import { type ComponentPropsWithoutRef } from 'react';
import { Tabs as BaseTabs } from '@base-ui/react/tabs';
import { cn } from '@/lib/cn';

/**
 * COSS UI × Notion 风格选项卡组件（底层基于 Base UI @base-ui/react/tabs 原语）。
 * 采用 Notion 式底部细线导航风格，保持键盘左右键切换与标准 ARIA 语义。
 */
export function Tabs({
  className,
  ...props
}: ComponentPropsWithoutRef<typeof BaseTabs.Root>) {
  return <BaseTabs.Root className={cn('flex flex-col gap-4', className)} {...props} />;
}

/** 选项卡导航列表容器 */
export function TabsList({
  className,
  ...props
}: ComponentPropsWithoutRef<typeof BaseTabs.List>) {
  return (
    <BaseTabs.List
      className={cn(
        'inline-flex w-full items-center gap-1 overflow-x-auto border-b border-line pb-1.5',
        className,
      )}
      {...props}
    />
  );
}

/** 单个选项卡触发按钮 */
export function TabsTrigger({
  className,
  ...props
}: ComponentPropsWithoutRef<typeof BaseTabs.Tab>) {
  return (
    <BaseTabs.Tab
      className={cn(
        'inline-flex h-8 cursor-pointer items-center justify-center rounded-md px-3 text-xs font-medium text-ink-muted transition-colors select-none whitespace-nowrap',
        'hover:bg-surface-muted hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-ring',
        'data-[selected]:bg-surface-muted data-[selected]:font-semibold data-[selected]:text-ink',
        'data-[disabled]:pointer-events-none data-[disabled]:opacity-50',
        className,
      )}
      {...props}
    />
  );
}

/** 选项卡内容面板 */
export function TabsContent({
  className,
  ...props
}: ComponentPropsWithoutRef<typeof BaseTabs.Panel>) {
  return (
    <BaseTabs.Panel
      className={cn(
        'outline-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring',
        className,
      )}
      {...props}
    />
  );
}
