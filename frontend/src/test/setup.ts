import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

/**
 * Vitest 全局测试初始化：
 * - 注册 @testing-library/jest-dom 语义断言
 * - 每个用例结束后自动清理 DOM，避免跨用例状态污染
 */
afterEach(() => {
  cleanup();
});
