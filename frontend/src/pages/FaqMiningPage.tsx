import { useState } from 'react';
import { zodResolver } from '@hookform/resolvers/zod';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { CheckCircle2, Inbox, Play, RefreshCw, Send, ShieldAlert, UserCheck } from 'lucide-react';
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
  fetchFaqCandidates,
  formatFaqError,
  publishFaqVersion,
  submitFaqCandidateForReview,
  submitFaqReviewDecision,
  triggerFaqMiningRun,
  type FaqCandidateItem,
} from '@/features/faq/api';
import { useAuth } from '@/lib/auth-store';

const reviewSchema = z.object({
  decision: z.enum(['APPROVE', 'REJECT']),
  comments: z.string().trim().min(2, '请填写不少于 2 个字符的审核意见'),
});

type ReviewValues = z.infer<typeof reviewSchema>;

/**
 * PRD 模块 8（Slice 6 真实对接）：FAQ 挖掘与审核中心。
 * 强制体现安全治理规则：禁止自动发布、创建者禁止审核自己提交的 FAQ、候选引用须来自受授权来源。
 */
export function FaqMiningPage() {
  const queryClient = useQueryClient();
  const { currentOrgId, user } = useAuth();
  const [reviewTarget, setReviewTarget] = useState<FaqCandidateItem | null>(null);
  const [feedback, setFeedback] = useState<{ variant: 'success' | 'danger'; message: string } | null>(
    null,
  );

  const candidatesQuery = useQuery({
    queryKey: ['faq', currentOrgId, 'candidates'],
    queryFn: () => fetchFaqCandidates(currentOrgId!),
    enabled: Boolean(currentOrgId),
  });

  const form = useForm<ReviewValues>({
    resolver: zodResolver(reviewSchema),
    defaultValues: { decision: 'APPROVE', comments: '' },
  });

  const miningMutation = useMutation({
    mutationFn: () => triggerFaqMiningRun(currentOrgId!),
    onSuccess: (res) => {
      setFeedback({
        variant: 'success',
        message: `已触发 HDBSCAN FAQ 挖掘任务（状态：${res.status}）。`,
      });
      void queryClient.invalidateQueries({ queryKey: ['faq', currentOrgId, 'candidates'] });
    },
    onError: (err) => {
      setFeedback({ variant: 'danger', message: formatFaqError(err, '触发 FAQ 挖掘失败') });
    },
  });

  const submitReviewMutation = useMutation({
    mutationFn: (candidateId: string) => submitFaqCandidateForReview(currentOrgId!, candidateId),
    onSuccess: () => {
      setFeedback({ variant: 'success', message: '已提交至人工审核队列（PENDING_REVIEW）。' });
      void queryClient.invalidateQueries({ queryKey: ['faq', currentOrgId, 'candidates'] });
    },
    onError: (err) => {
      setFeedback({ variant: 'danger', message: formatFaqError(err, '提交审核失败') });
    },
  });

  const reviewDecisionMutation = useMutation({
    mutationFn: (values: ReviewValues) =>
      submitFaqReviewDecision(currentOrgId!, reviewTarget!.faqId ?? reviewTarget!.id, values),
    onSuccess: () => {
      setFeedback({ variant: 'success', message: '审核决策已记录并写入不可变审核历史。' });
      form.reset();
      setReviewTarget(null);
      void queryClient.invalidateQueries({ queryKey: ['faq', currentOrgId, 'candidates'] });
    },
    onError: (err) => {
      setFeedback({ variant: 'danger', message: formatFaqError(err, '提交审核决策失败') });
    },
  });

  const publishMutation = useMutation({
    mutationFn: (faqId: string) => publishFaqVersion(currentOrgId!, faqId),
    onSuccess: () => {
      setFeedback({ variant: 'success', message: 'FAQ 已发布为不可变版本并通知缓存预热。' });
      void queryClient.invalidateQueries({ queryKey: ['faq', currentOrgId] });
    },
    onError: (err) => {
      setFeedback({ variant: 'danger', message: formatFaqError(err, '发布 FAQ 失败') });
    },
  });

  const candidates = candidatesQuery.data ?? [];

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-3 border-b border-line pb-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="space-y-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="outline" className="font-mono text-[10px]">
              PRD-08
            </Badge>
            <h1 className="text-xl font-semibold tracking-tight text-ink">FAQ 挖掘与审核中心</h1>
            <Badge variant="warning">禁止自动发布</Badge>
            <Badge variant="neutral">创建者不可自审</Badge>
          </div>
          <p className="text-sm text-ink-secondary">
            从脱敏问答聚类（HDBSCAN）中提取高频问题候选，基于授权知识来源生成答案并强制双人分离审批。
          </p>
        </div>

        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => void candidatesQuery.refetch()}
            disabled={candidatesQuery.isFetching || !currentOrgId}
          >
            <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
            刷新
          </Button>
          <Button
            size="sm"
            loading={miningMutation.isPending}
            disabled={!currentOrgId}
            onClick={() => miningMutation.mutate()}
          >
            <Play className="h-3.5 w-3.5" aria-hidden="true" />
            手动触发挖掘
          </Button>
        </div>
      </div>

      <Alert variant="warning" title="双人分离审批红线（PRD §6.6）" icon={<ShieldAlert className="h-4 w-4" />}>
        <strong>创建者禁止审核自己创建或提交的 FAQ</strong>
        ；不同权限域的问题文本绝不会通过 FAQ 候选内容相互泄露，所有候选答案必须通过来源引用校验。
      </Alert>

      {feedback ? <Alert variant={feedback.variant}>{feedback.message}</Alert> : null}

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <UserCheck className="h-4 w-4 text-ink-muted" aria-hidden="true" />
            FAQ 候选队列（{candidates.length}）
          </CardTitle>
          <CardDescription>
            展示聚类频次、独立用户数、来源引用数、置信度与 Lifecycle 状态。
          </CardDescription>
        </CardHeader>

        <CardContent>
          {candidatesQuery.isLoading ? (
            <div className="space-y-2.5">
              <Skeleton shape="block" className="h-12 w-full" />
              <Skeleton shape="block" className="h-12 w-full" />
            </div>
          ) : candidatesQuery.isError ? (
            <Alert variant="danger" title="加载 FAQ 候选失败">
              {formatFaqError(candidatesQuery.error, '无法获取候选列表')}
            </Alert>
          ) : candidates.length === 0 ? (
            <div className="flex flex-col items-center justify-center rounded-lg border border-dashed border-line py-10 text-center">
              <Inbox className="h-5 w-5 text-ink-muted" aria-hidden="true" />
              <p className="mt-2 text-sm font-medium text-ink">当前暂无 FAQ 挖掘候选</p>
              <p className="mt-1 text-xs text-ink-muted">可点击右上角「手动触发挖掘」执行聚类任务。</p>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>代表问题（脱敏）</TableHead>
                  <TableHead>频次 / 独立用户</TableHead>
                  <TableHead>来源引用</TableHead>
                  <TableHead>置信度</TableHead>
                  <TableHead>生命周期</TableHead>
                  <TableHead className="text-right">审核流操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {candidates.map((item) => {
                  const isSelfCreator = Boolean(item.createdBy && user?.id && item.createdBy === user.id);
                  return (
                    <TableRow key={item.id}>
                      <TableCell>
                        <p className="font-medium text-ink">{item.question}</p>
                        {item.answerDraft ? (
                          <p className="line-clamp-1 text-xs text-ink-muted">{item.answerDraft}</p>
                        ) : null}
                      </TableCell>
                      <TableCell className="text-xs">
                        {item.frequencyCount} 次 · {item.uniqueUserCount} 人
                      </TableCell>
                      <TableCell>
                        <Badge variant="outline">{item.sourceCitationCount} 条引用</Badge>
                      </TableCell>
                      <TableCell className="font-mono text-xs">
                        {(item.confidenceScore * 100).toFixed(0)}%
                      </TableCell>
                      <TableCell>
                        <Badge
                          variant={
                            item.lifecycleState === 'PENDING_REVIEW'
                              ? 'warning'
                              : item.lifecycleState === 'REJECTED'
                                ? 'danger'
                                : 'info'
                          }
                        >
                          {item.lifecycleState}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-right">
                        <div className="inline-flex flex-wrap items-center justify-end gap-1.5">
                          {item.lifecycleState === 'CANDIDATE' ? (
                            <Button
                              variant="outline"
                              size="sm"
                              loading={submitReviewMutation.isPending}
                              onClick={() => submitReviewMutation.mutate(item.id)}
                            >
                              <Send className="h-3.5 w-3.5" aria-hidden="true" />
                              提交审核
                            </Button>
                          ) : null}

                          <Button
                            variant="outline"
                            size="sm"
                            disabled={isSelfCreator}
                            title={isSelfCreator ? '创建者禁止审核自己提交的 FAQ' : undefined}
                            onClick={() => {
                              form.reset({ decision: 'APPROVE', comments: '' });
                              setReviewTarget(item);
                            }}
                          >
                            {isSelfCreator ? '创建者禁自审' : '审核决策'}
                          </Button>

                          <Button
                            size="sm"
                            loading={publishMutation.isPending}
                            onClick={() => publishMutation.mutate(item.faqId ?? item.id)}
                          >
                            <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
                            发布
                          </Button>
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

      {/* 审核决策弹窗 */}
      <Dialog
        open={Boolean(reviewTarget)}
        onOpenChange={(open) => {
          if (!open) setReviewTarget(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>FAQ 人工审核决策</DialogTitle>
            <DialogDescription>
              创建者禁止审核自己的 FAQ 候选；审核记录将持久化保存审批人、意见与政策版本。
            </DialogDescription>
          </DialogHeader>

          <form
            className="mt-4 space-y-3"
            onSubmit={form.handleSubmit((vals) => reviewDecisionMutation.mutate(vals))}
            noValidate
          >
            <div className="rounded-lg border border-line bg-surface-subtle p-3 text-xs">
              <p className="font-semibold text-ink">{reviewTarget?.question}</p>
              {reviewTarget?.answerDraft ? (
                <p className="mt-1 text-ink-secondary">{reviewTarget.answerDraft}</p>
              ) : null}
            </div>

            <div className="space-y-1">
              <label htmlFor="faq-review-decision" className="text-xs font-medium text-ink">
                审核结论
              </label>
              <select
                id="faq-review-decision"
                className="flex h-9 w-full rounded-lg border border-line bg-surface-elevated px-3 text-sm text-ink"
                {...form.register('decision')}
              >
                <option value="APPROVE">APPROVE（审核通过）</option>
                <option value="REJECT">REJECT（驳回修改）</option>
              </select>
            </div>

            <div className="space-y-1">
              <label htmlFor="faq-review-comments" className="text-xs font-medium text-ink">
                审核意见（必填）
              </label>
              <Input
                id="faq-review-comments"
                placeholder="例如：来源文档与页码引用核对无误，同意发布"
                invalid={Boolean(form.formState.errors.comments)}
                {...form.register('comments')}
              />
              {form.formState.errors.comments ? (
                <p className="text-xs text-danger">{form.formState.errors.comments.message}</p>
              ) : null}
            </div>

            <DialogFooter>
              <DialogClose render={<Button variant="outline" size="sm">取消</Button>} />
              <Button type="submit" size="sm" loading={reviewDecisionMutation.isPending}>
                提交审核记录
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  );
}
