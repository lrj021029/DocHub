import { useMemo, useState } from 'react';
import { zodResolver } from '@hookform/resolvers/zod';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Inbox, RefreshCw, Search, UserPlus } from 'lucide-react';
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
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Skeleton,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui';
import {
  fetchOrgMembers,
  formatIamError,
  inviteOrgMember,
  updateOrgMemberStatus,
} from '@/features/iam/api';
import { useAuth } from '@/lib/auth-store';
import type { OrganizationMemberItem } from '@/types/domain';

/** 邀请/添加成员表单校验 */
const inviteMemberSchema = z.object({
  email: z.email('请输入有效的企业邮箱'),
  display_name: z.string().trim().min(2, '显示名称不少于 2 个字符').max(64, '显示名称不超过 64 个字符'),
  password: z
    .string()
    .optional()
    .refine((val) => !val || val.length >= 8, '若设置初始密码，长度不得少于 8 位'),
});

type InviteMemberValues = z.infer<typeof inviteMemberSchema>;

export interface MembersTabProps {
  orgId: string;
}

/**
 * IAM 选项卡 1：组织成员管理。
 * 对接 GET / POST / PATCH /api/v1/organizations/{org_id}/members。
 * 注意：操作按钮依据前端 permissions 做体验层优雅显隐，后端仍对所有请求强制二次鉴权。
 */
export function MembersTab({ orgId }: MembersTabProps) {
  const queryClient = useQueryClient();
  const { hasPermission } = useAuth();
  const [searchKeyword, setSearchKeyword] = useState('');
  const [statusFilter, setStatusFilter] = useState<'ALL' | 'ACTIVE' | 'DISABLED'>('ALL');
  const [inviteOpen, setInviteOpen] = useState(false);
  const [actionFeedback, setActionFeedback] = useState<{
    variant: 'success' | 'danger';
    message: string;
  } | null>(null);

  // 前端仅作体验层显隐控制；后端接口始终执行权威 require_permission 校验
  const canManageMembers =
    hasPermission('org:members:write') ||
    hasPermission('member:manage') ||
    hasPermission('org:manage') ||
    hasPermission('organization:manage');

  const membersQuery = useQuery({
    queryKey: ['iam', orgId, 'members', statusFilter],
    queryFn: () =>
      fetchOrgMembers(orgId, {
        status: statusFilter === 'ALL' ? undefined : statusFilter,
      }),
    enabled: Boolean(orgId),
  });

  const {
    register,
    handleSubmit,
    reset,
    formState: { errors },
  } = useForm<InviteMemberValues>({
    resolver: zodResolver(inviteMemberSchema),
    defaultValues: { email: '', display_name: '', password: '' },
  });

  const inviteMutation = useMutation({
    mutationFn: (values: InviteMemberValues) =>
      inviteOrgMember(orgId, {
        email: values.email,
        display_name: values.display_name,
        password: values.password?.trim() || undefined,
      }),
    onSuccess: () => {
      setActionFeedback({ variant: 'success', message: '成员已加入当前组织。' });
      reset();
      setInviteOpen(false);
      void queryClient.invalidateQueries({ queryKey: ['iam', orgId, 'members'] });
    },
    onError: (error) => {
      setActionFeedback({ variant: 'danger', message: formatIamError(error, '添加成员失败') });
    },
  });

  const toggleStatusMutation = useMutation({
    mutationFn: ({ memberId, nextStatus }: { memberId: string; nextStatus: 'ACTIVE' | 'DISABLED' }) =>
      updateOrgMemberStatus(orgId, memberId, nextStatus),
    onSuccess: (_, vars) => {
      setActionFeedback({
        variant: 'success',
        message: vars.nextStatus === 'ACTIVE' ? '已重新启用该成员。' : '已停用该成员在当前组织的访问权。',
      });
      void queryClient.invalidateQueries({ queryKey: ['iam', orgId, 'members'] });
    },
    onError: (error) => {
      setActionFeedback({ variant: 'danger', message: formatIamError(error, '更新成员状态失败') });
    },
  });

  const filteredMembers = useMemo(() => {
    const items = membersQuery.data ?? [];
    const kw = searchKeyword.trim().toLowerCase();
    if (!kw) return items;
    return items.filter(
      (m) =>
        m.email.toLowerCase().includes(kw) ||
        m.displayName.toLowerCase().includes(kw) ||
        m.organizationRoles.some((r) => r.toLowerCase().includes(kw)),
    );
  }, [membersQuery.data, searchKeyword]);

  const handleToggleMember = (member: OrganizationMemberItem) => {
    const nextStatus = member.status === 'ACTIVE' ? 'DISABLED' : 'ACTIVE';
    toggleStatusMutation.mutate({ memberId: member.id, nextStatus });
  };

  return (
    <Card>
      <CardHeader className="flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <CardTitle>组织成员台账</CardTitle>
          <CardDescription>
            管理当前 Organization 成员状态与组织角色归属。停用后该成员在本组织下的所有知识访问立即失效。
          </CardDescription>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => void membersQuery.refetch()}
            disabled={membersQuery.isFetching}
          >
            <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
            刷新
          </Button>

          {canManageMembers ? (
            <Dialog open={inviteOpen} onOpenChange={setInviteOpen}>
              <DialogTrigger
                render={
                  <Button size="sm">
                    <UserPlus className="h-3.5 w-3.5" aria-hidden="true" />
                    邀请 / 添加成员
                  </Button>
                }
              />
              <DialogContent>
                <DialogHeader>
                  <DialogTitle>添加组织成员</DialogTitle>
                  <DialogDescription>
                    向当前 Organization 添加已有或新企业账号；后端将强制校验调用者的成员管理权限。
                  </DialogDescription>
                </DialogHeader>

                <form
                  className="mt-4 space-y-3"
                  onSubmit={handleSubmit((vals) => inviteMutation.mutate(vals))}
                  noValidate
                >
                  <div className="space-y-1">
                    <label htmlFor="invite-email" className="text-xs font-medium text-ink">
                      企业邮箱
                    </label>
                    <Input
                      id="invite-email"
                      type="email"
                      placeholder="member@acme.local"
                      invalid={Boolean(errors.email)}
                      {...register('email')}
                    />
                    {errors.email ? (
                      <p className="text-xs text-danger">{errors.email.message}</p>
                    ) : null}
                  </div>

                  <div className="space-y-1">
                    <label htmlFor="invite-name" className="text-xs font-medium text-ink">
                      成员姓名
                    </label>
                    <Input
                      id="invite-name"
                      placeholder="例如：张明"
                      invalid={Boolean(errors.display_name)}
                      {...register('display_name')}
                    />
                    {errors.display_name ? (
                      <p className="text-xs text-danger">{errors.display_name.message}</p>
                    ) : null}
                  </div>

                  <div className="space-y-1">
                    <label htmlFor="invite-password" className="text-xs font-medium text-ink">
                      初始密码（可选，新账号生效）
                    </label>
                    <Input
                      id="invite-password"
                      type="password"
                      placeholder="留空则关联已有平台账号"
                      invalid={Boolean(errors.password)}
                      {...register('password')}
                    />
                    {errors.password ? (
                      <p className="text-xs text-danger">{errors.password.message}</p>
                    ) : null}
                  </div>

                  <DialogFooter>
                    <DialogClose render={<Button variant="outline" size="sm">取消</Button>} />
                    <Button type="submit" size="sm" loading={inviteMutation.isPending}>
                      确认添加
                    </Button>
                  </DialogFooter>
                </form>
              </DialogContent>
            </Dialog>
          ) : null}
        </div>
      </CardHeader>

      <CardContent className="space-y-4">
        {actionFeedback ? (
          <Alert variant={actionFeedback.variant}>{actionFeedback.message}</Alert>
        ) : null}

        {/* 筛选工具栏 */}
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <div className="relative flex-1">
            <Search
              className="pointer-events-none absolute top-2.5 left-3 h-4 w-4 text-ink-muted"
              aria-hidden="true"
            />
            <Input
              aria-label="按邮箱或姓名筛选成员"
              placeholder="搜索邮箱、姓名或组织角色..."
              value={searchKeyword}
              onChange={(event) => setSearchKeyword(event.target.value)}
              className="pl-9"
            />
          </div>
          <div className="w-full sm:w-44">
            <Select
              value={statusFilter}
              onValueChange={(val) => {
                if (val === 'ALL' || val === 'ACTIVE' || val === 'DISABLED') {
                  setStatusFilter(val);
                }
              }}
            >
              <SelectTrigger aria-label="按成员状态筛选">
                <SelectValue placeholder="成员状态" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="ALL">全部状态</SelectItem>
                <SelectItem value="ACTIVE">仅启用（ACTIVE）</SelectItem>
                <SelectItem value="DISABLED">仅停用（DISABLED）</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>

        {/* Loading / Error / Empty / Data 四态完整覆盖 */}
        {membersQuery.isLoading ? (
          <div className="space-y-2.5" data-testid="members-loading">
            <Skeleton shape="line" className="w-1/3" />
            <Skeleton shape="block" className="h-12 w-full" />
            <Skeleton shape="block" className="h-12 w-full" />
          </div>
        ) : membersQuery.isError ? (
          <Alert variant="danger" title="加载组织成员失败">
            <p>{formatIamError(membersQuery.error, '无法获取成员列表')}</p>
            <Button
              variant="outline"
              size="sm"
              className="mt-2"
              onClick={() => void membersQuery.refetch()}
            >
              重新加载
            </Button>
          </Alert>
        ) : filteredMembers.length === 0 ? (
          <div
            className="flex flex-col items-center justify-center rounded-lg border border-dashed border-line py-10 text-center"
            data-testid="members-empty"
          >
            <Inbox className="h-5 w-5 text-ink-muted" aria-hidden="true" />
            <p className="mt-2 text-sm font-medium text-ink">未找到符合条件的组织成员</p>
            <p className="mt-1 text-xs text-ink-muted">
              可调整筛选条件，或由具备成员管理权限的管理员邀请新成员加入。
            </p>
          </div>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>成员信息</TableHead>
                <TableHead>组织角色（OrganizationRole）</TableHead>
                <TableHead>系统角色（SystemRole）</TableHead>
                <TableHead>状态</TableHead>
                {canManageMembers ? <TableHead className="text-right">操作</TableHead> : null}
              </TableRow>
            </TableHeader>
            <TableBody>
              {filteredMembers.map((member) => (
                <TableRow key={member.id}>
                  <TableCell>
                    <p className="font-medium text-ink">{member.displayName}</p>
                    <p className="text-xs text-ink-muted">{member.email}</p>
                  </TableCell>
                  <TableCell>
                    <div className="flex flex-wrap gap-1">
                      {member.organizationRoles.length > 0 ? (
                        member.organizationRoles.map((role) => (
                          <Badge key={role} variant="info">
                            {role}
                          </Badge>
                        ))
                      ) : (
                        <span className="text-xs text-ink-muted">未分配</span>
                      )}
                    </div>
                  </TableCell>
                  <TableCell>
                    <div className="flex flex-wrap gap-1">
                      {member.systemRoles.length > 0 ? (
                        member.systemRoles.map((role) => (
                          <Badge key={role} variant="warning">
                            {role}
                          </Badge>
                        ))
                      ) : (
                        <span className="text-xs text-ink-muted">—</span>
                      )}
                    </div>
                  </TableCell>
                  <TableCell>
                    <Badge variant={member.status === 'ACTIVE' ? 'success' : 'danger'}>
                      {member.status}
                    </Badge>
                  </TableCell>
                  {canManageMembers ? (
                    <TableCell className="text-right">
                      <Button
                        variant="outline"
                        size="sm"
                        loading={toggleStatusMutation.isPending}
                        onClick={() => handleToggleMember(member)}
                      >
                        {member.status === 'ACTIVE' ? '停用' : '启用'}
                      </Button>
                    </TableCell>
                  ) : null}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}
