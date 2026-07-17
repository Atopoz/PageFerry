/** 创建 PageFerry 的 React root，并在开发期保留 StrictMode 检查。 */

import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import { App } from './App';
import './styles/global.css';

const root = document.getElementById('root');

if (root === null) {
  throw new Error('PageFerry root element is missing');
}

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
