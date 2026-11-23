import { type HTMLAttributes, type ReactNode } from 'react';
import { AlertCircle, AlertTriangle, CheckCircle2, Info } from 'lucide-react';
import { cn } from '@/lib/cn';

/** Callout 提示条语义类型 */
export type AlertVariant = 'info' | 'success' | 'warning' | 'danger';

export interface AlertProps extends HTMLAttributes<HTMLDivElement> {
  variant?: AlertVariant;
  /** 可选标题 */
  title?: string;
  /** 可选自定义左侧图标 */
  icon?: ReactNode;
}

const ALERT_STYLES: Record<AlertVariant, string> = {
  info: 'border-info-line/80 bg-info-bg/75 text-ink',
  success: 'border-success-line/80 bg-success-bg/75 text-ink',
  warning: 'border-warning-line/90 bg-warning-bg/85 text-ink',
  danger: 'border-danger-line/90 bg-danger-bg/80 text-ink',
};

const ICON_COLORS: Record<AlertVariant, string> = {
  info: 'text-info',
  success: 'text-success',
  warning: 'text-warning',
  danger: 'text-danger',
};

const DEFAULT_ICONS: Record<AlertVariant, typeof Info> = {
  info: Info,
  success: CheckCircle2,
  warning: AlertTriangle,
  danger: AlertCircle,
};

/**
 * COSS UI × Notion Callout 风格语义提示块。
 * 柔和底色配合高可读深墨文字与左侧语义图标，适合呈现 Quarantine、L2/L3 外发拦截与 ACL 说明。
 */
export function Alert({
  variant = 'info',
  title,
  icon,
  className,
  children,
  ...props
}: AlertProps) {
  const IconComp = DEFAULT_ICONS[variant];

  return (
    <div
      role={variant === 'danger' ? 'alert' : 'status'}
      className={cn(
        'flex items-start gap-3 rounded-lg border px-4 py-3 text-sm leading-relaxed',
        ALERT_STYLES[variant],
        className,
      )}
      {...props}
    >
      <span className={cn('mt-0.5 shrink-0', ICON_COLORS[variant])} aria-hidden="true">
        {icon ?? <IconComp className="h-4 w-4" />}
      </span>
      <div className="min-w-0 flex-1 space-y-0.5">
        {title ? <p className="text-[13px] font-semibold leading-5 text-ink">{title}</p> : null}
        {children ? (
          <div className="text-xs leading-5 text-ink-secondary">{children}</div>
        ) : null}
      </div>
    </div>
  );
}
