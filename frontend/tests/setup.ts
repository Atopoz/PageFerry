/** 安装 Testing Library 的 DOM matcher，并在每个测试后清理 React tree。 */

import '@testing-library/jest-dom/vitest';

/** Radix 在 jsdom 中会读取浏览器才提供的 pointer capture API。 */
if (!HTMLElement.prototype.hasPointerCapture) {
  HTMLElement.prototype.hasPointerCapture = () => false;
}

/** jsdom 不实现 pointer capture，测试只需稳定的空操作边界。 */
if (!HTMLElement.prototype.setPointerCapture) {
  HTMLElement.prototype.setPointerCapture = () => undefined;
}

/** Radix 定位选中项时可能请求滚动，jsdom 中不需要实际滚动。 */
if (!HTMLElement.prototype.scrollIntoView) {
  HTMLElement.prototype.scrollIntoView = () => undefined;
}
import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

afterEach(cleanup);
