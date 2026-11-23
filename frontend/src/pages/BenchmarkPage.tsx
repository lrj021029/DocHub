import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Gauge, Inbox, Play, RefreshCw, ShieldCheck } from 'lucide-react';
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
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui';
import {
  fetchBenchmarks,
  formatGovError,
  triggerBenchmarkRun,
} from '@/features/governance/api';
import { useAuth } from '@/lib/auth-store';

/**
 * PRD 模块 14（Slice 8 真实对接）：RAG / Parser Benchmark 评测台。
 * 坚守硬发布门槛：ACL Violation = 0、未授权 Chunk 进入 Reranker/LLM = 0、跨 Organization 泄露 = 0。
 */
export function BenchmarkPage() {
  const queryClient = useQueryClient();
  const { currentOrgId } = useAuth();

  const runsQuery = useQuery({
    queryKey: ['benchmarks', currentOrgId],
    queryFn: () => fetchBenchmarks(currentOrgId!),
    enabled: Boolean(currentOrgId),
  });

  const triggerMutation = useMutation({
    mutationFn: () => triggerBenchmarkRun(currentOrgId!, 'RAG_ACL_REGRESSION'),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['benchmarks', currentOrgId] });
    },
  });

  const runs = runsQuery.data ?? [];

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-3 border-b border-line pb-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="space-y-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="outline" className="font-mono text-[10px]">
              PRD-14
            </Badge>
            <h1 className="text-xl font-semibold tracking-tight text-ink">
              RAG 与解析 Benchmark
            </h1>
            <Badge variant="danger">ACL Violation = 0 红线</Badge>
            <Badge variant="neutral">禁止虚构分数</Badge>
          </div>
          <p className="text-sm text-ink-secondary">
            对接 GET/POST /api/v1/benchmarks，真实记录 Recall@K、MRR、Citation Accuracy 与安全对抗零越权门槛。
          </p>
        </div>

        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => void runsQuery.refetch()}
            disabled={runsQuery.isFetching || !currentOrgId}
          >
            <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
            刷新
          </Button>
          <Button
            size="sm"
            loading={triggerMutation.isPending}
            disabled={!currentOrgId}
            onClick={() => triggerMutation.mutate()}
          >
            <Play className="h-3.5 w-3.5" aria-hidden="true" />
            触发 Benchmark 评测
          </Button>
        </div>
      </div>

      <Alert variant="info" title="发布阻断性安全红线（TECHNICAL_SPEC §17.2）" icon={<ShieldCheck className="h-4 w-4" />}>
        <strong>ACL Violation = 0 · 未授权 Chunk 进入 Reranker/LLM = 0 · 跨 Organization 泄露 = 0</strong>
        。任何安全失败均不可由 Recall 或延迟指标抵消。
      </Alert>

      {triggerMutation.isError ? (
        <Alert variant="danger" title="触发评测失败">
          {formatGovError(triggerMutation.error, '无法启动 Benchmark 运行')}
        </Alert>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Gauge className="h-4 w-4 text-ink-muted" aria-hidden="true" />
            Benchmark 历史执行记录（{runs.length}）
          </CardTitle>
          <CardDescription>仅展示后端持久化写入的真实 BenchmarkRun 评测结果。</CardDescription>
        </CardHeader>
        <CardContent>
          {runsQuery.isLoading ? (
            <div className="space-y-2.5">
              <Skeleton shape="block" className="h-12 w-full" />
              <Skeleton shape="block" className="h-12 w-full" />
            </div>
          ) : runsQuery.isError ? (
            <Alert variant="danger" title="加载 Benchmark 失败">
              {formatGovError(runsQuery.error, '无法获取评测记录')}
            </Alert>
          ) : runs.length === 0 ? (
            <div className="flex flex-col items-center justify-center rounded-lg border border-dashed border-line py-10 text-center">
              <Inbox className="h-5 w-5 text-ink-muted" aria-hidden="true" />
              <p className="mt-2 text-sm font-medium text-ink">暂无 Benchmark 运行记录</p>
              <p className="mt-1 text-xs text-ink-muted">点击右上角按钮可发起真实评测任务。</p>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>套件 / 状态</TableHead>
                  <TableHead>Recall@K</TableHead>
                  <TableHead>MRR</TableHead>
                  <TableHead>Citation Accuracy</TableHead>
                  <TableHead>ACL 安全红线（须全 0）</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {runs.map((run) => {
                  const safePass =
                    run.aclViolations === 0 &&
                    run.unauthorizedPromptChunks === 0 &&
                    run.crossOrgLeaks === 0;
                  return (
                    <TableRow key={run.id}>
                      <TableCell>
                        <p className="font-mono text-xs font-medium text-ink">{run.suiteType}</p>
                        <Badge variant={run.status === 'COMPLETED' ? 'success' : 'info'} className="mt-1 text-[10px]">
                          {run.status}
                        </Badge>
                      </TableCell>
                      <TableCell className="font-mono text-xs">
                        {run.recallAtK !== null ? `${(run.recallAtK * 100).toFixed(1)}%` : '—'}
                      </TableCell>
                      <TableCell className="font-mono text-xs">
                        {run.mrr !== null ? run.mrr.toFixed(3) : '—'}
                      </TableCell>
                      <TableCell className="font-mono text-xs">
                        {run.citationAccuracy !== null
                          ? `${(run.citationAccuracy * 100).toFixed(1)}%`
                          : '—'}
                      </TableCell>
                      <TableCell>
                        <Badge variant={safePass ? 'success' : 'danger'} className="font-mono text-[10px]">
                          ACL={run.aclViolations} · PromptLeak={run.unauthorizedPromptChunks} · CrossOrg={run.crossOrgLeaks}
                        </Badge>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
