import { createContext, createElement, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import type { AuthUser, OrganizationSummary } from '@/types/domain';

/**
 * 内存认证会话快照结构。
 * 安全红线（ADR-003 & TECHNICAL_SPEC §3.3）：
 * - Access Token 仅保存在模块内存变量与 React Context 中；
 * - 严禁写入 localStorage、sessionStorage 或 URL 参数；
 * - Refresh Token 由后端通过 HttpOnly + Secure + SameSite Cookie 管理。
 */
export interface AuthSessionState {
  accessToken: string | null;
  csrfToken: string | null;
  user: AuthUser | null;
  currentOrgId: string | null;
  organizations: OrganizationSummary[];
  isAuthenticated: boolean;
  isRefreshing: boolean;
}

/** 认证会话变更监听器 */
type AuthListener = (state: AuthSessionState) => void;

/** 刷新接口返回的数据结构 */
export interface RefreshResponsePayload {
  access_token?: string;
  accessToken?: string;
  csrf_token?: string;
  csrfToken?: string;
  user?: AuthUser;
  current_organization_id?: string;
  currentOrgId?: string;
  organizations?: OrganizationSummary[];
}

/** 仅存于 JS 内存中的认证状态（页面刷新后通过 HttpOnly Cookie 静默续期恢复） */
let memoryState: AuthSessionState = {
  accessToken: null,
  csrfToken: null,
  user: null,
  currentOrgId: null,
  organizations: [],
  isAuthenticated: false,
  isRefreshing: false,
};

/** 订阅内存认证状态变更的监听器集合 */
const listeners = new Set<AuthListener>();

/** 并发 401 时的单次静默 Refresh 去重 Promise */
let inFlightRefreshPromise: Promise<string | null> | null = null;

/** 通知所有订阅者最新的内存会话快照 */
function notifyListeners(): void {
  const snapshot: AuthSessionState = { ...memoryState };
  for (const listener of listeners) {
    listener(snapshot);
  }
}

/** 更新内存会话状态（永不触碰 localStorage 或 sessionStorage） */
function patchMemoryState(partial: Partial<AuthSessionState>): void {
  memoryState = {
    ...memoryState,
    ...partial,
  };
  memoryState.isAuthenticated = Boolean(memoryState.accessToken);
  notifyListeners();
}

/**
 * 从同源非 HttpOnly CSRF Cookie 读取令牌（作为服务端未通过 JSON 返回时的兜底来源）。
 * 注意：此 Cookie 仅用于防 CSRF 的双提交校验，绝非 Access Token 或 Refresh Token。
 */
export function readCsrfCookie(): string | null {
  if (typeof document === 'undefined' || !document.cookie) {
    return null;
  }
  const cookieNames = ['dochub_csrf_token', 'csrf_token', 'XSRF-TOKEN'];
  const pairs = document.cookie.split(';');
  for (const pair of pairs) {
    const [rawKey, ...rest] = pair.trim().split('=');
    if (rawKey && cookieNames.includes(rawKey)) {
      return decodeURIComponent(rest.join('='));
    }
  }
  return null;
}

/** 读取当前内存中的 Access Token */
export function getAccessToken(): string | null {
  return memoryState.accessToken;
}

/** 读取当前内存中的 CSRF Token（优先内存显式值，其次双提交 Cookie） */
export function getCsrfToken(): string | null {
  return memoryState.csrfToken ?? readCsrfCookie();
}

/** 读取当前选中的 Organization ID */
export function getCurrentOrgId(): string | null {
  return memoryState.currentOrgId;
}

/** 读取完整内存会话快照 */
export function getAuthSnapshot(): AuthSessionState {
  return { ...memoryState };
}

/**
 * 写入内存认证会话（登录成功或静默续期成功后调用）。
 * 严格仅更新内存变量，不持久化任何令牌到 Web Storage。
 */
export function setAuthSession(params: {
  accessToken: string;
  csrfToken?: string | null;
  user?: AuthUser | null;
  currentOrgId?: string | null;
  organizations?: OrganizationSummary[];
}): void {
  const nextOrgs = params.organizations ?? memoryState.organizations;
  const resolvedOrgId =
    params.currentOrgId !== undefined
      ? params.currentOrgId
      : (memoryState.currentOrgId ?? nextOrgs[0]?.id ?? null);

  patchMemoryState({
    accessToken: params.accessToken,
    csrfToken: params.csrfToken !== undefined ? params.csrfToken : memoryState.csrfToken,
    user: params.user !== undefined ? params.user : memoryState.user,
    organizations: nextOrgs,
    currentOrgId: resolvedOrgId,
  });
}

/** 仅更新当前选中的 Organization ID（绑定后续 API 的 X-Organization-Id 请求头） */
export function setCurrentOrgId(orgId: string | null): void {
  patchMemoryState({ currentOrgId: orgId });
}

/** 仅更新内存中的 CSRF Token */
export function setCsrfToken(csrfToken: string | null): void {
  patchMemoryState({ csrfToken });
}

/** 清空内存会话（登出或 Refresh 失败时调用） */
export function clearAuthSession(): void {
  patchMemoryState({
    accessToken: null,
    csrfToken: null,
    user: null,
    currentOrgId: null,
    organizations: [],
    isRefreshing: false,
  });
}

/** 订阅内存认证状态变更，返回取消订阅函数 */
export function subscribeAuth(listener: AuthListener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/**
 * 使用 HttpOnly Refresh Cookie + X-CSRF-Token 执行静默令牌续期。
 * - 自动对并发触发的续期请求去重，保证同一时间只发一次 /api/v1/auth/refresh；
 * - 成功后将新 Access Token 仅存入内存并返回；失败时清空会话并返回 null。
 */
export async function refreshAccessToken(
  fetchImpl: typeof fetch = globalThis.fetch,
): Promise<string | null> {
  if (inFlightRefreshPromise) {
    return inFlightRefreshPromise;
  }

  inFlightRefreshPromise = (async () => {
    patchMemoryState({ isRefreshing: true });
    try {
      const headers = new Headers({
        Accept: 'application/json',
      });
      const csrf = getCsrfToken();
      if (csrf) {
        headers.set('X-CSRF-Token', csrf);
      }
      if (memoryState.currentOrgId) {
        headers.set('X-Organization-Id', memoryState.currentOrgId);
      }

      const response = await fetchImpl('/api/v1/auth/refresh', {
        method: 'POST',
        credentials: 'include',
        headers,
      });

      if (!response.ok) {
        clearAuthSession();
        return null;
      }

      const headerCsrf = response.headers.get('X-CSRF-Token');
      const payload = (await response.json()) as RefreshResponsePayload;
      const nextToken = payload.access_token ?? payload.accessToken ?? null;

      if (!nextToken) {
        clearAuthSession();
        return null;
      }

      setAuthSession({
        accessToken: nextToken,
        csrfToken: payload.csrf_token ?? payload.csrfToken ?? headerCsrf ?? getCsrfToken(),
        user: payload.user ?? memoryState.user,
        currentOrgId:
          payload.current_organization_id ??
          payload.currentOrgId ??
          memoryState.currentOrgId,
        organizations: payload.organizations ?? memoryState.organizations,
      });

      return nextToken;
    } catch {
      clearAuthSession();
      return null;
    } finally {
      patchMemoryState({ isRefreshing: false });
      inFlightRefreshPromise = null;
    }
  })();

  return inFlightRefreshPromise;
}

/** React Context 对外暴露的认证上下文接口 */
export interface AuthContextValue extends AuthSessionState {
  setSession: typeof setAuthSession;
  clearSession: typeof clearAuthSession;
  switchOrganization: (orgId: string) => void;
  refreshSession: () => Promise<string | null>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

/**
 * AuthProvider：将内存 auth-store 状态桥接到 React 组件树。
 * 保证组件响应式更新的同时，所有令牌始终仅留存于内存。
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthSessionState>(() => getAuthSnapshot());

  useEffect(() => {
    return subscribeAuth(setState);
  }, []);

  const switchOrganization = useCallback((orgId: string) => {
    setCurrentOrgId(orgId);
  }, []);

  const refreshSession = useCallback(() => {
    return refreshAccessToken();
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      ...state,
      setSession: setAuthSession,
      clearSession: clearAuthSession,
      switchOrganization,
      refreshSession,
    }),
    [state, switchOrganization, refreshSession],
  );

  return createElement(AuthContext.Provider, { value }, children);
}

/** 获取当前内存认证上下文 */
export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error('useAuth 必须在 AuthProvider 内部调用');
  }
  return ctx;
}
