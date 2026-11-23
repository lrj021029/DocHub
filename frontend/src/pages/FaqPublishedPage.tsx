import { useState } from 'react';
import { zodResolver } from '@hookform/resolvers/zod';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Ban, BookCheck, DatabaseZap, Inbox, Plus, RefreshCw } from 'lucide-react';
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
  DialogTrigger,
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
  createManualFaq,
  disableFaqItem,
  fetchFaqList,
  formatFaqError,
  publishFaqVersion,
} from '@/features/faq/api';
import { useAuth } from '@/lib/auth-store';

/** 手工创建 MANUAL FAQ 表单校验（PRD §6.5 强制要求事实依据、有效期与维护人） */
const manualFaqSchema = z.object({
  question: z.string().trim().min(4, '标准问题不少于 4 个字符'),
  answer: z.string().trim().min(6, '标准答案不少于 6 个字符'),
  evidence: z.string().trim().min(4, '必须填写事实依据（文档来源或政策文号）'),
  maintainer: z.string().trim().min(2, '必须指定维护人'),
  expires_at: z.string().trim().min(1, '必须指定有效期限'),
});

type ManualFaqValues = z.infer<typeof manualFaqSchema>;

/**
 * PRD 模块 9（Slice 6 & 7 真实对接）：已发布 FAQ 管理与缓存状态。
 * 遵循 ADR-001 裁决：独立分离呈现 Lifecycle / Publication（UNPUBLISHED/PUBLISHED/DISABLED）/ Source Health（HEALTHY/STALE/EXPIRED）三轴。
 */
export function FaqPublishedPage() {
  const queryClient = useQueryClient();
  const { currentOrgId } = useAuth();
  const [createOpen, setCreateOpen] = useState(false);
  const [feedback, setFeedback] = useState<{ variant: 'success' | 'danger'; message: string } | null>(
    null,
  );

  const faqQuery = useQuery({
    queryKey: ['faq', currentOrgId, 'list'],
    queryFn: () => fetchFaqList(currentOrgId!),
    enabled: Boolean(currentOrgId),
  });

  const form = useForm<ManualFaqValues>({
    resolver: zodResolver(manualFaqSchema),
    defaultValues: {
      question: '',
      answer: '',
      evidence: '',
      maintainer: '',
      expires_at: '',
    },
  });

  const createManualMutation = useMutation({
    mutationFn: (values: ManualFaqValues) =>
      createManualFaq(currentOrgId!, {
        ...values,
        expires_at: new Date(values.expires_at).toISOString(),
      }),
    onSuccess: () => {
      setFeedback({ variant: 'success', message: '手工 MANUAL FAQ 已创建并标记事实依据与维护人。' });
      form.reset();
      setCreateOpen(false);
      void queryClient.invalidateQueries({ queryKey: ['faq', currentOrgId] });
    },
    onError: (err) => {
      setFeedback({ variant: 'danger', message: formatFaqError(err, '创建手工 FAQ 失败') });
    },
  });

  const disableMutation = useMutation({
    mutationFn: (faqId: string) => disableFaqItem(currentOrgId!, faqId),
    onSuccess: () => {
      setFeedback({ variant: 'success', message: 'FAQ 已停用（DISABLED），缓存失效事件已发出。' });
      void queryClient.invalidateQueries({ queryKey: ['faq', currentOrgId] });
    },
    onError: (err) => {
      setFeedback({ variant: 'danger', message: formatFaqError(err, '停用 FAQ 失败') });
    },
  });

  const publishMutation = useMutation({
    mutationFn: (faqId: string) => publishFaqVersion(currentOrgId!, faqId),
    onSuccess: () => {
      setFeedback({ variant: 'success', message: 'FAQ 已重新发布（PUBLISHED）。' });
      void queryClient.invalidateQueries({ queryKey: ['faq', currentOrgId] });
    },
    onError: (err) => {
      setFeedback({ variant: 'danger', message: formatFaqError(err, '发布 FAQ 失败') });
    },
  });

  const faqs = faqQuery.data ?? [];

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-3 border-b border-line pb-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="space-y-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="outline" className="font-mono text-[10px]">
              PRD-09
            </Badge>
            <h1 className="text-xl font-semibold tracking-tight text-ink">已发布 FAQ 与缓存治理</h1>
            <Badge variant="neutral">三轴状态解耦</Badge>
            <Badge variant="info">命中二次 ACL 复核</Badge>
          </div>
          <p className="text-sm text-ink-secondary">
            严格分离 Lifecycle、Publication（UNPUBLISHED / PUBLISHED / DISABLED）与 Source Health（HEALTHY / STALE / EXPIRED）三轴，绝不以 STALE 覆盖已发布事实。
          </p>
        </div>

        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => void faqQuery.refetch()}
            disabled={faqQuery.isFetching || !currentOrgId}
          >
            <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
            刷新
          </Button>

          <Dialog open={createOpen} onOpenChange={setCreateOpen}>
            <DialogTrigger
              render={
                <Button size="sm" disabled={!currentOrgId}>
                  <Plus className="h-3.5 w-3.5" aria-hidden="true" />
                  创建 MANUAL FAQ
                </Button>
              }
            />
            <DialogContent>
              <DialogHeader>
                <DialogTitle>创建手工 FAQ（MANUAL）</DialogTitle>
                <DialogDescription>
                  依据 PRD §6.5，手工 FAQ 必须标记 MANUAL，并强制填写事实依据、有效期限与维护人。
                </DialogDescription>
              </DialogHeader>
              <form
                className="mt-4 space-y-3"
                onSubmit={form.handleSubmit((vals) => createManualMutation.mutate(vals))}
                noValidate
              >
                <div className="space-y-1">
                  <label htmlFor="manual-faq-q" className="text-xs font-medium text-ink">
                    标准问题
                  </label>
                  <Input
                    id="manual-faq-q"
                    placeholder="例如：年假申请需要提前几天发起？"
                    invalid={Boolean(form.formState.errors.question)}
                    {...form.register('question')}
                  />
                  {form.formState.errors.question ? (
                    <p className="text-xs text-danger">{form.formState.errors.question.message}</p>
                  ) : null}
                </div>

                <div className="space-y-1">
                  <label htmlFor="manual-faq-a" className="text-xs font-medium text-ink">
                    标准回答
                  </label>
                  <Input
                    id="manual-faq-a"
                    placeholder="请提前 3 个工作日提交 OA 审批..."
                    invalid={Boolean(form.formState.errors.answer)}
                    {...form.register('answer')}
                  />
                  {form.formState.errors.answer ? (
                    <p className="text-xs text-danger">{form.formState.errors.answer.message}</p>
                  ) : null}
                </div>

                <div className="space-y-1">
                  <label htmlFor="manual-faq-evidence" className="text-xs font-medium text-ink">
                    事实依据（必填）
                  </label>
                  <Input
                    id="manual-faq-evidence"
                    placeholder="例如：《员工考勤制度 2026 版》第 4.2 条"
                    invalid={Boolean(form.formState.errors.evidence)}
                    {...form.register('evidence')}
                  />
                  {form.formState.errors.evidence ? (
                    <p className="text-xs text-danger">{form.formState.errors.evidence.message}</p>
                  ) : null}
                </div>

                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                  <div className="space-y-1">
                    <label htmlFor="manual-faq-maintainer" className="text-xs font-medium text-ink">
                      维护人（必填）
                    </label>
                    <Input
                      id="manual-faq-maintainer"
                      placeholder="hr@acme.local"
                      invalid={Boolean(form.formState.errors.maintainer)}
                      {...form.register('maintainer')}
                    />
                    {form.formState.errors.maintainer ? (
                      <p className="text-xs text-danger">
                        {form.formState.errors.maintainer.message}
                      </p>
                    ) : null}
                  </div>
                  <div className="space-y-1">
                    <label htmlFor="manual-faq-expires" className="text-xs font-medium text-ink">
                      有效期限（必填）
                    </label>
                    <Input
                      id="manual-faq-expires"
                      type="date"
                      invalid={Boolean(form.formState.errors.expires_at)}
                      {...form.register('expires_at')}
                    />
                    {form.formState.errors.expires_at ? (
                      <p className="text-xs text-danger">
                        {form.formState.errors.expires_at.message}
                      </p>
                    ) : null}
                  </div>
                </div>

                <DialogFooter>
                  <DialogClose render={<Button variant="outline" size="sm">取消</Button>} />
                  <Button type="submit" size="sm" loading={createManualMutation.isPending}>
                    保存手工 FAQ
                  </Button>
                </DialogFooter>
              </form>
            </DialogContent>
          </Dialog>
        </div>
      </div>

      {feedback ? <Alert variant={feedback.variant}>{feedback.message}</Alert> : null}

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <DatabaseZap className="h-4 w-4 text-ink-muted" aria-hidden="true" />
            FAQ 三轴状态与 Redis 缓存台账（{faqs.length}）
          </CardTitle>
          <CardDescription>
            Cache Key 绑定 organization_id、space_id、faq_id、faq_version、faq_acl_revision 与 source_version_hash。
          </CardDescription>
        </CardHeader>

        <CardContent>
          {faqQuery.isLoading ? (
            <div className="space-y-2.5">
              <Skeleton shape="block" className="h-12 w-full" />
              <Skeleton shape="block" className="h-12 w-full" />
            </div>
          ) : faqQuery.isError ? (
            <Alert variant="danger" title="加载 FAQ 台账失败">
              {formatFaqError(faqQuery.error, '无法获取已发布 FAQ 列表')}
            </Alert>
          ) : faqs.length === 0 ? (
            <div className="flex flex-col items-center justify-center rounded-lg border border-dashed border-line py-10 text-center">
              <Inbox className="h-5 w-5 text-ink-muted" aria-hidden="true" />
              <p className="mt-2 text-sm font-medium text-ink">暂无受管 FAQ 记录</p>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>FAQ 问题 / 版本</TableHead>
                  <TableHead>轴 1：Lifecycle</TableHead>
                  <TableHead>轴 2：Publication</TableHead>
                  <TableHead>轴 3：Source Health</TableHead>
                  <TableHead>Redis 缓存与命中</TableHead>
                  <TableHead className="text-right">操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {faqs.map((faq) => (
                  <TableRow key={faq.id}>
                    <TableCell>
                      <div className="flex items-center gap-1.5">
                        <span className="font-medium text-ink">{faq.question}</span>
                        {faq.isManual ? <Badge variant="outline">MANUAL</Badge> : null}
                      </div>
                      <p className="font-mono text-[11px] text-ink-muted">
                        v{faq.versionNo} · ACL rev #{faq.aclRevision ?? 1}
                      </p>
                    </TableCell>
                    <TableCell>
                      <Badge variant="neutral" className="font-mono text-[10px]">
                        {faq.lifecycleState}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      <Badge
                        variant={
                          faq.publicationState === 'PUBLISHED'
                            ? 'success'
                            : faq.publicationState === 'DISABLED'
                              ? 'danger'
                              : 'warning'
                        }
                        className="font-mono text-[10px]"
                      >
                        {faq.publicationState}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      <Badge
                        variant={
                          faq.sourceHealthState === 'HEALTHY'
                            ? 'success'
                            : faq.sourceHealthState === 'STALE'
                              ? 'warning'
                              : 'danger'
                        }
                        className="font-mono text-[10px]"
                      >
                        {faq.sourceHealthState}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-xs text-ink-secondary">
                      <span className="font-mono">{faq.cacheStatus ?? 'SYNCED'}</span> · 命中{' '}
                      {faq.cacheHitCount ?? 0} 次
                    </TableCell>
                    <TableCell className="text-right">
                      {faq.publicationState === 'PUBLISHED' ? (
                        <Button
                          variant="outline"
                          size="sm"
                          loading={disableMutation.isPending}
                          onClick={() => disableMutation.mutate(faq.id)}
                        >
                          <Ban className="h-3.5 w-3.5" aria-hidden="true" />
                          停用
                        </Button>
                      ) : (
                        <Button
                          size="sm"
                          loading={publishMutation.isPending}
                          onClick={() => publishMutation.mutate(faq.id)}
                        >
                          <BookCheck className="h-3.5 w-3.5" aria-hidden="true" />
                          发布
                        </Button>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
