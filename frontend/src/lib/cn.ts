import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

/**
 * 合并条件样式类并解决 Tailwind CSS 原子类冲突。
 * 遵循 COSS UI 组件样式组合惯例。
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
