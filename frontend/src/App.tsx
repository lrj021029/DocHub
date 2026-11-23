import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import { RequireAuth } from '@/components/auth/RequireAuth';
import { AppShell } from '@/layouts/AppShell';
import { AuthProvider } from '@/lib/auth-store';
import { AclPage } from '@/pages/AclPage';
import { AnalyticsPage } from '@/pages/AnalyticsPage';
import { AuditPage } from '@/pages/AuditPage';
import { BenchmarkPage } from '@/pages/BenchmarkPage';
import { ChatPage } from '@/pages/ChatPage';
import { DashboardPage } from '@/pages/DashboardPage';
import { DocumentsPage } from '@/pages/DocumentsPage';
import { FaqMiningPage } from '@/pages/FaqMiningPage';
import { FaqPublishedPage } from '@/pages/FaqPublishedPage';
import { KnowledgeGapPage } from '@/pages/KnowledgeGapPage';
import { LoginPage } from '@/pages/LoginPage';
import { OrgAdminPage } from '@/pages/OrgAdminPage';
import { SettingsPage } from '@/pages/SettingsPage';
import { SpacesPage } from '@/pages/SpacesPage';

/** 创建带企业级稳健默认策略的 TanStack QueryClient */
export function createAppQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: 1,
        refetchOnWindowFocus: false,
      },
    },
  });
}

/**
 * 全部 14 个 PRD 模块的路由树定义（可被主应用与测试环境共享）。
 * - `/login` 为公开登录认证路由；
 * - 其余 13 个业务工作台路由全部受 RequireAuth 守卫保护（进入前自动尝试单次静默 Refresh）。
 */
export function AppRoutes() {
  return (
    <Routes>
      {/* PRD 01: 公开登录认证页 */}
      <Route path="/login" element={<LoginPage />} />

      {/* 受认证保护的企业工作台路由 */}
      <Route
        element={
          <RequireAuth>
            <AppShell />
          </RequireAuth>
        }
      >
        {/* PRD 02: 总览工作台 */}
        <Route index element={<DashboardPage />} />
        {/* PRD 03: Organization / Department / User / Role 管理 */}
        <Route path="/org-admin" element={<OrgAdminPage />} />
        {/* PRD 04: Knowledge Space 管理与入口授权 */}
        <Route path="/spaces" element={<SpacesPage />} />
        {/* PRD 05: 文档维护中心 */}
        <Route path="/documents" element={<DocumentsPage />} />
        {/* PRD 06: 文档四维权限 ACL 配置 */}
        <Route path="/acl" element={<AclPage />} />
        {/* PRD 07: AI 问答工作台 */}
        <Route path="/chat" element={<ChatPage />} />
        {/* PRD 08: FAQ 挖掘与审核中心 */}
        <Route path="/faq-mining" element={<FaqMiningPage />} />
        {/* PRD 09: 已发布 FAQ 与缓存治理 */}
        <Route path="/faq-published" element={<FaqPublishedPage />} />
        {/* PRD 10: Knowledge Gap 缺口闭环 */}
        <Route path="/knowledge-gaps" element={<KnowledgeGapPage />} />
        {/* PRD 11: 数据运营分析看板 */}
        <Route path="/analytics" element={<AnalyticsPage />} />
        {/* PRD 12: 模型与系统配置中心 */}
        <Route path="/settings" element={<SettingsPage />} />
        {/* PRD 13: 业务与安全审计中心 */}
        <Route path="/audit" element={<AuditPage />} />
        {/* PRD 14: RAG 与解析 Benchmark */}
        <Route path="/benchmark" element={<BenchmarkPage />} />
        {/* 未知路由回退至总览工作台 */}
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}

const rootQueryClient = createAppQueryClient();

/**
 * DocHub 应用根组件：组合 QueryClientProvider、内存 AuthProvider 与 BrowserRouter。
 */
export function App() {
  return (
    <QueryClientProvider client={rootQueryClient}>
      <AuthProvider>
        <BrowserRouter>
          <AppRoutes />
        </BrowserRouter>
      </AuthProvider>
    </QueryClientProvider>
  );
}

export default App;
