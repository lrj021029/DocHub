import { useQuery } from '@tanstack/react-query';
import {
  Activity,
  ArrowRight,
  FileText,
  MessageSquareText,
  ShieldCheck,
  Sparkles,
} from 'lucide-react';
import { Link } from 'react-router-dom';
import {
  Alert,
  Badge,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  Skeleton,
} from '@/components/ui';
import { fetchAnalyticsOverview } from '@/features/governance/api';
import { fetchBackendReadiness } from '@/lib/api-client';
import { useAuth } from '@/lib/auth-store';

/**
 * PRD 模块 2：Notion 风格总览工作台。
 * 真实接入 GET /api/v1/analytics/overview 与 GET /api/v1/readyz，呈现组织级核心指标与快捷工作入口。
 */
export function DashboardPage() {
  const { currentOrgId, organizations, user } = useAuth();
  const activeOrg = organizations.find((o) => o.id === currentOrgId);

  const overviewQuery = useQuery({
    queryKey: ['analytics', currentOrgId, 'overview'],
    queryFn: () => fetchAnalyticsOverview(currentOrgId!),
    enabled: Boolean(currentOrgId),
    retry: false,
  });

  const readinessQuery = useQuery({
    queryKey: ['system', 'readyz'],
    queryFn: () => fetchBackendReadiness(),
    retry: false,
  });

  const overview = overviewQuery.data;
  const quickLinks = [
    {
      title: 'AI 问答工作台',
      desc: '选择授权 Knowledge Space 发起混合检索与 SSE 流式回答',
      to: '/chat',
      icon: MessageSquareText,
    },
    {
      title: '文档维护中心',
      desc: '单文件/批量上传、MinerU 与轻量解析器分阶段任务跟踪',
      to: '/documents',
      icon: FileText,
    },
    {
      title: '文档四维 ACL 配置',
      desc: '配置 Global / Department / Role / User Allow OR 授权与版本 Diff',
      to: '/acl',
      icon: ShieldCheck,
    },
    {
      title: 'FAQ 挖掘与审核',
      desc: '查看 HDBSCAN 高频聚类候选并执行双人分离人工审核',
      to: '/faq-mining',
      icon: Sparkles,
    },
  ];

  const metricCards = [
    {
      label: '授权 Knowledge Space',
      value: overview ? String(overview.authorizedSpaces) : '--',
      hint: '仅统计当前 Organization 内有权访问空间',
    },
    {
      label: '已激活文档 / Chunk',
      value: overview ? `${overview.activeDocuments} / ${overview.activeChunks}` : '--',
      hint: '不含 Quarantine 待授权隔离文档',
    },
    {
      label: 'FAQ 直接命中率',
      value: overview ? `${(overview.faqHitRate * 100).toFixed(1)}%` : '--',
      hint: '命中后每次强制重新校验来源 ACL',
    },
    {
      label: '开放 Knowledge Gap',
      value: overview ? String(overview.openGapCount) : '--',
      hint: '仅含合法缺口，不含 ACL 拒绝事件',
    },
  ];

  return (
    <div className="space-y-6">
      {/* Notion 式文档头部 */}
      <div className="flex flex-col gap-3 border-b border-line pb-5 sm:flex-row sm:items-start sm:justify-between">
        <div className="space-y-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="outline" className="font-mono text-[10px]">
              PRD-02
            </Badge>
            <h1 className="text-2xl font-semibold tracking-tight text-ink">
              {activeOrg ? `${activeOrg.name} · 知识治理总览` : 'DocHub 总览工作台'}
            </h1>
            <Badge variant="success">
              <Activity className="h-3 w-3" aria-hidden="true" />
              {readinessQuery.data?.status ?? '探测中'}
            </Badge>
          </div>
          <p className="text-sm text-ink-secondary">
            欢迎回来，{user?.displayName ?? user?.email ?? '成员'}。本工作台严格按当前 Organization 与后端权威 ACL 过滤展示。
          </p>
        </div>

        <Link to="/chat">
          <Button size="sm">
            发起 AI 问答
            <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
          </Button>
        </Link>
      </div>

      {/* 核心指标卡 */}
      {overviewQuery.isLoading ? (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {Array.from({ length: 4 }).map((_, idx) => (
            <Skeleton key={idx} shape="block" className="h-24" />
          ))}
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {metricCards.map((item) => (
            <Card key={item.label}>
              <CardContent className="py-4">
                <p className="text-xs font-medium text-ink-muted">{item.label}</p>
                <p className="mt-1.5 text-2xl font-semibold tracking-tight text-ink tabular-nums">
                  {item.value}
                </p>
                <p className="mt-1 text-[11px] text-ink-muted">{item.hint}</p>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      <Alert variant="info" title="企业级权限一致性保障">
        所有检索、Reranker、FAQ 缓存命中与历史会话重开均在 PostgreSQL 强制复核当前 ACL Revision，绝不将未授权内容传入模型或普通看板。
      </Alert>

      {/* 快捷模块入口 */}
      <div className="grid grid-cols-1 gap-3.5 md:grid-cols-2">
        {quickLinks.map((item) => {
          const IconComp = item.icon;
          return (
            <Card key={item.to} className="hover:border-line-strong">
              <CardHeader className="flex-row items-center justify-between space-y-0">
                <div className="flex items-center gap-2.5">
                  <div className="flex h-8 w-8 items-center justify-center rounded-md bg-surface-muted text-ink">
                    <IconComp className="h-4 w-4" aria-hidden="true" />
                  </div>
                  <CardTitle>{item.title}</CardTitle>
                </div>
                <Link to={item.to}>
                  <Button variant="outline" size="sm">
                    打开
                  </Button>
                </Link>
              </CardHeader>
              <CardContent className="pt-2.5">
                <CardDescription>{item.desc}</CardDescription>
              </CardContent>
            </Card>
          );
        })}
      </div>
    </div>
  );
}
