import { type ComponentPropsWithoutRef, type ReactNode } from 'react';
import { Dialog as BaseDialog } from '@base-ui/react/dialog';
import { X } from 'lucide-react';
import { cn } from '@/lib/cn';

/**
 * COSS UI 对话框组件（底层基于 Base UI @base-ui/react/dialog 原语）。
 * 原生支持 Esc 关闭、焦点陷阱（Focus Trap）、无障碍 aria-labelledby/aria-describedby 与背景遮罩。
 */
export const Dialog = BaseDialog.Root;
export const DialogTrigger = BaseDialog.Trigger;
export const DialogClose = BaseDialog.Close;

export interface DialogContentProps extends ComponentPropsWithoutRef<typeof BaseDialog.Popup> {
  /** 是否显示右上角关闭按钮，默认开启 */
  showCloseButton?: boolean;
  /** 子节点内容 */
  children?: ReactNode;
}

/** 对话框弹出主体（含 Backdrop 遮罩与 Portal 渲染） */
export function DialogContent({
  showCloseButton = true,
  className,
  children,
  ...props
}: DialogContentProps) {
  return (
    <BaseDialog.Portal>
      <BaseDialog.Backdrop
        className={cn(
          'fixed inset-0 z-50 bg-ink/35 backdrop-blur-[2px] transition-opacity duration-150',
          'data-[starting-style]:opacity-0 data-[ending-style]:opacity-0',
        )}
      />
      <BaseDialog.Popup
        className={cn(
          'fixed top-1/2 left-1/2 z-50 w-[min(92vw,32rem)] -translate-x-1/2 -translate-y-1/2',
          'rounded-xl border border-line bg-surface-elevated p-6 text-ink shadow-md outline-none',
          'transition-all duration-150 data-[starting-style]:scale-95 data-[starting-style]:opacity-0',
          'data-[ending-style]:scale-95 data-[ending-style]:opacity-0',
          className,
        )}
        {...props}
      >
        {children}
        {showCloseButton ? (
          <BaseDialog.Close
            aria-label="关闭弹窗"
            className={cn(
              'absolute top-4 right-4 inline-flex h-7 w-7 items-center justify-center rounded-md',
              'text-ink-muted transition-colors hover:bg-surface-muted hover:text-ink',
              'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring',
            )}
          >
            <X className="h-4 w-4" aria-hidden="true" />
          </BaseDialog.Close>
        ) : null}
      </BaseDialog.Popup>
    </BaseDialog.Portal>
  );
}

/** 对话框头部容器 */
export function DialogHeader({ className, ...props }: ComponentPropsWithoutRef<'div'>) {
  return <div className={cn('flex flex-col gap-1.5 pr-6', className)} {...props} />;
}

/** 对话框标题 */
export function DialogTitle({
  className,
  ...props
}: ComponentPropsWithoutRef<typeof BaseDialog.Title>) {
  return (
    <BaseDialog.Title
      className={cn('text-base font-semibold tracking-tight text-ink', className)}
      {...props}
    />
  );
}

/** 对话框描述文案 */
export function DialogDescription({
  className,
  ...props
}: ComponentPropsWithoutRef<typeof BaseDialog.Description>) {
  return (
    <BaseDialog.Description
      className={cn('text-xs leading-relaxed text-ink-muted', className)}
      {...props}
    />
  );
}

/** 对话框底部操作区 */
export function DialogFooter({ className, ...props }: ComponentPropsWithoutRef<'div'>) {
  return (
    <div
      className={cn('mt-5 flex flex-wrap items-center justify-end gap-2', className)}
      {...props}
    />
  );
}
