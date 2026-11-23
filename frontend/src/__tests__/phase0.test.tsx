import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AppRoutes } from '@/App';
import {
  Alert,
  Badge,
  Button,
  Skeleton,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from '@/components/ui';
import { LOGIN_NAV_ITEM, WORKSPACE_NAV_ITEMS } from '@/config/navigation';
import { apiClient } from '@/lib/api-client';
import {
  AuthProvider,
  clearAuthSession,
  getAccessToken,
  getCurrentOrgId,
  refreshAccessToken,
  setAuthSession,
  setCurrentOrgId,
} from '@/lib/auth-store';
import {
  assertNoTokenInUrl,
  parseSSEStream,
  streamChatSSE,
  type ChatSSEEvent,
} from '@/lib/sse-client';

/** 构造模拟分块传输的 ReadableStream<Uint8Array>，验证任意边界切割解析能力 */
function createChunkedStream(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(encoder.encode(chunk));
      }
      controller.close();
    },
  });
}

describe('Phase 0 前端安全基线与工程验证', () => {
  beforeEach(() => {
    clearAuthSession();
    window.localStorage.clear();
    window.sessionStorage.clear();
  });

  afterEach(() => {
    // 先卸载 React 组件树，再清空内存认证状态，避免卸载前触发无意义的 act 警告
    cleanup();
    vi.restoreAllMocks();
    clearAuthSession();
  });

  describe('1. 内存认证存储与 API 客户端安全红线', () => {
    it('Access Token 仅驻留内存，绝不写入 localStorage 或 sessionStorage', async () => {
      const localSetItemSpy = vi.spyOn(Storage.prototype, 'setItem');

      setAuthSession({
        accessToken: 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.in-memory-only.sig',
        csrfToken: 'csrf-secret-token-01',
        currentOrgId: 'org-acme-001',
      });
      setCurrentOrgId('org-acme-002');

      expect(getAccessToken()).toBe('eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.in-memory-only.sig');
      expect(getCurrentOrgId()).toBe('org-acme-002');

      // 模拟通过 HttpOnly Cookie + X-CSRF-Token 静默刷新令牌
      const mockFetch = vi.fn<typeof fetch>().mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            access_token: 'refreshed-in-memory-jwt-02',
            csrf_token: 'csrf-secret-token-02',
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      );

      const refreshed = await refreshAccessToken(mockFetch);
      expect(refreshed).toBe('refreshed-in-memory-jwt-02');
      expect(getAccessToken()).toBe('refreshed-in-memory-jwt-02');

      // 严格校验 Web Storage 零写入、零残留
      expect(localSetItemSpy).not.toHaveBeenCalled();
      expect(window.localStorage.length).toBe(0);
      expect(window.sessionStorage.length).toBe(0);

      // 校验 Refresh 请求携带 credentials: 'include' 与 X-CSRF-Token
      expect(mockFetch).toHaveBeenCalledTimes(1);
      const [refreshUrl, refreshInit] = mockFetch.mock.calls[0]!;
      expect(refreshUrl).toBe('/api/v1/auth/refresh');
      expect(refreshInit?.credentials).toBe('include');
      const refreshHeaders = new Headers(refreshInit?.headers);
      expect(refreshHeaders.get('X-CSRF-Token')).toBe('csrf-secret-token-01');
    });

    it('API 客户端自动附加安全请求头，并在 401 时触发单次静默 Refresh 后重放原请求', async () => {
      setAuthSession({
        accessToken: 'expired-access-token',
        csrfToken: 'csrf-token-initial',
        currentOrgId: 'org-finance-01',
      });

      const mockFetch = vi
        .fn<typeof fetch>()
        // 首次业务请求返回 401
        .mockResolvedValueOnce(
          new Response(JSON.stringify({ code: 'TOKEN_EXPIRED', message: '令牌已过期' }), {
            status: 401,
            headers: { 'Content-Type': 'application/json' },
          }),
        )
        // 自动触发单次静默 Refresh 返回新令牌
        .mockResolvedValueOnce(
          new Response(
            JSON.stringify({
              access_token: 'fresh-access-token',
              csrf_token: 'csrf-token-rotated',
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } },
          ),
        )
        // 使用新令牌重放原业务请求成功
        .mockResolvedValueOnce(
          new Response(JSON.stringify({ items: ['doc-1'] }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }),
        );

      const result = await apiClient.get<{ items: string[] }>('/api/v1/documents', {
        fetchImpl: mockFetch,
      });

      expect(result).toEqual({ items: ['doc-1'] });
      expect(mockFetch).toHaveBeenCalledTimes(3);

      // 校验首次请求头
      const firstHeaders = new Headers(mockFetch.mock.calls[0]?.[1]?.headers);
      expect(firstHeaders.get('Authorization')).toBe('Bearer expired-access-token');
      expect(firstHeaders.get('X-Organization-Id')).toBe('org-finance-01');
      expect(firstHeaders.get('X-CSRF-Token')).toBe('csrf-token-initial');

      // 校验重放请求已切换为新内存令牌与轮换后的 CSRF Token
      const replayHeaders = new Headers(mockFetch.mock.calls[2]?.[1]?.headers);
      expect(replayHeaders.get('Authorization')).toBe('Bearer fresh-access-token');
      expect(replayHeaders.get('X-CSRF-Token')).toBe('csrf-token-rotated');

      // 全程不得触碰 localStorage
      expect(window.localStorage.length).toBe(0);
    });
  });

  describe('2. POST Fetch SSE 流式解析器（覆盖 7 类标准事件与安全校验）', () => {
    it('能够跨分块边界完整解析全部 7 类 SSE 事件', async () => {
      // 刻意在事件中间切分 chunk，模拟真实网络 TCP 分包
      const rawChunks = [
        ': heartbeat\n\n',
        'event: message_start\ndata: {"message_id":"msg-001"}\n\n',
        'event: retrieval_started\ndata: {"space_ids":["sp-hr","sp-fin"]}\n\n',
        'event: retrieval_completed\ndata: {"authorized_count":2,"lat',
        'ency_ms":42}\n\n',
        'event: citation\ndata: {"citation_id":"cit-1","document_title":"差旅报销规范","page":4}\n\n',
        'event: token\ndata: {"text":"一线城市住宿上限为 "}\n\n',
        'data: {"event":"token","data":{"text":"800 元/晚。"}}\n\n',
        'event: message_complete\ndata: {"usage":{"prompt_tokens":320,"completion_tokens":48}}\n\n',
        'event: message_error\ndata: {"code":"UPSTREAM_TIMEOUT","message":"超时","retryable":true}\n\n',
      ];

      const events: ChatSSEEvent[] = [];
      await parseSSEStream(createChunkedStream(rawChunks), (evt) => {
        events.push(evt);
      });

      expect(events.map((e) => e.event)).toEqual([
        'message_start',
        'retrieval_started',
        'retrieval_completed',
        'citation',
        'token',
        'token',
        'message_complete',
        'message_error',
      ]);

      expect(events[0]).toEqual({ event: 'message_start', data: { message_id: 'msg-001' } });
      expect(events[1]).toEqual({
        event: 'retrieval_started',
        data: { space_ids: ['sp-hr', 'sp-fin'] },
      });
      expect(events[2]).toEqual({
        event: 'retrieval_completed',
        data: { authorized_count: 2, latency_ms: 42 },
      });
      expect(events[3]).toEqual({
        event: 'citation',
        data: { citation_id: 'cit-1', document_title: '差旅报销规范', page: 4 },
      });
      expect(events[4]).toEqual({ event: 'token', data: { text: '一线城市住宿上限为 ' } });
      expect(events[5]).toEqual({ event: 'token', data: { text: '800 元/晚。' } });
      expect(events[6]).toEqual({
        event: 'message_complete',
        data: { usage: { prompt_tokens: 320, completion_tokens: 48 } },
      });
      expect(events[7]).toEqual({
        event: 'message_error',
        data: { code: 'UPSTREAM_TIMEOUT', message: '超时', retryable: true },
      });
    });

    it('严禁在 SSE URL Query 中拼接任何 JWT，并强制通过 Authorization Header 发送 POST', async () => {
      expect(() =>
        assertNoTokenInUrl('/api/v1/chat/conversations/1/messages:stream?token=secret-jwt'),
      ).toThrow(/严禁携带敏感凭据参数/);

      setAuthSession({
        accessToken: 'sse-in-memory-jwt',
        csrfToken: 'sse-csrf-token',
        currentOrgId: 'org-demo',
      });

      const received: ChatSSEEvent[] = [];
      const mockFetch = vi.fn<typeof fetch>().mockResolvedValueOnce(
        new Response(
          createChunkedStream([
            'event: message_start\ndata: {"message_id":"m-9"}\n\n',
            'event: token\ndata: {"text":"已通过鉴权"}\n\n',
            'event: message_complete\ndata: {"usage":{"prompt_tokens":10,"completion_tokens":5}}\n\n',
          ]),
          { status: 200, headers: { 'Content-Type': 'text/event-stream' } },
        ),
      );

      await streamChatSSE({
        url: '/api/v1/chat/conversations/c-1/messages:stream',
        payload: { query: '测试问题', space_ids: ['sp-1'] },
        onEvent: (evt) => received.push(evt),
        fetchImpl: mockFetch,
      });

      expect(mockFetch).toHaveBeenCalledTimes(1);
      const [reqUrl, reqInit] = mockFetch.mock.calls[0]!;
      expect(reqUrl).toBe('/api/v1/chat/conversations/c-1/messages:stream');
      expect(String(reqUrl)).not.toContain('sse-in-memory-jwt');
      expect(reqInit?.method).toBe('POST');
      expect(reqInit?.credentials).toBe('include');

      const headers = new Headers(reqInit?.headers);
      expect(headers.get('Authorization')).toBe('Bearer sse-in-memory-jwt');
      expect(headers.get('X-Organization-Id')).toBe('org-demo');
      expect(headers.get('X-CSRF-Token')).toBe('sse-csrf-token');
      expect(received).toHaveLength(3);
    });
  });

  describe('3. AppShell 完整 14 模块导航入口与实时健康状态渲染', () => {
    it('正确渲染全部 14 个 PRD 导航入口、组织切换器、用户菜单及后端 readyz 指示器', async () => {
      // 注入内存登录会话以通过 RequireAuth 受保护路由守卫（不写入 localStorage）
      setAuthSession({
        accessToken: 'phase0-shell-memory-token',
        csrfToken: 'phase0-csrf',
        currentOrgId: 'org-acme',
        organizations: [
          {
            id: 'org-acme',
            name: 'Acme Corp',
            slug: 'acme-corp',
            status: 'ACTIVE',
            roleNames: ['KNOWLEDGE_MANAGER'],
          },
        ],
        user: {
          id: 'u-1',
          email: 'km@acme.local',
          displayName: '知识管理员',
          systemRoles: [],
          organizationRoles: ['KNOWLEDGE_MANAGER'],
          permissions: ['org:manage'],
        },
      });

      // 模拟 /api/v1/readyz 返回就绪状态（每次调用构造全新 Response，避免 DashboardPage 与 AppShell 并发读取时复用已消费的流）
      const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
        const url = String(input);
        if (url.endsWith('/api/v1/readyz')) {
          return new Response(JSON.stringify({ status: 'ready', checks: { db: 'ok' } }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        return new Response(JSON.stringify({}), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      });

      const queryClient = new QueryClient({
        defaultOptions: { queries: { retry: false } },
      });

      render(
        <QueryClientProvider client={queryClient}>
          <AuthProvider>
            <MemoryRouter initialEntries={['/']}>
              <AppRoutes />
            </MemoryRouter>
          </AuthProvider>
        </QueryClientProvider>,
      );

      // 验证全部 13 个工作台模块 + 1 个登录与会话入口（共 14 个 PRD 模块）全部渲染
      const allExpectedNavItems = [...WORKSPACE_NAV_ITEMS, LOGIN_NAV_ITEM];
      expect(allExpectedNavItems).toHaveLength(14);

      for (const item of allExpectedNavItems) {
        const link = screen.getByRole('link', { name: item.label });
        expect(link).toBeInTheDocument();
        expect(link).toHaveAttribute('href', item.path);
      }

      // 验证顶部当前 Organization 切换器与用户菜单存在
      expect(screen.getByLabelText('切换当前 Organization')).toBeInTheDocument();
      expect(screen.getByTestId('user-menu')).toBeInTheDocument();

      // 验证后端 /api/v1/readyz 健康指示器展示真实探测结果
      await waitFor(() => {
        expect(screen.getByTestId('backend-readiness-indicator')).toHaveTextContent('后端就绪');
      });

      expect(fetchSpy).toHaveBeenCalledWith(
        '/api/v1/readyz',
        expect.objectContaining({ method: 'GET' }),
      );
    });

    it('COSS UI 基础设计系统组件支持键盘切换与标准无障碍语义', () => {
      render(
        <div>
          <Button variant="primary">提交审批</Button>
          <Badge variant="success">PUBLISHED</Badge>
          <Alert variant="warning" title="外发策略提醒">
            L2/L3 敏感文档默认禁止外发
          </Alert>
          <Skeleton shape="line" data-testid="loading-skeleton" />
          <Tabs defaultValue="tab-a">
            <TabsList>
              <TabsTrigger value="tab-a">业务审计</TabsTrigger>
              <TabsTrigger value="tab-b">安全审计</TabsTrigger>
            </TabsList>
            <TabsContent value="tab-a">业务审计面板</TabsContent>
            <TabsContent value="tab-b">安全审计面板</TabsContent>
          </Tabs>
        </div>,
      );

      expect(screen.getByRole('button', { name: '提交审批' })).toBeInTheDocument();
      expect(screen.getByText('PUBLISHED')).toBeInTheDocument();
      expect(screen.getByRole('status')).toHaveTextContent('L2/L3 敏感文档默认禁止外发');
      expect(screen.getByTestId('loading-skeleton')).toBeInTheDocument();
      expect(screen.getByText('业务审计面板')).toBeVisible();

      fireEvent.click(screen.getByRole('tab', { name: '安全审计' }));
      expect(screen.getByText('安全审计面板')).toBeVisible();
    });
  });
});
