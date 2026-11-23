import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AppRoutes } from '@/App';
import {
  AuthProvider,
  clearAuthSession,
  resetAuthBootstrap,
  setAuthSession,
} from '@/lib/auth-store';

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

function seedKnowledgeManagerSession() {
  setAuthSession({
    accessToken: 'jwt-slice23-km-memory-token',
    csrfToken: 'csrf-slice23',
    currentOrgId: 'org-acme',
    organizations: [
      { id: 'org-acme', name: 'Acme Corp', slug: 'acme-corp', status: 'ACTIVE', roleNames: ['KNOWLEDGE_MANAGER'] },
    ],
    user: {
      id: 'u-km',
      email: 'km@acme.local',
      displayName: '知识管理员',
      systemRoles: [],
      organizationRoles: ['KNOWLEDGE_MANAGER'],
      permissions: [
        'space:manage',
        'document:create',
        'document:manage',
        'acl:write',
        'org:manage',
      ],
    },
  });
}

describe('Slice 2 & 3：Knowledge Space、文档中心（Quarantine/BLOCKED_POLICY）与四维 ACL 测试', () => {
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

  it('Knowledge Space 页面正确渲染公开/私有边界、表单校验与私有 Space 成员管理', async () => {
    seedKnowledgeManagerSession();

    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input);
      const method = (init?.method ?? 'GET').toUpperCase();

      if (url.endsWith('/api/v1/readyz')) return jsonResponse({ status: 'ready' });
      if (url.endsWith('/api/v1/organizations/org-acme/spaces') && method === 'GET') {
        return jsonResponse({
          items: [
            {
              id: 'sp-hr',
              name: 'HR 机密空间',
              description: '高管薪酬与绩效细则',
              visibility: 'PRIVATE',
              answer_policy: 'STRICT_EVIDENCE',
              embedding_profile_name: 'Qwen-v3',
              status: 'ACTIVE',
              config_version: 2,
            },
            {
              id: 'sp-pub',
              name: '通用制度空间',
              visibility: 'PUBLIC',
              answer_policy: 'GROUNDED_WITH_GENERAL_CONTEXT',
              status: 'ACTIVE',
              config_version: 1,
            },
          ],
        });
      }
      if (url.endsWith('/api/v1/spaces/sp-hr/members') && method === 'GET') {
        return jsonResponse({
          items: [
            {
              id: 'sm-1',
              space_id: 'sp-hr',
              user_id: 'u-hr',
              user_email: 'hr@acme.local',
              user_display_name: '林人事',
              status: 'ACTIVE',
              valid_until: '2027-01-01T00:00:00Z',
            },
          ],
        });
      }
      if (url.includes('/api/v1/organizations/org-acme/members') && method === 'GET') {
        return jsonResponse({
          items: [
            { id: 'm-1', user_id: 'u-hr', email: 'hr@acme.local', display_name: '林人事', status: 'ACTIVE' },
            { id: 'm-2', user_id: 'u-emp', email: 'employee@acme.local', display_name: '普通员工', status: 'ACTIVE' },
          ],
        });
      }
      if (url.endsWith('/api/v1/spaces/sp-hr/members') && method === 'PATCH') {
        return jsonResponse({ items: [] });
      }
      return jsonResponse({});
    });

    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <AuthProvider>
          <MemoryRouter initialEntries={['/spaces']}>
            <AppRoutes />
          </MemoryRouter>
        </AuthProvider>
      </QueryClientProvider>,
    );

    // 验证私有 Space 列表与显式成员加载
    await waitFor(() => {
      expect(screen.getByText('HR 机密空间')).toBeInTheDocument();
      expect(screen.getByText('林人事')).toBeInTheDocument();
    });

    // 验证新建 Space 表单 Zod 校验
    fireEvent.click(screen.getByRole('button', { name: /新建 Knowledge Space/ }));
    fireEvent.change(screen.getByLabelText('Space 名称'), { target: { value: 'A' } });
    fireEvent.click(screen.getByRole('button', { name: '确认创建' }));
    await waitFor(() => {
      expect(screen.getByText('Space 名称不少于 2 个字符')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole('button', { name: '取消' }));

    // 切换选中 PUBLIC Space，验证“非跨组织公开”提示
    fireEvent.click(screen.getByText('通用制度空间'));
    await waitFor(() => {
      expect(screen.getByText(/绝不代表跨组织或互联网公开/)).toBeInTheDocument();
    });

    expect(fetchSpy).toHaveBeenCalledWith(
      '/api/v1/spaces/sp-hr/members',
      expect.objectContaining({ method: 'GET' }),
    );
  });

  it('文档中心正确渲染 QUARANTINED 标识与 L2/L3 BLOCKED_POLICY 五阶段真实状态', async () => {
    seedKnowledgeManagerSession();

    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith('/api/v1/readyz')) return jsonResponse({ status: 'ready' });
      if (url.endsWith('/api/v1/organizations/org-acme/spaces')) {
        return jsonResponse({
          items: [{ id: 'sp-fin', name: '财务合规空间', visibility: 'PRIVATE', status: 'ACTIVE' }],
        });
      }
      if (url.startsWith('/api/v1/documents?')) {
        return jsonResponse({
          total: 1,
          items: [
            {
              id: 'doc-secret-pdf',
              space_id: 'sp-fin',
              space_name: '财务合规空间',
              title: '2026_高管薪酬扫描件.pdf',
              source_type: 'UPLOAD',
              status: 'QUARANTINED',
              current_version_id: 'ver-001',
              current_version_no: 1,
              sensitivity_level: 'L3',
              acl_configured: false,
              latest_parse_status: 'BLOCKED_POLICY',
            },
          ],
        });
      }
      if (url.endsWith('/api/v1/documents/doc-secret-pdf/versions')) {
        return jsonResponse({
          items: [
            {
              id: 'ver-001',
              version_no: 1,
              filename: '2026_高管薪酬扫描件.pdf',
              mime_type: 'application/pdf',
              size_bytes: 204800,
              file_sha256: 'abc123',
            },
          ],
        });
      }
      if (url.endsWith('/api/v1/document-versions/ver-001/processing-status')) {
        return jsonResponse({
          document_id: 'doc-secret-pdf',
          version_id: 'ver-001',
          acl_configured: false,
          can_publish: false,
          import_job: { status: 'QUARANTINED' },
          parse_job: {
            status: 'BLOCKED_POLICY',
            parser_provider: 'LocalOCRParserAdapter',
            error_code: 'L3_EGRESS_FORBIDDEN',
            error_detail: '本地 OCR 未开启，严禁外发 MinerU 云服务。',
          },
          chunking_job: { status: 'QUEUED' },
          embedding_job: { status: 'QUEUED' },
          index_job: { status: 'CREATED' },
          blocks: [],
          chunks: [],
        });
      }
      return jsonResponse({});
    });

    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <AuthProvider>
          <MemoryRouter initialEntries={['/documents']}>
            <AppRoutes />
          </MemoryRouter>
        </AuthProvider>
      </QueryClientProvider>,
    );

    await waitFor(() => {
      expect(screen.getByText('2026_高管薪酬扫描件.pdf')).toBeInTheDocument();
      expect(screen.getByText('QUARANTINED（未授权不可检索）')).toBeInTheDocument();
      expect(screen.getByText('禁外发 MinerU')).toBeInTheDocument();
    });

    await waitFor(() => {
      expect(
        screen.getByText(/安全策略拦截：BLOCKED_POLICY（L2\/L3 禁止默认外发 MinerU）/),
      ).toBeInTheDocument();
      expect(screen.getAllByText(/L3_EGRESS_FORBIDDEN/).length).toBeGreaterThanOrEqual(1);
      expect(screen.getByText(/文档处于 Quarantine（待授权隔离态）/)).toBeInTheDocument();
    });
  });

  it('四维 ACL 编辑器校验必填 reason、屏蔽 SystemRole 并正确处理 expected_revision 409 冲突', async () => {
    seedKnowledgeManagerSession();

    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input);
      const method = (init?.method ?? 'GET').toUpperCase();

      if (url.endsWith('/api/v1/readyz')) return jsonResponse({ status: 'ready' });
      if (url.startsWith('/api/v1/documents?')) {
        return jsonResponse({
          total: 1,
          items: [
            {
              id: 'doc-travel',
              space_id: 'sp-fin',
              title: '差旅报销规范.md',
              status: 'ACTIVE',
              sensitivity_level: 'L1',
            },
          ],
        });
      }
      if (url.endsWith('/api/v1/organizations/org-acme/departments')) {
        return jsonResponse({
          items: [{ id: 'dept-fin', parent_id: null, name: '财务部', status: 'ACTIVE' }],
        });
      }
      if (url.endsWith('/api/v1/organizations/org-acme/roles')) {
        return jsonResponse({
          system_roles: [{ id: 'sr-sysadmin', code: 'SYSTEM_ADMIN', name: '系统管理员', scope: 'SYSTEM' }],
          organization_roles: [
            { id: 'or-hr', code: 'HR', name: '人力资源角色', scope: 'ORGANIZATION', permissions: [] },
          ],
        });
      }
      if (url.includes('/api/v1/organizations/org-acme/members')) {
        return jsonResponse({
          items: [
            { id: 'm-1', user_id: 'u-alice', email: 'alice@acme.local', display_name: 'Alice', status: 'ACTIVE' },
          ],
        });
      }
      if (url.endsWith('/api/v1/documents/doc-travel/acl') && method === 'GET') {
        return jsonResponse({
          document_id: 'doc-travel',
          revision: 3,
          status: 'ACTIVE',
          grants: [{ subject_type: 'ROLE', subject_id: 'or-hr' }],
        });
      }
      if (url.endsWith('/api/v1/documents/doc-travel/acl/revisions') && method === 'GET') {
        return jsonResponse({
          items: [
            {
              id: 'rev-3',
              revision: 3,
              changed_by_email: 'km@acme.local',
              reason: '初始向 HR 开放差旅制度',
              created_at: '2026-11-23T10:00:00Z',
              diff: { added: ['ROLE:HR'], removed: [] },
            },
          ],
        });
      }
      if (url.endsWith('/api/v1/documents/doc-travel/acl/revisions') && method === 'POST') {
        return jsonResponse(
          { code: 'ACL_REVISION_CONFLICT', message: 'expected_revision=3 与当前数据库版本 4 不一致' },
          409,
        );
      }
      return jsonResponse({});
    });

    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <AuthProvider>
          <MemoryRouter initialEntries={['/acl?documentId=doc-travel']}>
            <AppRoutes />
          </MemoryRouter>
        </AuthProvider>
      </QueryClientProvider>,
    );

    // 等待 ACL 加载并确认屏蔽 SystemRole（仅渲染 OrganizationRole）
    await waitFor(() => {
      expect(screen.getByTestId('acl-current-revision')).toHaveTextContent('当前 Revision #3');
      expect(screen.getByText('人力资源角色')).toBeInTheDocument();
      expect(screen.getByText('初始向 HR 开放差旅制度')).toBeInTheDocument();
    });
    expect(screen.queryByText('系统管理员')).not.toBeInTheDocument();

    // 1) 未填写 reason 时拦截提交
    fireEvent.click(screen.getByRole('button', { name: /提交 ACL Revision/ }));
    await waitFor(() => {
      expect(screen.getByText(/变更理由（reason）为必填审计字段/)).toBeInTheDocument();
    });

    // 2) 填写 reason 后提交，验证携带 expected_revision=3 与 Idempotency-Key，并正确展示 409 冲突刷新提示
    fireEvent.change(screen.getByLabelText(/变更理由/), {
      target: { value: '同步财务部门授权' },
    });
    fireEvent.click(screen.getByRole('button', { name: /提交 ACL Revision/ }));

    await waitFor(() => {
      expect(screen.getByText(/乐观锁冲突（expected_revision 已过期）/)).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /立即刷新最新 ACL Revision/ })).toBeInTheDocument();
    });

    const postCall = fetchSpy.mock.calls.find(
      ([url, init]) =>
        String(url).endsWith('/api/v1/documents/doc-travel/acl/revisions') &&
        (init?.method ?? 'GET').toUpperCase() === 'POST',
    );
    expect(postCall).toBeDefined();
    const postHeaders = new Headers(postCall?.[1]?.headers);
    expect(postHeaders.get('Idempotency-Key')).toBeTruthy();
    expect(JSON.parse(String(postCall?.[1]?.body))).toMatchObject({
      expected_revision: 3,
      reason: '同步财务部门授权',
    });
  });
});
