import { type ComponentPropsWithoutRef } from 'react';
import { Tabs as BaseTabs } from '@base-ui/react/tabs';
import { cn } from '@/lib/cn';

/**
 * COSS UI 选项卡组件（底层基于 Base UI @base-ui/react/tabs 原语）。
 * 原生支持左右方向键切换、Home/End 跳转与标准 ARIA tablist/tab/tabpanel 语义。
 */
export function Tabs({
  className,
  ...props
}: ComponentPropsWithoutRef<typeof BaseTabs.Root>) {
  return <BaseTabs.Root className={cn('flex flex-col gap-3', className)} {...props} />;
}

/** 选项卡导航列表容器 */
export function TabsList({
  className,
  ...props
}: ComponentPropsWithoutRef<typeof BaseTabs.List>) {
  return (
    <BaseTabs.List
      className={cn(
        'inline-flex w-fit items-center gap-1 rounded-lg border border-line bg-surface-muted/80 p-1',
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
        'inline-flex h-7 cursor-pointer items-center justify-center rounded-md px-3 text-xs font-medium text-ink-secondary transition-all select-none',
        'hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-ring',
        'data-[selected]:bg-surface-elevated data-[selected]:text-ink data-[selected]:shadow-xs',
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
