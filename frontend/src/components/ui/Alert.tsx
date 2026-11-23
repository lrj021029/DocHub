import { type HTMLAttributes, type ReactNode } from 'react';
import { AlertCircle, AlertTriangle, CheckCircle2, Info } from 'lucide-react';
import { cn } from '@/lib/cn';

/** 提示条语义类型 */
export type AlertVariant = 'info' | 'success' | 'warning' | 'danger';

export interface AlertProps extends HTMLAttributes<HTMLDivElement> {
  variant?: AlertVariant;
  /** 可选标题 */
  title?: string;
  /** 可选自定义左侧图标 */
  icon?: ReactNode;
}

const ALERT_STYLES: Record<AlertVariant, string> = {
  info: 'border-info-line bg-info-bg/80 text-info',
  success: 'border-success-line bg-success-bg/80 text-success',
  warning: 'border-warning-line bg-warning-bg/80 text-warning',
  danger: 'border-danger-line bg-danger-bg/80 text-danger',
};

const DEFAULT_ICONS: Record<AlertVariant, typeof Info> = {
  info: Info,
  success: CheckCircle2,
  warning: AlertTriangle,
  danger: AlertCircle,
};

/**
 * COSS UI 语义提示组件。
 * 用于展示权限受限说明、L2/L3 外发拦截警示、请求错误恢复提示与操作确认。
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
      <span className="mt-0.5 shrink-0" aria-hidden="true">
        {icon ?? <IconComp className="h-4 w-4" />}
      </span>
      <div className="min-w-0 flex-1 space-y-1">
        {title ? <p className="font-semibold leading-5">{title}</p> : null}
        {children ? <div className="text-xs leading-5 opacity-95">{children}</div> : null}
      </div>
    </div>
  );
}
