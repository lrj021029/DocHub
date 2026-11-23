import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AppRoutes } from '@/App';
import {
  AuthProvider,
  clearAuthSession,
  getAccessToken,
  getCsrfToken,
  getCurrentOrgId,
  resetAuthBootstrap,
  setAuthSession,
  switchActiveOrganization,
} from '@/lib/auth-store';

/** 创建测试隔离的 QueryClient（关闭自动重试以便精确断言错误态） */
function createTestQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
}

/** 构造标准 JSON Response */
function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('Slice 1：登录认证闭环、多组织切换与 IAM 管理台测试', () => {
  beforeEach(() => {
    clearAuthSession();
    resetAuthBootstrap();
    window.localStorage.clear();
    window.sessionStorage.clear();
    document.cookie = 'dochub_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/';
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    clearAuthSession();
    resetAuthBootstrap();
  });

  it('登录表单提交后 Access Token 仅存于内存不在 localStorage，并自动跳转首页', async () => {
    const storageSetSpy = vi.spyOn(Storage.prototype, 'setItem');

    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith('/api/v1/auth/login')) {
        return jsonResponse({
          access_token: 'jwt-slice1-in-memory-token',
          csrf_token: 'csrf-slice1-value',
          active_organization_id: 'org-acme',
          organizations: [
            { id: 'org-acme', name: 'Acme Corp', slug: 'acme-corp', status: 'ACTIVE' },
            { id: 'org-beta', name: 'Beta Corp', slug: 'beta-corp', status: 'ACTIVE' },
          ],
          user: {
            id: 'u-admin',
            email: 'admin@acme.local',
            display_name: '平台管理员',
            system_roles: ['SYSTEM_ADMIN'],
            organization_roles: ['ORG_ADMIN'],
            permissions: ['org:manage', 'org:members:write', 'org:departments:write', 'org:roles:write'],
          },
        });
      }
      if (url.endsWith('/api/v1/readyz')) {
        return jsonResponse({ status: 'ready' });
      }
      return jsonResponse({});
    });

    const queryClient = createTestQueryClient();
    render(
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <MemoryRouter initialEntries={['/login']}>
            <AppRoutes />
          </MemoryRouter>
        </AuthProvider>
      </QueryClientProvider>,
    );

    fireEvent.change(screen.getByLabelText('企业邮箱'), {
      target: { value: 'admin@acme.local' },
    });
    fireEvent.change(screen.getByLabelText('登录密码'), {
      target: { value: 'SecurePass#2026' },
    });
    fireEvent.click(screen.getByRole('button', { name: /安全登录/ }));

    // 登录成功后跳转至总览工作台并展示用户邮箱与角色徽章
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: /知识治理总览/ })).toBeInTheDocument();
    });

    expect(getAccessToken()).toBe('jwt-slice1-in-memory-token');
    expect(getCsrfToken()).toBe('csrf-slice1-value');
    expect(getCurrentOrgId()).toBe('org-acme');
    expect(screen.getByTestId('user-menu')).toHaveTextContent('admin@acme.local');
    expect(screen.getByTestId('user-menu')).toHaveTextContent('SYSTEM_ADMIN');

    // 严格断言 Access Token 绝不写入 localStorage / sessionStorage
    expect(storageSetSpy).not.toHaveBeenCalled();
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
    expect(fetchSpy).toHaveBeenCalledWith(
      '/api/v1/auth/login',
      expect.objectContaining({ method: 'POST', credentials: 'include' }),
    );
  });

  it('未登录访问受保护路由前先尝试一次静默 /auth/refresh，失败后自动跳转 /login', async () => {
    // 验证兼容读取 dochub_csrf Cookie
    document.cookie = 'dochub_csrf=cookie-csrf-token-99; path=/';

    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith('/api/v1/auth/refresh')) {
        return jsonResponse({ detail: 'refresh session expired' }, 401);
      }
      return jsonResponse({});
    });

    const queryClient = createTestQueryClient();
    render(
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <MemoryRouter initialEntries={['/org-admin']}>
            <AppRoutes />
          </MemoryRouter>
        </AuthProvider>
      </QueryClientProvider>,
    );

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: '登录 DocHub 知识平台' })).toBeInTheDocument();
    });

    const refreshCall = fetchSpy.mock.calls.find(([url]) => String(url).endsWith('/api/v1/auth/refresh'));
    expect(refreshCall).toBeDefined();
    const refreshHeaders = new Headers(refreshCall?.[1]?.headers);
    expect(refreshHeaders.get('X-CSRF-Token')).toBe('cookie-csrf-token-99');
  });

  it('组织切换调用 POST /api/v1/organizations/{org_id}/switch 更新内存 Token 与上下文，登出调用 /auth/logout', async () => {
    setAuthSession({
      accessToken: 'token-org-acme',
      csrfToken: 'csrf-acme',
      currentOrgId: 'org-acme',
      organizations: [
        { id: 'org-acme', name: 'Acme Corp', slug: 'acme-corp', status: 'ACTIVE', roleNames: ['HR'] },
        { id: 'org-beta', name: 'Beta Corp', slug: 'beta-corp', status: 'ACTIVE', roleNames: ['EMPLOYEE'] },
      ],
      user: {
        id: 'u-1',
        email: 'hr@acme.local',
        displayName: 'HR 专员',
        systemRoles: [],
        organizationRoles: ['HR'],
        permissions: ['org:manage'],
      },
    });

    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith('/api/v1/organizations/org-beta/switch')) {
        return jsonResponse({
          access_token: 'token-org-beta-new',
          csrf_token: 'csrf-beta-rotated',
          active_organization_id: 'org-beta',
          organization_roles: ['EMPLOYEE'],
          permissions: ['document:read'],
        });
      }
      if (url.endsWith('/api/v1/auth/logout')) {
        return jsonResponse({ ok: true });
      }
      if (url.endsWith('/api/v1/readyz')) {
        return jsonResponse({ status: 'ready' });
      }
      return jsonResponse({});
    });

    const switched = await switchActiveOrganization('org-beta');
    expect(switched).toBe(true);
    expect(getAccessToken()).toBe('token-org-beta-new');
    expect(getCurrentOrgId()).toBe('org-beta');
    expect(window.localStorage.length).toBe(0);

    // 验证 AppShell 点击退出调用 POST /api/v1/auth/logout 并清空内存会话
    const queryClient = createTestQueryClient();
    render(
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <MemoryRouter initialEntries={['/']}>
            <AppRoutes />
          </MemoryRouter>
        </AuthProvider>
      </QueryClientProvider>,
    );

    fireEvent.click(screen.getByRole('button', { name: /退出/ }));

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: '登录 DocHub 知识平台' })).toBeInTheDocument();
    });
    expect(getAccessToken()).toBeNull();
    expect(fetchSpy).toHaveBeenCalledWith(
      '/api/v1/auth/logout',
      expect.objectContaining({ method: 'POST', credentials: 'include' }),
    );
  });

  it('组织管理页正确渲染部门树与角色管理界面，并执行表单校验与 SystemRole 分离提示', async () => {
    setAuthSession({
      accessToken: 'token-iam-admin',
      csrfToken: 'csrf-iam',
      currentOrgId: 'org-acme',
      organizations: [
        { id: 'org-acme', name: 'Acme Corp', slug: 'acme-corp', status: 'ACTIVE', roleNames: ['ORG_ADMIN'] },
      ],
      user: {
        id: 'u-admin',
        email: 'admin@acme.local',
        displayName: '系统管理员',
        systemRoles: ['SYSTEM_ADMIN'],
        organizationRoles: ['ORG_ADMIN'],
        permissions: ['org:manage', 'org:members:write', 'org:departments:write', 'org:roles:write'],
      },
    });

    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input);
      const method = (init?.method ?? 'GET').toUpperCase();

      if (url.endsWith('/api/v1/readyz')) return jsonResponse({ status: 'ready' });
      if (url.includes('/members') && method === 'GET') {
        return jsonResponse({
          items: [
            {
              id: 'm-1',
              user_id: 'u-hr',
              email: 'hr@acme.local',
              display_name: '林人事',
              status: 'ACTIVE',
              organization_roles: ['HR'],
              system_roles: [],
            },
          ],
        });
      }
      if (url.endsWith('/departments') && method === 'GET') {
        return jsonResponse({
          items: [
            { id: 'dept-hq', parent_id: null, name: '总部', status: 'ACTIVE' },
            { id: 'dept-hr', parent_id: 'dept-hq', name: '人力资源部', status: 'ACTIVE' },
            { id: 'dept-fin', parent_id: 'dept-hq', name: '财务部', status: 'ACTIVE' },
          ],
        });
      }
      if (url.includes('/memberships') && method === 'GET') {
        return jsonResponse({
          items: [
            {
              id: 'dm-1',
              department_id: 'dept-hq',
              user_id: 'u-hr',
              user_email: 'hr@acme.local',
              user_display_name: '林人事',
              membership_type: 'FULL_TIME',
              is_primary: true,
              status: 'ACTIVE',
            },
          ],
        });
      }
      if (url.endsWith('/roles') && method === 'GET') {
        return jsonResponse({
          system_roles: [{ id: 'sr-1', code: 'SYSTEM_ADMIN', name: '平台系统管理员', scope: 'SYSTEM' }],
          organization_roles: [
            {
              id: 'or-hr',
              code: 'HR',
              name: '人力资源角色',
              scope: 'ORGANIZATION',
              permissions: ['document:read', 'faq:review'],
            },
          ],
        });
      }
      if (url.endsWith('/api/v1/permissions') && method === 'GET') {
        return jsonResponse({
          items: [
            { code: 'document:create', description: '导入与创建文档', scope: 'ORGANIZATION' },
            { code: 'system:config', description: '平台系统参数配置', scope: 'SYSTEM' },
          ],
        });
      }
      return jsonResponse({});
    });

    const queryClient = createTestQueryClient();
    render(
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <MemoryRouter initialEntries={['/org-admin']}>
            <AppRoutes />
          </MemoryRouter>
        </AuthProvider>
      </QueryClientProvider>,
    );

    // 1) 验证成员管理 Tab 渲染真实成员
    await waitFor(() => {
      expect(screen.getByText('林人事')).toBeInTheDocument();
    });

    // 2) 切换至部门树 Tab，验证树形结构渲染与新建部门表单 Zod 校验
    fireEvent.click(screen.getByRole('tab', { name: /部门树与多部门兼职/ }));
    await waitFor(() => {
      expect(screen.getByRole('tree', { name: '组织部门树' })).toBeInTheDocument();
      expect(screen.getByText('人力资源部')).toBeInTheDocument();
      expect(screen.getByText('财务部')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: /新建部门/ }));
    fireEvent.change(screen.getByLabelText('部门名称'), { target: { value: 'A' } });
    fireEvent.click(screen.getByRole('button', { name: '确认创建' }));
    await waitFor(() => {
      expect(screen.getByText('部门名称不少于 2 个字符')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole('button', { name: '取消' }));

    // 3) 切换至组织角色与权限树 Tab，验证 SystemRole / OrganizationRole 视觉分离与角色代码校验
    fireEvent.click(screen.getByRole('tab', { name: /组织角色与权限树/ }));
    await waitFor(() => {
      expect(screen.getByText(/绝不自动拥有任何文档读取权限/)).toBeInTheDocument();
      expect(screen.getByText('人力资源角色')).toBeInTheDocument();
      expect(screen.getByText('document:create')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: /新建组织角色/ }));
    fireEvent.change(screen.getByLabelText('角色代码'), { target: { value: 'invalid-code' } });
    fireEvent.change(screen.getByLabelText('角色名称'), { target: { value: '测试角色' } });
    fireEvent.click(screen.getByRole('button', { name: '创建角色' }));
    await waitFor(() => {
      expect(screen.getByText(/请使用大写字母、数字与下划线/)).toBeInTheDocument();
    });
  });

  it('后端返回 HTTP 403 无权限时，IAM 页面展示清晰的无权操作警示且不暴露管理写按钮', async () => {
    setAuthSession({
      accessToken: 'token-ordinary-employee',
      csrfToken: 'csrf-emp',
      currentOrgId: 'org-acme',
      organizations: [
        { id: 'org-acme', name: 'Acme Corp', slug: 'acme-corp', status: 'ACTIVE', roleNames: ['EMPLOYEE'] },
      ],
      user: {
        id: 'u-emp',
        email: 'employee@acme.local',
        displayName: '普通员工',
        systemRoles: [],
        organizationRoles: ['EMPLOYEE'],
        permissions: ['chat:ask'],
      },
    });

    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith('/api/v1/readyz')) return jsonResponse({ status: 'ready' });
      if (url.includes('/api/v1/organizations/org-acme/members')) {
        return jsonResponse(
          { code: 'PERMISSION_DENIED', detail: '缺少 org:members:read 权限' },
          403,
        );
      }
      return jsonResponse({});
    });

    const queryClient = createTestQueryClient();
    render(
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <MemoryRouter initialEntries={['/org-admin']}>
            <AppRoutes />
          </MemoryRouter>
        </AuthProvider>
      </QueryClientProvider>,
    );

    // 无权限用户不展示管理写按钮，并清晰呈现后端 403 拒绝提示
    expect(screen.queryByRole('button', { name: /邀请 \/ 添加成员/ })).not.toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(/无权限执行此操作（HTTP 403）/);
    });
  });
});
