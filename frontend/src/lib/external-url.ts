/** 在 Tauri 中使用系统 opener 打开外部 HTTP(S) 页面，并为 Web dev 保留安全 fallback。 */

/** 判断当前 renderer 是否运行在 Tauri WebView 中。 */
function isTauriRuntime(): boolean {
  return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;
}

/**
 * 规范化可以交给系统浏览器的外链。
 *
 * 这里故意只允许 HTTP(S)，避免 catalog 或 sidecar 中的异常值把 `file:`、
 * `javascript:` 等任意 scheme 交给原生 opener。
 */
export function normalizeExternalHttpUrl(rawUrl: string): string {
  let parsed: URL;
  try {
    parsed = new URL(rawUrl);
  } catch {
    throw new Error('外部链接不是有效 URL。');
  }

  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new Error('外部链接只允许使用 HTTP 或 HTTPS。');
  }
  if (parsed.username || parsed.password) {
    throw new Error('外部链接不能包含账号信息。');
  }
  return parsed.href;
}

/** 返回可安全渲染为 anchor href 的 URL；非法值不进入 DOM。 */
export function safeExternalHttpUrl(
  rawUrl: string | null | undefined,
): string | null {
  if (!rawUrl) return null;
  try {
    return normalizeExternalHttpUrl(rawUrl);
  } catch {
    return null;
  }
}

/**
 * 用系统默认浏览器打开外链。
 *
 * Tauri runtime 显式调用 opener plugin；普通浏览器只在同步用户手势内创建
 * `noopener,noreferrer` 新页，避免异步后触发 popup blocker 或反向控制来源页。
 */
export async function openExternalHttpUrl(rawUrl: string): Promise<void> {
  const url = normalizeExternalHttpUrl(rawUrl);
  if (isTauriRuntime()) {
    const { openUrl } = await import('@tauri-apps/plugin-opener');
    await openUrl(url);
    return;
  }

  const opened = window.open(url, '_blank', 'noopener,noreferrer');
  if (opened === null) {
    throw new Error('浏览器阻止了新窗口，请允许 PageFerry 打开链接。');
  }
}
