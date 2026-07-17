/** 提供小型、无业务含义的前端样式工具。 */

import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

/** 合并条件 className，并让后出现的 Tailwind utility 覆盖冲突项。 */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
