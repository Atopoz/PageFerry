/** 识别只影响 PageFerry 窗口装饰策略的桌面平台。 */

export type DesktopPlatform = 'macos' | 'windows' | 'linux' | 'other';

/** 根据 WebView user agent 判断只影响窗口装饰的桌面平台。 */
export function detectDesktopPlatform(userAgent: string): DesktopPlatform {
  if (/Windows/i.test(userAgent)) return 'windows';
  if (/Macintosh|Mac OS X/i.test(userAgent)) return 'macos';
  if (/Linux/i.test(userAgent)) return 'linux';
  return 'other';
}
