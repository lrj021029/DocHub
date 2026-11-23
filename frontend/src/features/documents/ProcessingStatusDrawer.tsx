import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  CheckCircle2,
  ExternalLink,
  Layers,
  RefreshCw,
  RotateCcw,
  ShieldAlert,
  ShieldCheck,
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
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from '@/components/ui';
import {
  fetchDocumentVersions,
  fetchVersionProcessingStatus,
  formatDocumentError,
  publishDocumentVersion,
  reprocessDocumentVersion,
  type DocumentLedgerItem,
} from '@/features/documents/api';

export interface ProcessingStatusDrawerProps {
  orgId: string;
  document: DocumentLedgerItem;
  canManage: boolean;
  onStatusMutated: () => void;
}

/** 依据各子阶段真实枚举映射徽章语义（绝不使用虚构进度百分比） */
function getStageBadgeVariant(status: string) {
  if (status === 'SUCCEEDED' || status === 'ACTIVE' || status === 'READY') return 'success' as const;
  if (status === 'BLOCKED_POLICY' || status === 'FAILED' || status === 'REJECTED') {
    return 'danger' as const;
  }
  if (status === 'RUNNING' || status === 'SUBMITTED' || status === 'BUILDING' || status === 'VERIFYING') {
    return 'info' as const;
  }
  if (status === 'QUARANTINED' || status === 'QUEUED' || status === 'UNKNOWN') {
    return 'warning' as const;
  }
  return 'neutral' as const;
}

/**
 * 文档版本、五阶段真实处理进度、Block / Parent-Child Chunk 预览与发布面板。
 * 严格对接：
 * - GET /api/v1/documents/{document_id}/versions
 * - GET /api/v1/document-versions/{version_id}/processing-status
 * - POST /api/v1/document-versions/{version_id}/reprocess
 * - POST /api/v1/document-versions/{version_id}/publish
 */
export function ProcessingStatusDrawer({
  orgId,
  document,
  canManage,
  onStatusMutated,
}: ProcessingStatusDrawerProps) {
  const queryClient = useQueryClient();
  const [selectedVersionId, setSelectedVersionId] = useState<string | null>(
    document.currentVersionId,
  );
  const [feedback, setFeedback] = useState<{ variant: 'success' | 'danger'; message: string } | null>(
    null,
  );

  const versionsQuery = useQuery({
    queryKey: ['documents', orgId, document.id, 'versions'],
    queryFn: () => fetchDocumentVersions(orgId, document.id),
    enabled: Boolean(orgId && document.id),
  });

  const activeVersionId =
    selectedVersionId ?? document.currentVersionId ?? versionsQuery.data?.[0]?.id ?? null;

  const statusQuery = useQuery({
    queryKey: ['document-versions', orgId, activeVersionId, 'processing-status'],
    queryFn: () => fetchVersionProcessingStatus(orgId, activeVersionId!),
    enabled: Boolean(orgId && activeVersionId),
  });

  const reprocessMutation = useMutation({
    mutationFn: () => reprocessDocumentVersion(orgId, activeVersionId!, 'parse'),
    onSuccess: () => {
      setFeedback({ variant: 'success', message: '已触发该版本重新解析/切片/索引流水线。' });
      void queryClient.invalidateQueries({
        queryKey: ['document-versions', orgId, activeVersionId, 'processing-status'],
      });
      onStatusMutated();
    },
    onError: (err) => {
      setFeedback({ variant: 'danger', message: formatDocumentError(err, '触发重处理失败') });
    },
  });

  const publishMutation = useMutation({
    mutationFn: () => publishDocumentVersion(orgId, activeVersionId!),
    onSuccess: () => {
      setFeedback({
        variant: 'success',
        message: '文档版本已发布并解除 Quarantine，现已进入受 ACL 保护的 Active Retrieval Index。',
      });
      void queryClient.invalidateQueries({
        queryKey: ['document-versions', orgId, activeVersionId, 'processing-status'],
      });
      onStatusMutated();
    },
    onError: (err) => {
      setFeedback({ variant: 'danger', message: formatDocumentError(err, '发布文档版本失败') });
    },
  });

  const processing = statusQuery.data;
  const parseStage = processing?.stages.find((s) => s.stage === 'parse');
  const isBlockedPolicy = parseStage?.status === 'BLOCKED_POLICY';
  const aclReady = processing?.aclConfigured ?? document.aclConfigured ?? false;

  return (
    <Card data-testid="document-processing-panel">
      <CardHeader className="flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <CardTitle className="flex items-center gap-2">
              <Layers className="h-4 w-4 text-ink-muted" aria-hidden="true" />
              文档处理流水线与切片预览：{document.title}
            </CardTitle>
            <Badge variant={document.status === 'ACTIVE' ? 'success' : 'warning'}>
              {document.status}
            </Badge>
            <Badge
              variant={
                document.sensitivityLevel === 'L2' || document.sensitivityLevel === 'L3'
                  ? 'danger'
                  : 'outline'
              }
            >
              {document.sensitivityLevel}
            </Badge>
          </div>
          <CardDescription>
            严格分阶段展示 ImportJob → ParseJob → ChunkingJob → EmbeddingJob → IndexJob 真实状态，绝不展示虚构进度条。
          </CardDescription>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => void statusQuery.refetch()}
            disabled={statusQuery.isFetching}
          >
            <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
            刷新状态
          </Button>
          {canManage && activeVersionId ? (
            <>
              <Button
                variant="outline"
                size="sm"
                loading={reprocessMutation.isPending}
                onClick={() => reprocessMutation.mutate()}
              >
                <RotateCcw className="h-3.5 w-3.5" aria-hidden="true" />
                一键重处理
              </Button>
              <Button
                size="sm"
                loading={publishMutation.isPending}
                onClick={() => publishMutation.mutate()}
              >
                <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
                发布版本
              </Button>
            </>
          ) : null}
        </div>
      </CardHeader>

      <CardContent className="space-y-4">
        {feedback ? <Alert variant={feedback.variant}>{feedback.message}</Alert> : null}

        {/* Quarantine 未配 ACL 解除拦截提示 */}
        {document.status === 'QUARANTINED' ? (
          <Alert variant="warning" title="文档处于 Quarantine（待授权隔离态）">
            新导入文档默认不可被普通问答检索。未完成 Document ACL 配置前，无法解除 Quarantine 并发布至
            Active Index。
            {!aclReady ? (
              <div className="mt-2">
                <Link to={`/acl?documentId=${encodeURIComponent(document.id)}`}>
                  <Button variant="outline" size="sm">
                    <ShieldCheck className="h-3.5 w-3.5" aria-hidden="true" />
                    前往配置四维 ACL
                    <ExternalLink className="h-3 w-3" aria-hidden="true" />
                  </Button>
                </Link>
              </div>
            ) : null}
          </Alert>
        ) : null}

        {/* L2/L3 扫描 PDF 外发策略安全阻断提示 */}
        {isBlockedPolicy ? (
          <Alert
            variant="danger"
            title="安全策略拦截：BLOCKED_POLICY（L2/L3 禁止默认外发 MinerU）"
            icon={<ShieldAlert className="h-4 w-4" />}
          >
            当前文档为高敏感等级且触发外部解析门控，系统已执行 Fail-Closed 阻断（错误码：
            <span className="font-mono">{parseStage?.errorCode ?? 'EGRESS_POLICY_BLOCKED'}</span>
            ）。{parseStage?.errorDetail ?? '请启用合规本地 OCR 后再重试，严禁降级外发。'}
          </Alert>
        ) : null}

        {/* 不可变版本选择器 */}
        {(versionsQuery.data ?? []).length > 0 ? (
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <span className="font-medium text-ink-muted">不可变版本：</span>
            {(versionsQuery.data ?? []).map((ver) => (
              <Button
                key={ver.id}
                size="sm"
                variant={ver.id === activeVersionId ? 'primary' : 'outline'}
                onClick={() => setSelectedVersionId(ver.id)}
              >
                v{ver.versionNo} · {ver.filename}
              </Button>
            ))}
          </div>
        ) : null}

        {statusQuery.isLoading ? (
          <div className="grid grid-cols-1 gap-3 md:grid-cols-5">
            {Array.from({ length: 5 }).map((_, i) => (
              <Skeleton key={i} shape="block" className="h-24" />
            ))}
          </div>
        ) : statusQuery.isError ? (
          <Alert variant="danger" title="获取分阶段处理状态失败">
            {formatDocumentError(statusQuery.error, '无法读取版本处理状态')}
          </Alert>
        ) : !processing ? (
          <p className="py-6 text-center text-xs text-ink-muted">该文档尚无关联的可查询版本。</p>
        ) : (
          <Tabs defaultValue="pipeline">
            <TabsList aria-label="文档处理详情选项卡">
              <TabsTrigger value="pipeline">五阶段真实状态</TabsTrigger>
              <TabsTrigger value="blocks">ParsedBlock 预览（{processing.blocks.length}）</TabsTrigger>
              <TabsTrigger value="chunks">Parent-Child Chunk（{processing.chunks.length}）</TabsTrigger>
            </TabsList>

            <TabsContent value="pipeline">
              <div className="grid grid-cols-1 gap-3 md:grid-cols-5">
                {processing.stages.map((stage) => (
                  <div
                    key={stage.stage}
                    className="rounded-lg border border-line bg-surface-subtle p-3 space-y-2"
                  >
                    <p className="text-xs font-semibold text-ink">{stage.label}</p>
                    <Badge variant={getStageBadgeVariant(stage.status)} className="font-mono">
                      {stage.status}
                    </Badge>
                    {stage.provider ? (
                      <p className="text-[11px] text-ink-muted">引擎：{stage.provider}</p>
                    ) : null}
                    {stage.externalTaskId ? (
                      <p className="font-mono text-[10px] text-ink-secondary break-all">
                        MinerU Task: {stage.externalTaskId}
                      </p>
                    ) : null}
                    {stage.errorCode ? (
                      <p className="font-mono text-[11px] text-danger break-all">
                        错误码：{stage.errorCode}
                      </p>
                    ) : null}
                  </div>
                ))}
              </div>
            </TabsContent>

            <TabsContent value="blocks">
              {processing.blocks.length === 0 ? (
                <p className="rounded-lg border border-dashed border-line py-8 text-center text-xs text-ink-muted">
                  暂无解析结构块（等待 ParseJob 完成后回填 JSON/Markdown 权威产物）。
                </p>
              ) : (
                <div className="max-h-72 space-y-2 overflow-y-auto pr-1">
                  {processing.blocks.map((block) => (
                    <div
                      key={block.id}
                      className="rounded-lg border border-line bg-surface-subtle p-3 text-xs space-y-1"
                    >
                      <div className="flex items-center justify-between gap-2">
                        <Badge variant="outline" className="font-mono text-[10px]">
                          {block.blockType}
                        </Badge>
                        <span className="text-[11px] text-ink-muted">
                          {block.pageNumber ? `第 ${block.pageNumber} 页` : '无页码'}
                          {block.headingPath ? ` · ${block.headingPath}` : ''}
                        </span>
                      </div>
                      <p className="whitespace-pre-wrap text-ink-secondary">{block.text}</p>
                    </div>
                  ))}
                </div>
              )}
            </TabsContent>

            <TabsContent value="chunks">
              {processing.chunks.length === 0 ? (
                <p className="rounded-lg border border-dashed border-line py-8 text-center text-xs text-ink-muted">
                  暂无 Parent-Child 切片数据（等待 ChunkingJob 完成）。
                </p>
              ) : (
                <div className="max-h-72 space-y-2 overflow-y-auto pr-1">
                  {processing.chunks.map((chunk) => (
                    <div
                      key={chunk.id}
                      className="rounded-lg border border-line bg-surface-subtle p-3 text-xs space-y-1.5"
                    >
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <div className="flex items-center gap-1.5">
                          <Badge variant={chunk.parentChunkId ? 'info' : 'neutral'}>
                            {chunk.parentChunkId ? 'Child Chunk' : 'Parent Chunk'}
                          </Badge>
                          <span className="font-mono text-[11px] text-ink-muted">
                            {chunk.tokenCount} tokens
                          </span>
                        </div>
                        {chunk.pageRange ? (
                          <span className="text-[11px] text-ink-muted">页码：{chunk.pageRange}</span>
                        ) : null}
                      </div>
                      <p className="whitespace-pre-wrap text-ink-secondary">{chunk.contentText}</p>
                    </div>
                  ))}
                </div>
              )}
            </TabsContent>
          </Tabs>
        )}
      </CardContent>
    </Card>
  );
}
