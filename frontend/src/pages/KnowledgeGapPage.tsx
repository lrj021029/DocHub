import { useState } from 'react';
import { zodResolver } from '@hookform/resolvers/zod';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { CheckCheck, ClipboardList, Inbox, PlayCircle, RefreshCw, ShieldAlert } from 'lucide-react';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import {
  Alert,
  Badge,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Input,
  Skeleton,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui';
import {
  createKnowledgeGapTask,
  fetchKnowledgeGaps,
  formatGapError,
  KNOWLEDGE_GAP_STATUSES,
  updateKnowledgeGapTask,
  type KnowledgeGapItem,
  type KnowledgeGapTaskItem,
} from '@/features/gaps/api';
import { useAuth } from '@/lib/auth-store';
import type { KnowledgeGapStatus } from '@/types/domain';

const createTaskSchema = z.object({
  assignee_id: z.string().trim().min(1, '请填写或选择负责人 ID'),
  priority: z.enum(['LOW', 'MEDIUM', 'HIGH', 'URGENT']),
  due_at: z.string().optional(),
});

type CreateTaskValues = z.infer<typeof createTaskSchema>;

/** 依据 11 态生命周期映射柔和 Notion 胶囊变体 */
function getGapBadgeVariant(status: KnowledgeGapStatus) {
  if (status === 'RESOLVED' || status === 'CLOSED') return 'success' as const;
  if (status === 'REJECTED' || status === 'REOPENED') return 'danger' as const;
  if (status === 'IN_PROGRESS' || status === 'REVIEWING' || status === 'CONTENT_SUBMITTED') {
    return 'info' as const;
  }
  if (status === 'DETECTED' || status === 'TRIAGED' || status === 'ASSIGNED') {
    return 'warning' as const;
  }
  return 'neutral' as const;
}

/**
 * PRD 模块 10（Slice 7 真实对接）：Knowledge Gap 知识缺口闭环管理。
 * - 明确标注：不含 ACL 拒绝、越权尝试与模型/MinerU 运维故障；
 * - 完整展示 11 态状态标签、创建补充任务、关联补充文档及“回归验证达标后方可 RESOLVED”。
 */
export function KnowledgeGapPage() {
  const queryClient = useQueryClient();
  const { currentOrgId } = useAuth();
  const [taskModalGap, setTaskModalGap] = useState<KnowledgeGapItem | null>(null);
  const [resolveTargetTask, setResolveTargetTask] = useState<KnowledgeGapTaskItem | null>(null);
  const [relatedDocId, setRelatedDocId] = useState('');
  const [feedback, setFeedback] = useState<{ variant: 'success' | 'danger'; message: string } | null>(
    null,
  );

  const gapsQuery = useQuery({
    queryKey: ['knowledge-gaps', currentOrgId],
    queryFn: () => fetchKnowledgeGaps(currentOrgId!),
    enabled: Boolean(currentOrgId),
  });

  const taskForm = useForm<CreateTaskValues>({
    resolver: zodResolver(createTaskSchema),
    defaultValues: { assignee_id: '', priority: 'HIGH', due_at: '' },
  });

  const createTaskMutation = useMutation({
    mutationFn: (values: CreateTaskValues) =>
      createKnowledgeGapTask(currentOrgId!, taskModalGap!.id, {
        assignee_id: values.assignee_id,
        priority: values.priority,
        due_at: values.due_at ? new Date(values.due_at).toISOString() : null,
      }),
    onSuccess: () => {
      setFeedback({ variant: 'success', message: '知识补充任务已分派并记录至审计流。' });
      taskForm.reset();
      setTaskModalGap(null);
      void queryClient.invalidateQueries({ queryKey: ['knowledge-gaps', currentOrgId] });
    },
    onError: (err) => {
      setFeedback({ variant: 'danger', message: formatGapError(err, '创建补充任务失败') });
    },
  });

  const updateTaskMutation = useMutation({
    mutationFn: (params: {
      taskId: string;
      status?: KnowledgeGapStatus;
      related_document_id?: string | null;
      run_regression?: boolean;
    }) =>
      updateKnowledgeGapTask(currentOrgId!, params.taskId, {
        status: params.status,
        related_document_id: params.related_document_id,
        run_regression: params.run_regression,
      }),
    onSuccess: (res) => {
      setFeedback({
        variant: 'success',
        message:
          res.regression_passed === false
            ? '回归验证尚未达标，任务暂不允许关闭为 RESOLVED。'
            : '任务状态更新与回归验证已完成。',
      });
      setResolveTargetTask(null);
      setRelatedDocId('');
      void queryClient.invalidateQueries({ queryKey: ['knowledge-gaps', currentOrgId] });
    },
    onError: (err) => {
      setFeedback({ variant: 'danger', message: formatGapError(err, '更新任务失败') });
    },
  });

  const gaps = gapsQuery.data ?? [];

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-3 border-b border-line pb-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="space-y-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="outline" className="font-mono text-[10px]">
              PRD-10
            </Badge>
            <h1 className="text-xl font-semibold tracking-tight text-ink">
              Knowledge Gap 知识缺口闭环
            </h1>
            <Badge variant="warning">不含 ACL 拒绝与服务故障事件</Badge>
          </div>
          <p className="text-sm text-ink-secondary">
            聚合无召回、低相关性与授权证据不足问题，通过补充任务流转并在文档发布后触发 RAG 回归验证关闭。
          </p>
        </div>

        <Button
          variant="outline"
          size="sm"
          onClick={() => void gapsQuery.refetch()}
          disabled={gapsQuery.isFetching || !currentOrgId}
        >
          <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
          刷新
        </Button>
      </div>

      <Alert variant="warning" title="缺口准入与安全隔离规则（PRD §7.1）" icon={<ShieldAlert className="h-4 w-4" />}>
        <strong>ACL 拒绝（ACL_DENIED）、仅召回未授权文档与 LLM/MinerU 运维故障绝不进入普通 Knowledge Gap 池</strong>
        ；补充材料发布后必须重跑回归问题并达到质量标准，才允许标记为 <span className="font-mono">RESOLVED</span>。
      </Alert>

      {/* 11 态状态机总览标签 */}
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="text-xs font-medium text-ink-muted">11 态生命周期：</span>
        {KNOWLEDGE_GAP_STATUSES.map((state) => (
          <Badge key={state} variant={getGapBadgeVariant(state)} className="font-mono text-[10px]">
            {state}
          </Badge>
        ))}
      </div>

      {feedback ? <Alert variant={feedback.variant}>{feedback.message}</Alert> : null}

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <CheckCheck className="h-4 w-4 text-ink-muted" aria-hidden="true" />
            缺口清单与补充任务流转（{gaps.length}）
          </CardTitle>
          <CardDescription>支持按缺口创建负责人任务、关联文档与触发回归关闭验收。</CardDescription>
        </CardHeader>

        <CardContent>
          {gapsQuery.isLoading ? (
            <div className="space-y-2.5">
              <Skeleton shape="block" className="h-12 w-full" />
              <Skeleton shape="block" className="h-12 w-full" />
            </div>
          ) : gapsQuery.isError ? (
            <Alert variant="danger" title="加载 Knowledge Gap 失败">
              {formatGapError(gapsQuery.error, '无法获取缺口列表')}
            </Alert>
          ) : gaps.length === 0 ? (
            <div className="flex flex-col items-center justify-center rounded-lg border border-dashed border-line py-10 text-center">
              <Inbox className="h-5 w-5 text-ink-muted" aria-hidden="true" />
              <p className="mt-2 text-sm font-medium text-ink">当前组织暂无开放 Knowledge Gap</p>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>缺口主题 / 触发分类</TableHead>
                  <TableHead>Space / 频次</TableHead>
                  <TableHead>缺口状态</TableHead>
                  <TableHead>补充任务与负责人</TableHead>
                  <TableHead className="text-right">操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {gaps.map((gap) => {
                  const firstTask = gap.tasks[0] ?? null;
                  return (
                    <TableRow key={gap.id}>
                      <TableCell>
                        <p className="font-medium text-ink">{gap.topic}</p>
                        <Badge variant="outline" className="mt-1 font-mono text-[10px]">
                          {gap.eventType}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-xs text-ink-secondary">
                        <p>{gap.spaceName ?? gap.spaceId ?? '跨空间'}</p>
                        <p className="text-ink-muted">累计 {gap.occurrenceCount} 次</p>
                      </TableCell>
                      <TableCell>
                        <Badge variant={getGapBadgeVariant(gap.status)} className="font-mono text-[10px]">
                          {gap.status}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-xs">
                        {firstTask ? (
                          <div className="space-y-0.5">
                            <p className="font-medium text-ink">
                              负责人：{firstTask.assigneeName ?? firstTask.assigneeId ?? '待指派'}
                            </p>
                            <p className="text-ink-muted">
                              优先级 {firstTask.priority}
                              {firstTask.dueAt
                                ? ` · 截止 ${firstTask.dueAt.slice(0, 10)}`
                                : ''}
                            </p>
                          </div>
                        ) : (
                          <span className="text-ink-muted">暂未创建补充任务</span>
                        )}
                      </TableCell>
                      <TableCell className="text-right">
                        <div className="inline-flex items-center gap-1.5">
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => {
                              taskForm.reset({ assignee_id: '', priority: 'HIGH', due_at: '' });
                              setTaskModalGap(gap);
                            }}
                          >
                            <ClipboardList className="h-3.5 w-3.5" aria-hidden="true" />
                            创建任务
                          </Button>
                          {firstTask ? (
                            <Button
                              size="sm"
                              onClick={() => {
                                setRelatedDocId(firstTask.relatedDocumentId ?? '');
                                setResolveTargetTask(firstTask);
                              }}
                            >
                              <PlayCircle className="h-3.5 w-3.5" aria-hidden="true" />
                              回归验收
                            </Button>
                          ) : null}
                        </div>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {/* 创建补充任务弹窗 */}
      <Dialog
        open={Boolean(taskModalGap)}
        onOpenChange={(open) => {
          if (!open) setTaskModalGap(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>创建知识补充任务</DialogTitle>
            <DialogDescription>缺口主题：{taskModalGap?.topic}</DialogDescription>
          </DialogHeader>
          <form
            className="mt-4 space-y-3"
            onSubmit={taskForm.handleSubmit((vals) => createTaskMutation.mutate(vals))}
            noValidate
          >
            <div className="space-y-1">
              <label htmlFor="gap-assignee" className="text-xs font-medium text-ink">
                负责人 ID / 邮箱
              </label>
              <Input
                id="gap-assignee"
                placeholder="例如：km@acme.local"
                invalid={Boolean(taskForm.formState.errors.assignee_id)}
                {...taskForm.register('assignee_id')}
              />
              {taskForm.formState.errors.assignee_id ? (
                <p className="text-xs text-danger">
                  {taskForm.formState.errors.assignee_id.message}
                </p>
              ) : null}
            </div>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <div className="space-y-1">
                <label htmlFor="gap-priority" className="text-xs font-medium text-ink">
                  优先级
                </label>
                <select
                  id="gap-priority"
                  className="flex h-9 w-full rounded-lg border border-line bg-surface-elevated px-3 text-sm text-ink"
                  {...taskForm.register('priority')}
                >
                  <option value="URGENT">URGENT（紧急）</option>
                  <option value="HIGH">HIGH（高）</option>
                  <option value="MEDIUM">MEDIUM（中）</option>
                  <option value="LOW">LOW（低）</option>
                </select>
              </div>
              <div className="space-y-1">
                <label htmlFor="gap-due" className="text-xs font-medium text-ink">
                  截止时间（可选）
                </label>
                <Input id="gap-due" type="date" {...taskForm.register('due_at')} />
              </div>
            </div>
            <DialogFooter>
              <DialogClose render={<Button variant="outline" size="sm">取消</Button>} />
              <Button type="submit" size="sm" loading={createTaskMutation.isPending}>
                分派任务
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      {/* 关联文档与触发回归关闭验收弹窗 */}
      <Dialog
        open={Boolean(resolveTargetTask)}
        onOpenChange={(open) => {
          if (!open) setResolveTargetTask(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>关联补充文档并执行回归关闭验收</DialogTitle>
            <DialogDescription>
              补充文档发布后，必须运行对应 Gap 的 RAG 回归查询并达标，才允许关闭为 RESOLVED。
            </DialogDescription>
          </DialogHeader>
          <div className="mt-4 space-y-3">
            <div className="space-y-1">
              <label htmlFor="gap-related-doc" className="text-xs font-medium text-ink">
                关联已发布文档 ID
              </label>
              <Input
                id="gap-related-doc"
                value={relatedDocId}
                onChange={(e) => setRelatedDocId(e.target.value)}
                placeholder="输入补充文档 UUID"
              />
            </div>
          </div>
          <DialogFooter>
            <DialogClose render={<Button variant="outline" size="sm">取消</Button>} />
            <Button
              size="sm"
              loading={updateTaskMutation.isPending}
              onClick={() =>
                updateTaskMutation.mutate({
                  taskId: resolveTargetTask!.id,
                  related_document_id: relatedDocId.trim() || null,
                  run_regression: true,
                  status: 'RESOLVED',
                })
              }
            >
              触发回归验证并尝试 RESOLVED
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
