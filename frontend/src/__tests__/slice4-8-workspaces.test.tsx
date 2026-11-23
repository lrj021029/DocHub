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

function createSseStream(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const c of chunks) {
        controller.enqueue(encoder.encode(c));
      }
      controller.close();
    },
  });
}

function seedAuthenticatedUser(userId = 'u-km') {
  setAuthSession({
    accessToken: 'jwt-slice48-memory-token',
    csrfToken: 'csrf-slice48',
    currentOrgId: 'org-acme',
    organizations: [
      { id: 'org-acme', name: 'Acme Corp', slug: 'acme-corp', status: 'ACTIVE', roleNames: ['KNOWLEDGE_MANAGER'] },
    ],
    user: {
      id: userId,
      email: 'km@acme.local',
      displayName: '知识管理员',
      systemRoles: [],
      organizationRoles: ['KNOWLEDGE_MANAGER'],
      permissions: ['*'],
    },
  });
}

describe('Slice 4–8：Notion 风格 AI 问答、FAQ、Knowledge Gap、治理与安全测试', () => {
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

  it('AI 问答工作台支持历史会话失权段落隐藏、POST SSE 流式渲染与点击引用实时重鉴权', async () => {
    seedAuthenticatedUser('u-employee');

    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input);
      const method = (init?.method ?? 'GET').toUpperCase();

      if (url.endsWith('/api/v1/readyz')) return jsonResponse({ status: 'ready' });
      if (url.endsWith('/api/v1/organizations/org-acme/spaces')) {
        return jsonResponse({
          items: [{ id: 'sp-fin', name: '财务制度空间', visibility: 'PUBLIC', status: 'ACTIVE' }],
        });
      }
      if (url.endsWith('/api/v1/chat/conversations') && method === 'GET') {
        return jsonResponse({
          items: [{ id: 'conv-1', title: '差旅与薪酬制度咨询', space_ids: ['sp-fin'] }],
        });
      }
      if (url.endsWith('/api/v1/chat/conversations/conv-1') && method === 'GET') {
        return jsonResponse({
          id: 'conv-1',
          title: '差旅与薪酬制度咨询',
          space_ids: ['sp-fin'],
          messages: [
            {
              id: 'msg-hist-1',
              role: 'assistant',
              segments: [
                {
                  id: 'seg-allowed',
                  sequence: 1,
                  content: '普通员工出差一线城市住宿上限 800 元/晚。',
                  redacted: false,
                },
                {
                  id: 'seg-revoked',
                  sequence: 2,
                  content: '绝密高管股权激励正文不应出现在前端',
                  redacted: true,
                  redacted_reason: '该段落来源文档权限已撤销，已隐藏正文。',
                },
              ],
              citations: [
                {
                  citation_id: 'cit-travel-1',
                  document_title: '差旅报销规范.pdf',
                  page: 4,
                },
              ],
            },
          ],
        });
      }
      if (url.endsWith('/api/v1/citations/cit-travel-1') && method === 'GET') {
        return jsonResponse({
          citation_id: 'cit-travel-1',
          document_title: '差旅报销规范.pdf',
          page_number: 4,
          heading_path: '第三章 住宿报销标准',
          quote_text: '一线城市住宿费用报销上限为人民币 800 元/晚。',
          bbox: [0.1, 0.2, 0.7, 0.15],
          acl_revision: 5,
          authorized: true,
        });
      }
      if (url.endsWith('/api/v1/chat/conversations/conv-1/messages:stream') && method === 'POST') {
        return new Response(
          createSseStream([
            'event: message_start\ndata: {"message_id":"msg-live-1"}\n\n',
            'event: retrieval_started\ndata: {"space_ids":["sp-fin"]}\n\n',
            'event: retrieval_completed\ndata: {"authorized_count":1}\n\n',
            'event: citation\ndata: {"citation_id":"cit-live-1","document_title":"差旅报销规范.pdf","page":5}\n\n',
            'event: token\ndata: {"text":"机场往返打车需保留电子发票。"}\n\n',
            'event: message_complete\ndata: {"usage":{"prompt_tokens":100,"completion_tokens":20}}\n\n',
          ]),
          { status: 200, headers: { 'Content-Type': 'text/event-stream' } },
        );
      }
      return jsonResponse({});
    });

    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <AuthProvider>
          <MemoryRouter initialEntries={['/chat']}>
            <AppRoutes />
          </MemoryRouter>
        </AuthProvider>
      </QueryClientProvider>,
    );

    // 1) 历史会话失权段落展示锁定 Callout，且绝不泄露被隐藏正文
    await waitFor(() => {
      expect(screen.getByText(/普通员工出差一线城市住宿上限 800 元\/晚/)).toBeInTheDocument();
      expect(screen.getByText('已撤权回答片段已脱敏隐藏')).toBeInTheDocument();
    });
    expect(screen.queryByText(/绝密高管股权激励正文不应出现在前端/)).not.toBeInTheDocument();

    // 2) 点击引用卡片触发 GET /api/v1/citations/{id} 重鉴权并展示页码高亮框
    fireEvent.click(screen.getByRole('button', { name: /差旅报销规范\.pdf/ }));
    await waitFor(() => {
      expect(screen.getByText('第三章 住宿报销标准 · 第 4 页')).toBeInTheDocument();
      expect(screen.getByTestId('citation-bbox-highlight')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole('button', { name: '关闭' }));

    // 3) 发送实时 SSE 问答并验证 URL 不包含 JWT 且 Header 携带 Bearer
    fireEvent.change(screen.getByLabelText('输入知识库提问内容'), {
      target: { value: '机场打车如何报销？' },
    });
    fireEvent.click(screen.getByRole('button', { name: /发送/ }));

    await waitFor(() => {
      expect(screen.getByTestId('live-sse-answer')).toHaveTextContent(
        '机场往返打车需保留电子发票。',
      );
    });

    const sseCall = fetchSpy.mock.calls.find(([url]) => String(url).endsWith('messages:stream'));
    expect(sseCall).toBeDefined();
    expect(String(sseCall?.[0])).not.toContain('jwt-slice48-memory-token');
    expect(new Headers(sseCall?.[1]?.headers).get('Authorization')).toBe(
      'Bearer jwt-slice48-memory-token',
    );
  });

  it('FAQ 挖掘禁自审提示与已发布 FAQ 三轴解耦状态（PUBLISHED + STALE）正确渲染', async () => {
    seedAuthenticatedUser('u-creator-01');

    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith('/api/v1/readyz')) return jsonResponse({ status: 'ready' });
      if (url.endsWith('/api/v1/faq-candidates')) {
        return jsonResponse({
          items: [
            {
              id: 'cand-1',
              question: '生鲜破损如何赔付？',
              frequency_count: 14,
              unique_user_count: 9,
              confidence_score: 0.91,
              lifecycle_state: 'PENDING_REVIEW',
              source_citation_count: 2,
              created_by: 'u-creator-01',
            },
          ],
        });
      }
      if (url.endsWith('/api/v1/faq')) {
        return jsonResponse({
          items: [
            {
              id: 'faq-pub-1',
              question: '生鲜破损 48 小时理赔标准',
              version_no: 3,
              lifecycle_state: 'ARCHIVED',
              publication_state: 'PUBLISHED',
              source_health_state: 'STALE',
              is_manual: false,
              cache_status: 'INVALIDATED_PENDING_REVALIDATE',
              cache_hit_count: 42,
              acl_revision: 4,
            },
          ],
        });
      }
      return jsonResponse({});
    });

    const qc = createTestQueryClient();
    const { unmount } = render(
      <QueryClientProvider client={qc}>
        <AuthProvider>
          <MemoryRouter initialEntries={['/faq-mining']}>
            <AppRoutes />
          </MemoryRouter>
        </AuthProvider>
      </QueryClientProvider>,
    );

    // 验证创建者禁止审核自己提交的 FAQ
    await waitFor(() => {
      expect(screen.getByText('生鲜破损如何赔付？')).toBeInTheDocument();
      expect(screen.getByText(/创建者禁止审核自己创建或提交的 FAQ/)).toBeInTheDocument();
      expect(screen.getByRole('button', { name: '创建者禁自审' })).toBeDisabled();
    });
    unmount();

    // 验证已发布 FAQ 独立展示 Lifecycle / Publication / Source Health 三轴（PUBLISHED 与 STALE 并存）
    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <AuthProvider>
          <MemoryRouter initialEntries={['/faq-published']}>
            <AppRoutes />
          </MemoryRouter>
        </AuthProvider>
      </QueryClientProvider>,
    );

    await waitFor(() => {
      expect(screen.getByText('生鲜破损 48 小时理赔标准')).toBeInTheDocument();
      expect(screen.getByText('PUBLISHED')).toBeInTheDocument();
      expect(screen.getByText('STALE')).toBeInTheDocument();
      expect(screen.getByText('ARCHIVED')).toBeInTheDocument();
    });
  });

  it('Knowledge Gap 展示 11 态标签与补充任务回归闭环，且 Settings 绝不回显明文 API Key', async () => {
    seedAuthenticatedUser('u-km');

    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input);
      const method = (init?.method ?? 'GET').toUpperCase();

      if (url.endsWith('/api/v1/readyz')) return jsonResponse({ status: 'ready' });
      if (url.endsWith('/api/v1/knowledge-gaps') && method === 'GET') {
        return jsonResponse({
          items: [
            {
              id: 'gap-1',
              topic: '海外直邮保税仓清关延误赔付标准',
              event_type: 'INSUFFICIENT_AUTHORIZED_EVIDENCE',
              occurrence_count: 11,
              status: 'IN_PROGRESS',
              priority: 'HIGH',
              tasks: [
                {
                  id: 'task-1',
                  gap_id: 'gap-1',
                  assignee_name: '知识管理员',
                  priority: 'HIGH',
                  status: 'CONTENT_SUBMITTED',
                  related_document_id: 'doc-customs-01',
                },
              ],
            },
          ],
        });
      }
      if (url.endsWith('/api/v1/knowledge-gap-tasks/task-1') && method === 'PATCH') {
        return jsonResponse({ status: 'RESOLVED', regression_passed: true });
      }
      if (url.endsWith('/api/v1/providers/config') && method === 'GET') {
        return jsonResponse({
          llm: {
            base_url: 'https://100milionseek.top/v1',
            model: 'deepseek-chat',
            has_api_key_configured: true,
            // 即使响应里意外出现字段，前端防御逻辑也绝不渲染任何明文密钥
            api_key: 'sk-should-never-appear-in-dom',
          },
          embedding: {
            primary_provider: 'QWEN_API',
            qwen_model: 'text-embedding-v3',
            local_bge_enabled: true,
          },
          parser: {
            has_mineru_key_configured: true,
            local_ocr_enabled: false,
          },
        });
      }
      return jsonResponse({});
    });

    // 1) 验证 Knowledge Gap 页面与回归关闭验收弹窗
    const { unmount } = render(
      <QueryClientProvider client={createTestQueryClient()}>
        <AuthProvider>
          <MemoryRouter initialEntries={['/knowledge-gaps']}>
            <AppRoutes />
          </MemoryRouter>
        </AuthProvider>
      </QueryClientProvider>,
    );

    await waitFor(() => {
      expect(screen.getByText('海外直邮保税仓清关延误赔付标准')).toBeInTheDocument();
      expect(screen.getByText(/ACL 拒绝（ACL_DENIED）、仅召回未授权文档与 LLM\/MinerU 运维故障绝不进入/)).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: /回归验收/ }));
    fireEvent.click(screen.getByRole('button', { name: /触发回归验证并尝试 RESOLVED/ }));
    await waitFor(() => {
      expect(screen.getByText('任务状态更新与回归验证已完成。')).toBeInTheDocument();
    });
    unmount();

    // 2) 验证 Settings 页面展示脱敏配置，绝不在 DOM 中输出明文 API Key
    const { container } = render(
      <QueryClientProvider client={createTestQueryClient()}>
        <AuthProvider>
          <MemoryRouter initialEntries={['/settings']}>
            <AppRoutes />
          </MemoryRouter>
        </AuthProvider>
      </QueryClientProvider>,
    );

    await waitFor(() => {
      expect(screen.getByDisplayValue('https://100milionseek.top/v1')).toBeInTheDocument();
      expect(screen.getByText('LLM Secret: 后端已托管')).toBeInTheDocument();
      expect(screen.getByText('MinerU Secret: 后端已托管')).toBeInTheDocument();
    });

    expect(container.innerHTML).not.toContain('sk-should-never-appear-in-dom');
  });
});
