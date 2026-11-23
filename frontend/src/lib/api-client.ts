import {
  clearAuthSession,
  getAccessToken,
  getCsrfToken,
  getCurrentOrgId,
  refreshAccessToken,
} from '@/lib/auth-store';
import type { ReadinessStatus } from '@/types/domain';

/** 统一 API 错误对象，保留后端结构化错误码与链路追踪 ID */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details?: unknown;
  readonly requestId?: string;

  constructor(params: {
    status: number;
    code: string;
    message: string;
    details?: unknown;
    requestId?: string;
  }) {
    super(params.message);
    this.name = 'ApiError';
    this.status = params.status;
    this.code = params.code;
    this.details = params.details;
    this.requestId = params.requestId;
  }
}

/** 统一请求配置项 */
export interface ApiRequestOptions extends Omit<RequestInit, 'body'> {
  /** 请求体对象（非 FormData/string 时自动序列化为 JSON） */
  body?: unknown;
  /** 显式覆盖当前请求的 Organization ID */
  organizationId?: string | null;
  /** 是否禁用 401 自动静默 Refresh 重试（登录/刷新接口默认跳过） */
  skipAuthRefresh?: boolean;
  /** 幂等键（用于文档上传确认、发布、ACL 变更等关键写操作） */
  idempotencyKey?: string;
  /** 自定义 fetch 实现（便于单测注入） */
  fetchImpl?: typeof fetch;
}

/** 不应触发 401 自动续期重放的认证端点白名单 */
const NO_REFRESH_PATHS = ['/api/v1/auth/login', '/api/v1/auth/refresh', '/api/v1/auth/logout'];

/** 判断请求是否属于无需自动续期的端点 */
function shouldSkipRefresh(url: string, explicitSkip?: boolean): boolean {
  if (explicitSkip) return true;
  return NO_REFRESH_PATHS.some((path) => url.includes(path));
}

/** 组装标准安全请求头（Bearer 内存令牌 + X-Organization-Id + X-CSRF-Token） */
function buildRequestHeaders(
  options: ApiRequestOptions,
  accessToken: string | null,
): Headers {
  const headers = new Headers(options.headers);
  if (!headers.has('Accept')) {
    headers.set('Accept', 'application/json');
  }

  const isFormData = typeof FormData !== 'undefined' && options.body instanceof FormData;
  if (options.body !== undefined && !isFormData && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }

  if (accessToken) {
    headers.set('Authorization', `Bearer ${accessToken}`);
  }

  const orgId = options.organizationId !== undefined ? options.organizationId : getCurrentOrgId();
  if (orgId) {
    headers.set('X-Organization-Id', orgId);
  }

  const csrfToken = getCsrfToken();
  if (csrfToken) {
    headers.set('X-CSRF-Token', csrfToken);
  }

  if (options.idempotencyKey) {
    headers.set('Idempotency-Key', options.idempotencyKey);
  }

  return headers;
}

/** 序列化请求体 */
function serializeBody(body: unknown): BodyInit | undefined {
  if (body === undefined || body === null) {
    return undefined;
  }
  if (
    typeof body === 'string' ||
    body instanceof Blob ||
    body instanceof ArrayBuffer ||
    body instanceof URLSearchParams ||
    (typeof FormData !== 'undefined' && body instanceof FormData)
  ) {
    return body;
  }
  return JSON.stringify(body);
}

/** 解析后端错误响应体，兜底构造规范 ApiError */
async function parseApiError(response: Response): Promise<ApiError> {
  const requestId = response.headers.get('X-Request-Id') ?? undefined;
  let code = `HTTP_${response.status}`;
  let message = response.statusText || '请求失败';
  let details: unknown;

  try {
    const contentType = response.headers.get('content-type') ?? '';
    if (contentType.includes('application/json')) {
      const payload = (await response.json()) as {
        code?: string;
        error_code?: string;
        message?: string;
        detail?: string | { code?: string; message?: string };
        details?: unknown;
      };
      if (typeof payload.detail === 'string') {
        message = payload.detail;
      } else if (payload.detail && typeof payload.detail === 'object') {
        code = payload.detail.code ?? code;
        message = payload.detail.message ?? message;
      }
      code = payload.code ?? payload.error_code ?? code;
      message = payload.message ?? message;
      details = payload.details ?? payload;
    } else {
      const text = await response.text();
      if (text) {
        message = text;
      }
    }
  } catch {
    // 忽略解析异常，保留基础 HTTP 状态信息
  }

  return new ApiError({
    status: response.status,
    code,
    message,
    details,
    requestId,
  });
}

/**
 * 统一 Fetch 请求客户端。
 * 核心行为：
 * 1. 自动附加内存 Access Token（Authorization: Bearer）、X-Organization-Id、X-CSRF-Token；
 * 2. 默认携带 credentials: 'include' 以支持同源 HttpOnly Refresh Cookie；
 * 3. 收到 401 时自动触发单次静默 Refresh 并使用新令牌重放原请求一次。
 */
export async function apiRequest<T>(
  url: string,
  options: ApiRequestOptions = {},
): Promise<T> {
  const fetchImpl = options.fetchImpl ?? globalThis.fetch;
  const serializedBody = serializeBody(options.body);

  const execute = (token: string | null): Promise<Response> => {
    const headers = buildRequestHeaders(options, token);
    return fetchImpl(url, {
      ...options,
      headers,
      body: serializedBody,
      credentials: options.credentials ?? 'include',
    });
  };

  let response = await execute(getAccessToken());

  // 遇到 401 且允许静默续期时，执行单次 Refresh 并重放原请求
  if (response.status === 401 && !shouldSkipRefresh(url, options.skipAuthRefresh)) {
    const refreshedToken = await refreshAccessToken(fetchImpl);
    if (refreshedToken) {
      response = await execute(refreshedToken);
    } else {
      clearAuthSession();
    }
  }

  if (!response.ok) {
    throw await parseApiError(response);
  }

  // 204 No Content 直接返回 undefined
  if (response.status === 204) {
    return undefined as T;
  }

  const contentType = response.headers.get('content-type') ?? '';
  if (contentType.includes('application/json')) {
    return (await response.json()) as T;
  }
  return (await response.text()) as unknown as T;
}

/** 语义化 HTTP 方法封装 */
export const apiClient = {
  get: <T>(url: string, options?: Omit<ApiRequestOptions, 'method' | 'body'>) =>
    apiRequest<T>(url, { ...options, method: 'GET' }),

  post: <T>(url: string, body?: unknown, options?: Omit<ApiRequestOptions, 'method' | 'body'>) =>
    apiRequest<T>(url, { ...options, method: 'POST', body }),

  put: <T>(url: string, body?: unknown, options?: Omit<ApiRequestOptions, 'method' | 'body'>) =>
    apiRequest<T>(url, { ...options, method: 'PUT', body }),

  patch: <T>(url: string, body?: unknown, options?: Omit<ApiRequestOptions, 'method' | 'body'>) =>
    apiRequest<T>(url, { ...options, method: 'PATCH', body }),

  delete: <T>(url: string, options?: Omit<ApiRequestOptions, 'method'>) =>
    apiRequest<T>(url, { ...options, method: 'DELETE' }),
};

/**
 * 探测后端就绪探针（/api/v1/readyz），供 AppShell 顶部实时健康指示器使用。
 * 后端尚未启动或网络异常时返回 unavailable 状态，不抛出未捕获异常。
 */
export async function fetchBackendReadiness(
  fetchImpl: typeof fetch = globalThis.fetch,
): Promise<ReadinessStatus> {
  try {
    const response = await fetchImpl('/api/v1/readyz', {
      method: 'GET',
      headers: { Accept: 'application/json' },
      credentials: 'include',
    });

    if (!response.ok) {
      return {
        status: response.status >= 500 ? 'degraded' : 'unavailable',
        timestamp: new Date().toISOString(),
      };
    }

    const data = (await response.json()) as Partial<ReadinessStatus>;
    return {
      status: data.status ?? 'ready',
      checks: data.checks,
      timestamp: data.timestamp ?? new Date().toISOString(),
    };
  } catch {
    return {
      status: 'unavailable',
      timestamp: new Date().toISOString(),
    };
  }
}
