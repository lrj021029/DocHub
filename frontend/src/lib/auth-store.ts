import {
  createContext,
  createElement,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import type { AuthUser, OrganizationSummary, UserDepartmentSummary } from '@/types/domain';

/**
 * 内存认证会话快照结构。
 * 安全红线（ADR-003 & TECHNICAL_SPEC §3.3）：
 * - Access Token 仅保存在模块内存变量与 React Context 中；
 * - 严禁写入 localStorage、sessionStorage 或 URL 参数；
 * - Refresh Token 由后端通过 HttpOnly + SameSite Cookie 管理，配合 dochub_csrf Cookie 校验。
 */
export interface AuthSessionState {
  accessToken: string | null;
  csrfToken: string | null;
  user: AuthUser | null;
  currentOrgId: string | null;
  organizations: OrganizationSummary[];
  isAuthenticated: boolean;
  isRefreshing: boolean;
  /** 是否已完成首次启动静默续期探测 */
  hasBootstrapped: boolean;
}

/** 认证会话变更监听器 */
type AuthListener = (state: AuthSessionState) => void;

/** 后端原始用户字段结构（兼容 snake_case 与 camelCase） */
export interface RawAuthUserPayload {
  id?: string;
  user_id?: string;
  email?: string;
  display_name?: string;
  displayName?: string;
  full_name?: string;
  name?: string;
  status?: string;
  system_roles?: Array<string | { code?: string; name?: string }>;
  systemRoles?: string[];
  organization_roles?: Array<string | { code?: string; name?: string }>;
  organizationRoles?: string[];
  permissions?: Array<string | { code?: string }>;
  departments?: Array<{
    department_id?: string;
    departmentId?: string;
    department_name?: string;
    departmentName?: string;
    name?: string;
    is_primary?: boolean;
    isPrimary?: boolean;
    membership_type?: string;
    membershipType?: string;
    valid_from?: string | null;
    validFrom?: string | null;
    valid_until?: string | null;
    validUntil?: string | null;
  }>;
}

/** 后端原始组织字段结构 */
export interface RawOrganizationPayload {
  id: string;
  name: string;
  slug?: string;
  status?: string;
  role_names?: string[];
  roleNames?: string[];
  roles?: Array<string | { code?: string; name?: string }>;
}

/** 登录 / 刷新 / 组织切换 / /auth/me 响应统一兼容模型 */
export interface AuthEndpointPayload {
  access_token?: string;
  accessToken?: string;
  csrf_token?: string;
  csrfToken?: string;
  user?: RawAuthUserPayload;
  active_organization_id?: string | null;
  current_organization_id?: string | null;
  currentOrgId?: string | null;
  organization_id?: string | null;
  organizations?: RawOrganizationPayload[];
  system_roles?: Array<string | { code?: string; name?: string }>;
  organization_roles?: Array<string | { code?: string; name?: string }>;
  permissions?: Array<string | { code?: string }>;
  departments?: RawAuthUserPayload['departments'];
}

/** 将角色或权限数组标准化为字符串代码数组 */
function normalizeCodeList(
  items?: Array<string | { code?: string; name?: string }>,
): string[] {
  if (!Array.isArray(items)) return [];
  return items
    .map((item) => (typeof item === 'string' ? item : (item.code ?? item.name ?? '')))
    .filter(Boolean);
}

/** 将后端组织摘要标准化为前端 OrganizationSummary */
export function normalizeOrganizationList(
  rawList?: RawOrganizationPayload[],
): OrganizationSummary[] {
  if (!Array.isArray(rawList)) return [];
  return rawList.map((org) => {
    const rawStatus = (org.status ?? 'ACTIVE').toUpperCase();
    const status: OrganizationSummary['status'] =
      rawStatus === 'SUSPENDED' || rawStatus === 'ARCHIVED' ? rawStatus : 'ACTIVE';
    const roleNames =
      org.roleNames ??
      org.role_names ??
      normalizeCodeList(org.roles);
    return {
      id: org.id,
      name: org.name,
      slug: org.slug ?? org.id,
      status,
      roleNames,
    };
  });
}

/** 将后端登录或 /auth/me 返回的用户结构标准化为 AuthUser */
export function normalizeAuthUser(
  rawUser?: RawAuthUserPayload | null,
  fallback?: Partial<AuthEndpointPayload>,
): AuthUser | null {
  if (!rawUser && !fallback?.user) return null;
  const src = rawUser ?? fallback?.user;
  if (!src) return null;

  const email = src.email ?? '';
  const displayName =
    src.displayName ??
    src.display_name ??
    src.full_name ??
    src.name ??
    (email ? email.split('@')[0] ?? email : '已认证用户');

  const rawDepts = src.departments ?? fallback?.departments ?? [];
  const departments: UserDepartmentSummary[] = rawDepts.map((d) => ({
    departmentId: d.departmentId ?? d.department_id ?? '',
    departmentName: d.departmentName ?? d.department_name ?? d.name ?? '',
    isPrimary: Boolean(d.isPrimary ?? d.is_primary),
    membershipType: d.membershipType ?? d.membership_type ?? 'FULL_TIME',
    validFrom: d.validFrom ?? d.valid_from ?? null,
    validUntil: d.validUntil ?? d.valid_until ?? null,
  }));

  return {
    id: src.id ?? src.user_id ?? 'unknown-user',
    email,
    displayName,
    status: src.status,
    systemRoles: src.systemRoles ?? normalizeCodeList(src.system_roles ?? fallback?.system_roles),
    organizationRoles:
      src.organizationRoles ??
      normalizeCodeList(src.organization_roles ?? fallback?.organization_roles),
    permissions: normalizeCodeList(src.permissions ?? fallback?.permissions),
    departments,
  };
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
  hasBootstrapped: false,
};

/** 订阅内存认证状态变更的监听器集合 */
const listeners = new Set<AuthListener>();

/** 并发 401 或首屏初始化时的单次静默 Refresh 去重 Promise */
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
 * 从同源非 HttpOnly CSRF Cookie 读取令牌。
 * 优先兼容 Slice 1 后端约定的 `dochub_csrf`，同时兼容常见双提交 Cookie 名称。
 * 注意：此 Cookie 仅用于防 CSRF 校验，绝非 Access Token 或 Refresh Token。
 */
export function readCsrfCookie(): string | null {
  if (typeof document === 'undefined' || !document.cookie) {
    return null;
  }
  const cookieNames = ['dochub_csrf', 'dochub_csrf_token', 'csrf_token', 'XSRF-TOKEN'];
  const pairs = document.cookie.split(';');
  for (const expectedName of cookieNames) {
    for (const pair of pairs) {
      const [rawKey, ...rest] = pair.trim().split('=');
      if (rawKey === expectedName) {
        return decodeURIComponent(rest.join('='));
      }
    }
  }
  return null;
}

/** 读取当前内存中的 Access Token */
export function getAccessToken(): string | null {
  return memoryState.accessToken;
}

/** 读取当前内存中的 CSRF Token（优先内存显式值，其次双提交 Cookie `dochub_csrf`） */
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

/** 判断当前用户是否具备指定功能权限（仅用于 UI 优雅显隐，后端仍强制二次校验） */
export function hasPermission(permissionCode: string, state: AuthSessionState = memoryState): boolean {
  const perms = state.user?.permissions ?? [];
  if (perms.includes('*') || perms.includes(permissionCode)) {
    return true;
  }
  // 兼容模块通配符（例如 org:*）
  const [prefix] = permissionCode.split(':');
  if (prefix && perms.includes(`${prefix}:*`)) {
    return true;
  }
  return false;
}

/**
 * 写入内存认证会话（登录成功、静默续期或切换组织成功后调用）。
 * 严格仅更新内存变量，不持久化任何令牌到 Web Storage。
 */
export function setAuthSession(params: {
  accessToken: string;
  csrfToken?: string | null;
  user?: AuthUser | null;
  currentOrgId?: string | null;
  organizations?: OrganizationSummary[];
  hasBootstrapped?: boolean;
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
    hasBootstrapped: params.hasBootstrapped ?? true,
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

/** 更新内存中的当前用户与权限快照（例如 /auth/me 同步后） */
export function setAuthUser(user: AuthUser | null): void {
  patchMemoryState({ user });
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

/** 重置启动探测标记（仅供单元测试隔离场景使用） */
export function resetAuthBootstrap(): void {
  patchMemoryState({ hasBootstrapped: false });
}

/** 订阅内存认证状态变更，返回取消订阅函数 */
export function subscribeAuth(listener: AuthListener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/**
 * 内部辅助：调用 GET /api/v1/auth/me 拉取最新的当前用户、部门、角色与权限清单。
 */
export async function fetchCurrentUserProfile(
  fetchImpl: typeof fetch = globalThis.fetch,
): Promise<AuthUser | null> {
  const token = getAccessToken();
  if (!token) return null;

  const headers = new Headers({
    Accept: 'application/json',
    Authorization: `Bearer ${token}`,
  });
  if (memoryState.currentOrgId) {
    headers.set('X-Organization-Id', memoryState.currentOrgId);
  }

  try {
    const response = await fetchImpl('/api/v1/auth/me', {
      method: 'GET',
      credentials: 'include',
      headers,
    });
    if (!response.ok) return memoryState.user;

    const payload = (await response.json()) as AuthEndpointPayload & RawAuthUserPayload;
    const normalizedUser = normalizeAuthUser(payload.user ?? payload, payload);
    const normalizedOrgs = payload.organizations
      ? normalizeOrganizationList(payload.organizations)
      : memoryState.organizations;
    const nextOrgId =
      payload.active_organization_id ??
      payload.current_organization_id ??
      payload.currentOrgId ??
      memoryState.currentOrgId ??
      normalizedOrgs[0]?.id ??
      null;

    patchMemoryState({
      user: normalizedUser ?? memoryState.user,
      organizations: normalizedOrgs,
      currentOrgId: nextOrgId,
    });
    return normalizedUser;
  } catch {
    return memoryState.user;
  }
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
        patchMemoryState({ hasBootstrapped: true });
        return null;
      }

      const headerCsrf = response.headers.get('X-CSRF-Token');
      const payload = (await response.json()) as AuthEndpointPayload;
      const nextToken = payload.access_token ?? payload.accessToken ?? null;

      if (!nextToken) {
        clearAuthSession();
        patchMemoryState({ hasBootstrapped: true });
        return null;
      }

      const nextOrgs = payload.organizations
        ? normalizeOrganizationList(payload.organizations)
        : memoryState.organizations;
      const nextUser = payload.user
        ? normalizeAuthUser(payload.user, payload)
        : memoryState.user;

      setAuthSession({
        accessToken: nextToken,
        csrfToken: payload.csrf_token ?? payload.csrfToken ?? headerCsrf ?? getCsrfToken(),
        user: nextUser,
        currentOrgId:
          payload.active_organization_id ??
          payload.current_organization_id ??
          payload.currentOrgId ??
          memoryState.currentOrgId ??
          nextOrgs[0]?.id ??
          null,
        organizations: nextOrgs,
        hasBootstrapped: true,
      });

      return nextToken;
    } catch {
      clearAuthSession();
      patchMemoryState({ hasBootstrapped: true });
      return null;
    } finally {
      patchMemoryState({ isRefreshing: false });
      inFlightRefreshPromise = null;
    }
  })();

  return inFlightRefreshPromise;
}

/**
 * 切换当前活跃 Organization：
 * 真实调用 POST /api/v1/organizations/{org_id}/switch，更新内存 Access Token、
 * 当前组织上下文与权限清单；若后端已返回新 Token 则仅存于内存。
 */
export async function switchActiveOrganization(
  orgId: string,
  fetchImpl: typeof fetch = globalThis.fetch,
): Promise<boolean> {
  const headers = new Headers({
    Accept: 'application/json',
    'Content-Type': 'application/json',
  });
  const token = getAccessToken();
  if (token) {
    headers.set('Authorization', `Bearer ${token}`);
  }
  const csrf = getCsrfToken();
  if (csrf) {
    headers.set('X-CSRF-Token', csrf);
  }
  if (memoryState.currentOrgId) {
    headers.set('X-Organization-Id', memoryState.currentOrgId);
  }

  const response = await fetchImpl(`/api/v1/organizations/${encodeURIComponent(orgId)}/switch`, {
    method: 'POST',
    credentials: 'include',
    headers,
  });

  if (!response.ok) {
    return false;
  }

  const headerCsrf = response.headers.get('X-CSRF-Token');
  const payload = (await response.json()) as AuthEndpointPayload;
  const nextToken = payload.access_token ?? payload.accessToken ?? memoryState.accessToken;
  const nextOrgs = payload.organizations
    ? normalizeOrganizationList(payload.organizations)
    : memoryState.organizations;
  const nextUser = payload.user
    ? normalizeAuthUser(payload.user, payload)
    : memoryState.user
      ? {
          ...memoryState.user,
          organizationRoles: payload.organization_roles
            ? normalizeCodeList(payload.organization_roles)
            : memoryState.user.organizationRoles,
          permissions: payload.permissions
            ? normalizeCodeList(payload.permissions)
            : memoryState.user.permissions,
        }
      : null;

  const resolvedOrgId =
    payload.active_organization_id ??
    payload.current_organization_id ??
    payload.organization_id ??
    orgId;

  if (nextToken) {
    setAuthSession({
      accessToken: nextToken,
      csrfToken: payload.csrf_token ?? payload.csrfToken ?? headerCsrf ?? getCsrfToken(),
      user: nextUser,
      currentOrgId: resolvedOrgId,
      organizations: nextOrgs,
      hasBootstrapped: true,
    });
  } else {
    patchMemoryState({
      currentOrgId: resolvedOrgId,
      user: nextUser,
      organizations: nextOrgs,
    });
  }

  // 若切换接口未完整返回权限清单，则补拉一次 /auth/me 同步组织级权限
  if (!payload.permissions && !payload.user?.permissions && getAccessToken()) {
    await fetchCurrentUserProfile(fetchImpl);
  }

  return true;
}

/**
 * 主动登出当前会话：调用 POST /api/v1/auth/logout 撤销服务端会话并清空内存态。
 */
export async function logoutSession(
  fetchImpl: typeof fetch = globalThis.fetch,
): Promise<void> {
  try {
    const headers = new Headers({ Accept: 'application/json' });
    const token = getAccessToken();
    if (token) {
      headers.set('Authorization', `Bearer ${token}`);
    }
    const csrf = getCsrfToken();
    if (csrf) {
      headers.set('X-CSRF-Token', csrf);
    }
    if (memoryState.currentOrgId) {
      headers.set('X-Organization-Id', memoryState.currentOrgId);
    }
    await fetchImpl('/api/v1/auth/logout', {
      method: 'POST',
      credentials: 'include',
      headers,
    });
  } catch {
    // 即使网络异常也必须强制清空本地内存会话（Fail-Closed）
  } finally {
    clearAuthSession();
    patchMemoryState({ hasBootstrapped: true });
  }
}

/** React Context 对外暴露的认证上下文接口 */
export interface AuthContextValue extends AuthSessionState {
  setSession: typeof setAuthSession;
  clearSession: typeof clearAuthSession;
  switchOrganization: (orgId: string) => Promise<boolean>;
  refreshSession: () => Promise<string | null>;
  refreshProfile: () => Promise<AuthUser | null>;
  logout: () => Promise<void>;
  hasPermission: (permissionCode: string) => boolean;
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
    return switchActiveOrganization(orgId);
  }, []);

  const refreshSession = useCallback(() => {
    return refreshAccessToken();
  }, []);

  const refreshProfile = useCallback(() => {
    return fetchCurrentUserProfile();
  }, []);

  const logout = useCallback(() => {
    return logoutSession();
  }, []);

  const checkPerm = useCallback(
    (permissionCode: string) => hasPermission(permissionCode, state),
    [state],
  );

  const value = useMemo<AuthContextValue>(
    () => ({
      ...state,
      setSession: setAuthSession,
      clearSession: clearAuthSession,
      switchOrganization,
      refreshSession,
      refreshProfile,
      logout,
      hasPermission: checkPerm,
    }),
    [state, switchOrganization, refreshSession, refreshProfile, logout, checkPerm],
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
