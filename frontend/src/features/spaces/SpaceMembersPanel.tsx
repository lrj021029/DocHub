import { useState } from 'react';
import { zodResolver } from '@hookform/resolvers/zod';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Inbox, Lock, Trash2, UserPlus, Users } from 'lucide-react';
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
  Input,
  Skeleton,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui';
import { fetchOrgMembers } from '@/features/iam/api';
import {
  fetchSpaceMembers,
  formatSpaceError,
  patchSpaceMembers,
  type KnowledgeSpaceItem,
} from '@/features/spaces/api';

/** 新增私有 Space 成员表单校验 */
const addSpaceMemberSchema = z
  .object({
    user_id: z.string().trim().min(1, '请选择要授权的组织成员'),
    valid_from: z.string().optional(),
    valid_until: z.string().optional(),
  })
  .refine(
    (data) => {
      if (!data.valid_from || !data.valid_until) return true;
      return new Date(data.valid_until).getTime() > new Date(data.valid_from).getTime();
    },
    {
      message: '到期时间必须晚于生效时间',
      path: ['valid_until'],
    },
  );

type AddSpaceMemberValues = z.infer<typeof addSpaceMemberSchema>;

export interface SpaceMembersPanelProps {
  orgId: string;
  space: KnowledgeSpaceItem;
  canManage: boolean;
}

/**
 * 私有 Knowledge Space 显式入口成员管理面板。
 * 对接 GET / PATCH /api/v1/spaces/{space_id}/members，支持配置有效期并明确区分公开/私有入口边界。
 */
export function SpaceMembersPanel({ orgId, space, canManage }: SpaceMembersPanelProps) {
  const queryClient = useQueryClient();
  const [feedback, setFeedback] = useState<{ variant: 'success' | 'danger'; message: string } | null>(
    null,
  );

  const membersQuery = useQuery({
    queryKey: ['spaces', orgId, space.id, 'members'],
    queryFn: () => fetchSpaceMembers(orgId, space.id),
    enabled: Boolean(orgId && space.id && space.visibility === 'PRIVATE'),
  });

  const orgMembersQuery = useQuery({
    queryKey: ['iam', orgId, 'members', 'ALL'],
    queryFn: () => fetchOrgMembers(orgId),
    enabled: Boolean(orgId && space.visibility === 'PRIVATE' && canManage),
  });

  const form = useForm<AddSpaceMemberValues>({
    resolver: zodResolver(addSpaceMemberSchema),
    defaultValues: { user_id: '', valid_from: '', valid_until: '' },
  });

  const addMutation = useMutation({
    mutationFn: (values: AddSpaceMemberValues) =>
      patchSpaceMembers(orgId, space.id, {
        upsert: [
          {
            user_id: values.user_id,
            valid_from: values.valid_from ? new Date(values.valid_from).toISOString() : null,
            valid_until: values.valid_until ? new Date(values.valid_until).toISOString() : null,
            status: 'ACTIVE',
          },
        ],
      }),
    onSuccess: () => {
      setFeedback({ variant: 'success', message: '已成功更新私有 Space 显式入口成员。' });
      form.reset();
      void queryClient.invalidateQueries({ queryKey: ['spaces', orgId, space.id, 'members'] });
    },
    onError: (err) => {
      setFeedback({ variant: 'danger', message: formatSpaceError(err, '添加 Space 成员失败') });
    },
  });

  const removeMutation = useMutation({
    mutationFn: (userId: string) =>
      patchSpaceMembers(orgId, space.id, {
        remove_user_ids: [userId],
      }),
    onSuccess: () => {
      setFeedback({ variant: 'success', message: '已撤销该成员的私有 Space 入口授权。' });
      void queryClient.invalidateQueries({ queryKey: ['spaces', orgId, space.id, 'members'] });
    },
    onError: (err) => {
      setFeedback({ variant: 'danger', message: formatSpaceError(err, '移除 Space 成员失败') });
    },
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center justify-between gap-2">
          <span className="inline-flex items-center gap-2">
            {space.visibility === 'PRIVATE' ? (
              <Lock className="h-4 w-4 text-warning" aria-hidden="true" />
            ) : (
              <Users className="h-4 w-4 text-info" aria-hidden="true" />
            )}
            入口成员授权：{space.name}
          </span>
          <Badge variant={space.visibility === 'PUBLIC' ? 'info' : 'warning'}>
            {space.visibility === 'PUBLIC' ? 'PUBLIC（本组织公开）' : 'PRIVATE（显式成员）'}
          </Badge>
        </CardTitle>
        <CardDescription>
          Space 入口授权与 Document ACL 为两层独立校验；通过入口仅代表可进入空间，具体文档仍需满足四维 ACL。
        </CardDescription>
      </CardHeader>

      <CardContent className="space-y-4">
        {space.visibility === 'PUBLIC' ? (
          <Alert variant="info" title="组织内公开入口边界声明（PRD §3.2）">
            当前 Space 设为 <strong>PUBLIC</strong>，仅允许<strong>当前 Organization 内的所有有效成员</strong>
            进入，<strong>绝不代表跨组织或互联网公开</strong>。如需限制仅指定成员进入，请将可见性切换为 PRIVATE。
          </Alert>
        ) : (
          <>
            {feedback ? <Alert variant={feedback.variant}>{feedback.message}</Alert> : null}

            {/* 新增 / 续期成员表单 */}
            {canManage ? (
              <form
                onSubmit={form.handleSubmit((vals) => addMutation.mutate(vals))}
                className="grid grid-cols-1 gap-3 rounded-lg border border-line bg-surface-subtle p-3.5 md:grid-cols-4"
                noValidate
              >
                <div className="space-y-1 md:col-span-2">
                  <label htmlFor="space-member-user" className="text-xs font-medium text-ink">
                    选择本组织成员
                  </label>
                  <select
                    id="space-member-user"
                    className="flex h-9 w-full rounded-lg border border-line bg-surface-elevated px-3 text-sm text-ink shadow-xs"
                    {...form.register('user_id')}
                  >
                    <option value="">请选择成员...</option>
                    {(orgMembersQuery.data ?? []).map((member) => (
                      <option key={member.userId} value={member.userId}>
                        {member.displayName}（{member.email}）
                      </option>
                    ))}
                  </select>
                  {form.formState.errors.user_id ? (
                    <p className="text-xs text-danger">{form.formState.errors.user_id.message}</p>
                  ) : null}
                </div>

                <div className="space-y-1">
                  <label htmlFor="space-member-until" className="text-xs font-medium text-ink">
                    到期时间（valid_until，可选）
                  </label>
                  <Input
                    id="space-member-until"
                    type="datetime-local"
                    invalid={Boolean(form.formState.errors.valid_until)}
                    {...form.register('valid_until')}
                  />
                  {form.formState.errors.valid_until ? (
                    <p className="text-xs text-danger">
                      {form.formState.errors.valid_until.message}
                    </p>
                  ) : null}
                </div>

                <div className="flex items-end">
                  <Button type="submit" size="sm" className="w-full" loading={addMutation.isPending}>
                    <UserPlus className="h-3.5 w-3.5" aria-hidden="true" />
                    授权进入 Space
                  </Button>
                </div>
              </form>
            ) : null}

            {/* 成员列表 */}
            {membersQuery.isLoading ? (
              <div className="space-y-2">
                <Skeleton shape="block" className="h-10 w-full" />
                <Skeleton shape="block" className="h-10 w-full" />
              </div>
            ) : membersQuery.isError ? (
              <Alert variant="danger" title="加载私有 Space 成员失败">
                {formatSpaceError(membersQuery.error, '无法读取 Space 成员清单')}
              </Alert>
            ) : (membersQuery.data ?? []).length === 0 ? (
              <div className="flex flex-col items-center justify-center rounded-lg border border-dashed border-line py-8 text-center">
                <Inbox className="h-5 w-5 text-ink-muted" aria-hidden="true" />
                <p className="mt-2 text-xs font-medium text-ink">
                  该私有 Space 暂无显式成员（默认拒绝所有普通用户进入）
                </p>
              </div>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>成员</TableHead>
                    <TableHead>状态</TableHead>
                    <TableHead>有效期（valid_until）</TableHead>
                    {canManage ? <TableHead className="text-right">操作</TableHead> : null}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {(membersQuery.data ?? []).map((item) => (
                    <TableRow key={item.id}>
                      <TableCell>
                        <p className="font-medium text-ink">
                          {item.userDisplayName ?? item.userId}
                        </p>
                        {item.userEmail ? (
                          <p className="text-xs text-ink-muted">{item.userEmail}</p>
                        ) : null}
                      </TableCell>
                      <TableCell>
                        <Badge variant={item.status === 'ACTIVE' ? 'success' : 'warning'}>
                          {item.status}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-xs text-ink-secondary">
                        {item.validUntil
                          ? `至 ${item.validUntil.slice(0, 16).replace('T', ' ')}`
                          : '长期有效'}
                      </TableCell>
                      {canManage ? (
                        <TableCell className="text-right">
                          <Button
                            variant="ghost"
                            size="sm"
                            aria-label="撤销 Space 成员入口权"
                            onClick={() => removeMutation.mutate(item.userId)}
                          >
                            <Trash2 className="h-3.5 w-3.5 text-danger" aria-hidden="true" />
                          </Button>
                        </TableCell>
                      ) : null}
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}
