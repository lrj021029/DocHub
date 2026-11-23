import { type HTMLAttributes, type TdHTMLAttributes, type ThHTMLAttributes } from 'react';
import { cn } from '@/lib/cn';

/**
 * COSS UI 数据表格容器。
 * 内置水平滚动保护与统一圆角边框，适合文档台账、ACL、FAQ 与审计明细等复杂企业表格。
 */
export function Table({ className, ...props }: HTMLAttributes<HTMLTableElement>) {
  return (
    <div className="w-full overflow-x-auto rounded-lg border border-line bg-surface-elevated">
      <table className={cn('w-full border-collapse text-left text-sm', className)} {...props} />
    </div>
  );
}

/** 表头区域 */
export function TableHeader({ className, ...props }: HTMLAttributes<HTMLTableSectionElement>) {
  return (
    <thead
      className={cn('border-b border-line bg-surface-subtle text-xs font-medium text-ink-muted', className)}
      {...props}
    />
  );
}

/** 表体区域 */
export function TableBody({ className, ...props }: HTMLAttributes<HTMLTableSectionElement>) {
  return <tbody className={cn('divide-y divide-line/80', className)} {...props} />;
}

/** 表格行 */
export function TableRow({ className, ...props }: HTMLAttributes<HTMLTableRowElement>) {
  return (
    <tr
      className={cn('transition-colors hover:bg-surface-muted/50 data-[selected=true]:bg-info-bg/50', className)}
      {...props}
    />
  );
}

/** 表头单元格 */
export function TableHead({ className, ...props }: ThHTMLAttributes<HTMLTableCellElement>) {
  return (
    <th
      scope="col"
      className={cn('h-10 px-4 text-left align-middle text-xs font-medium text-ink-muted whitespace-nowrap', className)}
      {...props}
    />
  );
}

/** 数据单元格 */
export function TableCell({ className, ...props }: TdHTMLAttributes<HTMLTableCellElement>) {
  return (
    <td
      className={cn('px-4 py-3 align-middle text-sm text-ink tabular-nums', className)}
      {...props}
    />
  );
}

/** 表格说明标题（辅助无障碍朗读） */
export function TableCaption({ className, ...props }: HTMLAttributes<HTMLTableCaptionElement>) {
  return (
    <caption className={cn('py-2.5 text-xs text-ink-muted', className)} {...props} />
  );
}
