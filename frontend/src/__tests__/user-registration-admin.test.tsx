import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { UsersAdminTab } from '@/features/iam/UsersAdminTab';
import {
  AuthProvider,
  clearAuthSession,
  getAccessToken,
  resetAuthBootstrap,
  setAuthSession,
} from '@/lib/auth-store';
import { LoginPage } from '@/pages/LoginPage';

function createTestQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('账号自助注册与平台账号管理 UI 测试', () => {
  beforeEach(() => {
    clearAuthSession();
    resetAuthBootstrap();
    window.localStorage.clear();
    window.sessionStorage.clear();
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    clearAuthSession();
    resetAuthBootstrap();
  });

  it('登录页可切换至自助注册并提交 POST /api/v1/auth/register，令牌仅存内存不落盘', async () => {
    const storageSetSpy = vi.spyOn(Storage.prototype, 'setItem');
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith('/api/v1/auth/register')) {
        return jsonResponse(
          {
            access_token: 'jwt-registered-memory-only',
            csrf_token: 'csrf-reg-token',
            active_organization_id: 'org-acme',
            organizations: [{ id: 'org-acme', name: 'Acme Corp', slug: 'acme-corp', status: 'ACTIVE' }],
            user: {
              id: 'user-reg-1',
              email: 'alice@acme.local',
              display_name: '林晓晴',
              status: 'ACTIVE',
            },
          },
          201,
        );
      }
      if (url.endsWith('/api/v1/auth/me')) {
        return jsonResponse({
          user: { id: 'user-reg-1', email: 'alice@acme.local', display_name: '林晓晴', status: 'ACTIVE' },
          active_organization_id: 'org-acme',
          organizations: [{ id: 'org-acme', name: 'Acme Corp', slug: 'acme-corp', status: 'ACTIVE' }],
          system_roles: [],
          organization_roles: [{ id: 'role-emp', code: 'EMPLOYEE', name: '普通员工', scope: 'ORGANIZATION' }],
          permissions: ['chat:use'],
        });
      }
      return jsonResponse({});
    });

    const queryClient = createTestQueryClient();
    render(
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <MemoryRouter initialEntries={['/login']}>
            <LoginPage />
          </MemoryRouter>
        </AuthProvider>
      </QueryClientProvider>,
    );

    // 切换到「注册新账号」模式
    fireEvent.click(screen.getByRole('tab', { name: '注册新账号' }));
    expect(screen.getByText('注册 DocHub 企业账号')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('企业邮箱'), { target: { value: 'alice@acme.local' } });
    fireEvent.change(screen.getByLabelText('显示姓名'), { target: { value: '林晓晴' } });
    fireEvent.change(screen.getByLabelText('设置登录密码'), { target: { value: 'AlicePass#2026' } });
    fireEvent.change(screen.getByLabelText('确认登录密码'), { target: { value: 'AlicePass#2026' } });
    fireEvent.click(screen.getByRole('button', { name: /创建账号并登录/ }));

    await waitFor(() => {
      expect(getAccessToken()).toBe('jwt-registered-memory-only');
    });

    expect(storageSetSpy).not.toHaveBeenCalled();
    expect(fetchSpy).toHaveBeenCalledWith(
      '/api/v1/auth/register',
      expect.objectContaining({ method: 'POST' }),
    );
  });

  it('非 SYSTEM_ADMIN 查看平台账号管理选项卡时展示明确权限提示，不触发非法管理请求', () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    setAuthSession({
      accessToken: 'jwt-emp',
      user: {
        id: 'u-emp',
        email: 'employee@acme.local',
        displayName: '普通员工',
        systemRoles: [],
        organizationRoles: ['EMPLOYEE'],
        permissions: ['chat:use'],
      },
      currentOrgId: 'org-acme',
      organizations: [{ id: 'org-acme', name: 'Acme Corp', slug: 'acme-corp', status: 'ACTIVE', roleNames: ['EMPLOYEE'] }],
    });

    const queryClient = createTestQueryClient();
    render(
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <UsersAdminTab currentOrgId="org-acme" currentOrgName="Acme Corp" />
        </AuthProvider>
      </QueryClientProvider>,
    );

    expect(screen.getByText(/需要平台管理权限/)).toBeInTheDocument();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('SYSTEM_ADMIN 可加载全平台账号台账并对其他账号执行停用操作', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.startsWith('/api/v1/users') && (!init?.method || init.method === 'GET')) {
        return jsonResponse({
          items: [
            {
              id: 'u-admin',
              email: 'admin@acme.local',
              display_name: '系统管理员',
              status: 'ACTIVE',
              system_roles: ['SYSTEM_ADMIN'],
              organizations: [{ id: 'org-acme', name: 'Acme Corp', slug: 'acme-corp', member_status: 'ACTIVE' }],
              created_at: '2026-01-01T00:00:00Z',
            },
            {
              id: 'u-target',
              email: 'employee@acme.local',
              display_name: '普通员工',
              status: 'ACTIVE',
              system_roles: [],
              organizations: [{ id: 'org-acme', name: 'Acme Corp', slug: 'acme-corp', member_status: 'ACTIVE' }],
              created_at: '2026-01-02T00:00:00Z',
            },
          ],
          total: 2,
          offset: 0,
          limit: 100,
        });
      }
      if (url.endsWith('/api/v1/users/u-target') && init?.method === 'PATCH') {
        return jsonResponse({
          id: 'u-target',
          email: 'employee@acme.local',
          display_name: '普通员工',
          status: 'DISABLED',
          system_roles: [],
          organizations: [{ id: 'org-acme', name: 'Acme Corp', slug: 'acme-corp', member_status: 'ACTIVE' }],
          created_at: '2026-01-02T00:00:00Z',
        });
      }
      return jsonResponse({});
    });

    setAuthSession({
      accessToken: 'jwt-sys-admin',
      user: {
        id: 'u-admin',
        email: 'admin@acme.local',
        displayName: '系统管理员',
        systemRoles: ['SYSTEM_ADMIN'],
        organizationRoles: ['ORG_ADMIN'],
        permissions: ['system:user:manage', 'org:manage'],
      },
      currentOrgId: 'org-acme',
      organizations: [{ id: 'org-acme', name: 'Acme Corp', slug: 'acme-corp', status: 'ACTIVE', roleNames: ['ORG_ADMIN'] }],
    });

    const queryClient = createTestQueryClient();
    render(
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <UsersAdminTab currentOrgId="org-acme" currentOrgName="Acme Corp" />
        </AuthProvider>
      </QueryClientProvider>,
    );

    await waitFor(() => {
      expect(screen.getByText('employee@acme.local')).toBeInTheDocument();
    });

    const disableButtons = screen.getAllByRole('button', { name: /停用/ });
    // 第一行是自己（按钮禁用），第二行是普通员工（可点击）
    expect(disableButtons).toHaveLength(2);
    expect(disableButtons[0]).toBeDisabled();
    expect(disableButtons[1]).not.toBeDisabled();

    fireEvent.click(disableButtons[1]!);
    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalledWith(
        '/api/v1/users/u-target',
        expect.objectContaining({ method: 'PATCH' }),
      );
    });
  });
});
