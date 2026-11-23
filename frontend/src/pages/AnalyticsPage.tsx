import { useQuery } from '@tanstack/react-query';
import { BarChart3, RefreshCw, ShieldAlert } from 'lucide-react';
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
import {
  fetchAnalyticsOverview,
  fetchFaqAnalytics,
  fetchGapAnalytics,
  formatGovError,
} from '@/features/governance/api';
import { useAuth } from '@/lib/auth-store';

/**
 * PRD 模块 11（Slice 8 真实对接）：数据运营分析看板。
 * 严格按当前 Organization 与授权范围过滤，清晰声明无受限文档标题/排行旁路暴露。
 */
export function AnalyticsPage() {
  const { currentOrgId } = useAuth();

  const overviewQuery = useQuery({
    queryKey: ['analytics', currentOrgId, 'overview'],
    queryFn: () => fetchAnalyticsOverview(currentOrgId!),
    enabled: Boolean(currentOrgId),
  });

  const faqQuery = useQuery({
    queryKey: ['analytics', currentOrgId, 'faq'],
    queryFn: () => fetchFaqAnalytics(currentOrgId!),
    enabled: Boolean(currentOrgId),
  });

  const gapQuery = useQuery({
    queryKey: ['analytics', currentOrgId, 'knowledge-gaps'],
    queryFn: () => fetchGapAnalytics(currentOrgId!),
    enabled: Boolean(currentOrgId),
  });

  const overview = overviewQuery.data;
  const faq = faqQuery.data;
  const gap = gapQuery.data;
  const isLoading = overviewQuery.isLoading || faqQuery.isLoading || gapQuery.isLoading;
  const firstError = overviewQuery.error ?? faqQuery.error ?? gapQuery.error;

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-3 border-b border-line pb-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="space-y-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="outline" className="font-mono text-[10px]">
              PRD-11
            </Badge>
            <h1 className="text-xl font-semibold tracking-tight text-ink">数据运营分析看板</h1>
            <Badge variant="info">组织与授权范围过滤</Badge>
            <Badge variant="warning">受限资源零旁路暴露</Badge>
          </div>
          <p className="text-sm text-ink-secondary">
            实时汇聚 PV/UV、文档与 Chunk 规模、FAQ 命中与 STALE 比例、Knowledge Gap 解决率及检索/Token 耗时。
          </p>
        </div>

        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            void overviewQuery.refetch();
            void faqQuery.refetch();
            void gapQuery.refetch();
          }}
        >
          <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
          刷新指标
        </Button>
      </div>

      <Alert variant="info" title="隐私与防侧信道原则（PRD §9 & §10.1）" icon={<ShieldAlert className="h-4 w-4" />}>
        <strong>仅展示当前 Organization 与授权范围指标，无受限旁路</strong>
        ；受限文档标题、热门排行与 ACL 拒绝详情绝不出现在普通运营看板中。
      </Alert>

      {isLoading ? (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
          <Skeleton shape="block" className="h-36" />
          <Skeleton shape="block" className="h-36" />
          <Skeleton shape="block" className="h-36" />
        </div>
      ) : firstError ? (
        <Alert variant="danger" title="加载运营指标失败">
          {formatGovError(firstError, '无法获取运营分析数据')}
        </Alert>
      ) : (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <BarChart3 className="h-4 w-4 text-ink-muted" aria-hidden="true" />
                问答活跃度与性能耗时
              </CardTitle>
              <CardDescription>来自 GET /api/v1/analytics/overview</CardDescription>
            </CardHeader>
            <CardContent className="space-y-2 text-xs">
              <div className="flex justify-between">
                <span className="text-ink-muted">PV / UV</span>
                <span className="font-mono font-semibold text-ink">
                  {overview?.pv ?? 0} / {overview?.uv ?? 0}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-ink-muted">活跃会话数</span>
                <span className="font-mono font-semibold text-ink">
                  {overview?.activeConversations ?? 0}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-ink-muted">首 Token 延迟 P95</span>
                <span className="font-mono font-semibold text-ink">
                  {overview?.p95FirstTokenMs !== null && overview?.p95FirstTokenMs !== undefined
                    ? `${overview.p95FirstTokenMs} ms`
                    : '待采集'}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-ink-muted">累计 Token 消耗</span>
                <span className="font-mono font-semibold text-ink">
                  {(overview?.totalTokens ?? 0).toLocaleString()}
                </span>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>知识资产与 FAQ 效能</CardTitle>
              <CardDescription>来自 GET /api/v1/analytics/faq</CardDescription>
            </CardHeader>
            <CardContent className="space-y-2 text-xs">
              <div className="flex justify-between">
                <span className="text-ink-muted">有效文档 / Chunk</span>
                <span className="font-mono font-semibold text-ink">
                  {overview?.activeDocuments ?? 0} / {overview?.activeChunks ?? 0}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-ink-muted">已发布 FAQ / 候选</span>
                <span className="font-mono font-semibold text-ink">
                  {faq?.publishedCount ?? 0} / {faq?.candidateCount ?? 0}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-ink-muted">FAQ 命中率</span>
                <span className="font-mono font-semibold text-success">
                  {((faq?.hitRate ?? overview?.faqHitRate ?? 0) * 100).toFixed(1)}%
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-ink-muted">STALE 过期比例</span>
                <span className="font-mono font-semibold text-warning">
                  {((overview?.staleFaqRatio ?? 0) * 100).toFixed(1)}%
                </span>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Knowledge Gap 闭环效率</CardTitle>
              <CardDescription>来自 GET /api/v1/analytics/knowledge-gaps</CardDescription>
            </CardHeader>
            <CardContent className="space-y-2 text-xs">
              <div className="flex justify-between">
                <span className="text-ink-muted">缺口总数 / 已解决</span>
                <span className="font-mono font-semibold text-ink">
                  {gap?.totalGaps ?? 0} / {gap?.resolvedGaps ?? 0}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-ink-muted">闭环解决率</span>
                <span className="font-mono font-semibold text-success">
                  {((gap?.resolutionRate ?? overview?.gapResolutionRate ?? 0) * 100).toFixed(1)}%
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-ink-muted">平均闭环时长</span>
                <span className="font-mono font-semibold text-ink">
                  {gap?.avgResolutionHours !== null && gap?.avgResolutionHours !== undefined
                    ? `${gap.avgResolutionHours.toFixed(1)} h`
                    : '待采集'}
                </span>
              </div>
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  );
}
