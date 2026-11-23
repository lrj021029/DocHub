import { useState } from 'react';
import { zodResolver } from '@hookform/resolvers/zod';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  KeyRound,
  RefreshCw,
  Search,
  ShieldAlert,
  ShieldCheck,
  UserCheck,
  UserPlus,
  UserX,
} from 'lucide-react';
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
  createPlatformUser,
  fetchPlatformUsers,
  formatIamError,
  updatePlatformUser,
  type PlatformUserItem,
} from '@/features/iam/api';
import { useAuth } from '@/lib/auth-store';

/** 新建平台账号表单校验（密码需至少 8 位且同时包含字母与数字） */
const createPlatformUserSchema = z.object({
  email: z.email('请输入有效的企业邮箱地址'),
  display_name: z
    .string()
    .trim()
    .min(2, '显示名称不少于 2 个字符')
    .max(64, '显示名称不超过 64 个字符'),
  password: z
    .string()
    .min(8, '初始密码至少 8 位')
    .max(128, '密码不超过 128 位')
    .refine(
      (val) => /[A-Za-z]/.test(val) && /[0-9]/.test(val),
      '密码必须同时包含至少一个英文字母和一位数字',
    ),
  organization_role_code: z.enum(['EMPLOYEE', 'KNOWLEDGE_MANAGER', 'ORG_ADMIN', 'SECURITY_AUDITOR']),
  bind_current_org: z.boolean(),
  grant_system_admin: z.boolean(),
});

type CreatePlatformUserValues = z.infer<typeof createPlatformUserSchema>;

/** 管理员重置密码表单校验 */
const resetPasswordSchema = z.object({
  new_password: z
    .string()
    .min(8, '新密码至少 8 位')
    .max(128, '密码不超过 128 位')
    .refine(
      (val) => /[A-Za-z]/.test(val) && /[0-9]/.test(val),
      '密码必须同时包含至少一个英文字母和一位数字',
    ),
});

type ResetPasswordValues = z.infer<typeof resetPasswordSchema>;

export interface UsersAdminTabProps {
  currentOrgId: string | null;
  currentOrgName?: string | null;
}

/**
 * 平台级账号管理选项卡（Notion 极简治理风格）。
 * 对接 GET / POST / PATCH /api/v1/users（严格需要 system:user:manage 权限）。
 * 安全约束：
 * - 停用账号或重置密码后，后端立即吊销该用户全平台所有活跃 Refresh/Access 会话；
 * - 禁止管理员停用自身或撤销自身的 SYSTEM_ADMIN 角色；
 * - 授予 SYSTEM_ADMIN 仍严格遵守系统架构红线：不隐式授予任何受限文档读权。
 */
export function UsersAdminTab({ currentOrgId, currentOrgName }: UsersAdminTabProps) {
  const queryClient = useQueryClient();
  const { hasPermission, user: currentUser } = useAuth();
  const [searchKeyword, setSearchKeyword] = useState('');
  const [statusFilter, setStatusFilter] = useState<'ALL' | 'ACTIVE' | 'DISABLED' | 'LOCKED'>('ALL');
  const [createOpen, setCreateOpen] = useState(false);
  const [resetTarget, setResetTarget] = useState<PlatformUserItem | null>(null);
  const [feedback, setFeedback] = useState<{
    variant: 'success' | 'danger';
    message: string;
  } | null>(null);

  const canManagePlatformUsers = hasPermission('system:user:manage');

  const usersQuery = useQuery({
    queryKey: ['iam', 'platform-users', statusFilter, searchKeyword.trim()],
    queryFn: () =>
      fetchPlatformUsers({
        q: searchKeyword.trim() || undefined,
        status: statusFilter === 'ALL' ? undefined : statusFilter,
        limit: 100,
      }),
    enabled: canManagePlatformUsers,
    retry: false,
  });

  const createForm = useForm<CreatePlatformUserValues>({
    resolver: zodResolver(createPlatformUserSchema),
    defaultValues: {
      email: '',
      display_name: '',
      password: '',
      organization_role_code: 'EMPLOYEE',
      bind_current_org: Boolean(currentOrgId),
      grant_system_admin: false,
    },
  });

  const resetForm = useForm<ResetPasswordValues>({
    resolver: zodResolver(resetPasswordSchema),
    defaultValues: { new_password: '' },
  });

  const createMutation = useMutation({
    mutationFn: (values: CreatePlatformUserValues) =>
      createPlatformUser({
        email: values.email,
        display_name: values.display_name,
        password: values.password,
        grant_system_admin: values.grant_system_admin,
        organization_id: values.bind_current_org && currentOrgId ? currentOrgId : null,
        organization_role_code:
          values.bind_current_org && currentOrgId ? values.organization_role_code : null,
      }),
    onSuccess: (created) => {
      setFeedback({
        variant: 'success',
        message: `已创建平台账号 ${created.email}（${created.displayName}）。`,
      });
      createForm.reset({
        email: '',
        display_name: '',
        password: '',
        organization_role_code: 'EMPLOYEE',
        bind_current_org: Boolean(currentOrgId),
        grant_system_admin: false,
      });
      setCreateOpen(false);
      void queryClient.invalidateQueries({ queryKey: ['iam', 'platform-users'] });
      if (currentOrgId) {
        void queryClient.invalidateQueries({ queryKey: ['iam', currentOrgId, 'members'] });
      }
    },
    onError: (err) => {
      setFeedback({ variant: 'danger', message: formatIamError(err, '创建平台账号失败') });
    },
  });

  const updateMutation = useMutation({
    mutationFn: ({
      userId,
      patch,
    }: {
      userId: string;
      patch: Parameters<typeof updatePlatformUser>[1];
    }) => updatePlatformUser(userId, patch),
    onSuccess: (updated, vars) => {
      let msg = `已更新账号 ${updated.email}。`;
      if (vars.patch.new_password) {
        msg = `已重置 ${updated.email} 的登录密码，并立即吊销其全部活跃会话。`;
        setResetTarget(null);
        resetForm.reset();
      } else if (vars.patch.status === 'DISABLED') {
        msg = `已停用账号 ${updated.email}，其所有活跃登录会话已强制失效。`;
      } else if (vars.patch.status === 'ACTIVE') {
        msg = `已重新启用账号 ${updated.email}。`;
      } else if (vars.patch.grant_system_admin !== undefined) {
        msg = vars.patch.grant_system_admin
          ? `已授予 ${updated.email} 平台 SYSTEM_ADMIN 角色。`
          : `已撤销 ${updated.email} 的平台 SYSTEM_ADMIN 角色。`;
      }
      setFeedback({ variant: 'success', message: msg });
      void queryClient.invalidateQueries({ queryKey: ['iam', 'platform-users'] });
    },
    onError: (err) => {
      setFeedback({ variant: 'danger', message: formatIamError(err, '更新账号失败') });
    },
  });

  if (!canManagePlatformUsers) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <ShieldAlert className="h-4 w-4 text-warning" aria-hidden="true" />
            平台账号全局管理
          </CardTitle>
          <CardDescription>
            全平台账号创建、启用/停用、密码重置与 SYSTEM_ADMIN 角色治理仅对平台系统管理员开放。
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Alert variant="warning" title="需要平台管理权限（system:user:manage）">
            当前账号（{currentUser?.email ?? '—'}）未持有 <code>SYSTEM_ADMIN</code> 平台角色。
            如需新增或管理本组织成员，请切换至左侧「成员管理」选项卡，或使用{' '}
            <code>admin@acme.local</code> 登录管理全平台账号。
          </Alert>
        </CardContent>
      </Card>
    );
  }

  const users = usersQuery.data?.items ?? [];

  return (
    <Card>
      <CardHeader className="flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <CardTitle>全平台账号台账</CardTitle>
          <CardDescription>
            跨组织统一治理平台账号状态、密码重置与 SYSTEM_ADMIN 授权。停用或重置密码会即时吊销该账号的全部活跃会话。
          </CardDescription>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => void usersQuery.refetch()}
            disabled={usersQuery.isFetching}
          >
            <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
            刷新
          </Button>

          <Dialog open={createOpen} onOpenChange={setCreateOpen}>
            <DialogTrigger
              render={
                <Button size="sm">
                  <UserPlus className="h-3.5 w-3.5" aria-hidden="true" />
                  新建平台账号
                </Button>
              }
            />
            <DialogContent>
              <DialogHeader>
                <DialogTitle>新建平台用户账号</DialogTitle>
                <DialogDescription>
                  使用 Argon2id 哈希存储初始口令；可选同步加入当前组织并分配组织角色。
                </DialogDescription>
              </DialogHeader>

              <form
                onSubmit={createForm.handleSubmit((values) => createMutation.mutate(values))}
                className="space-y-3.5"
                noValidate
              >
                <div className="space-y-1">
                  <label htmlFor="admin-user-email" className="block text-xs font-medium text-ink">
                    企业邮箱
                  </label>
                  <Input
                    id="admin-user-email"
                    type="email"
                    placeholder="new.user@acme.local"
                    invalid={Boolean(createForm.formState.errors.email)}
                    {...createForm.register('email')}
                  />
                  {createForm.formState.errors.email ? (
                    <p className="text-xs text-danger">
                      {createForm.formState.errors.email.message}
                    </p>
                  ) : null}
                </div>

                <div className="space-y-1">
                  <label htmlFor="admin-user-name" className="block text-xs font-medium text-ink">
                    显示姓名
                  </label>
                  <Input
                    id="admin-user-name"
                    placeholder="例如：张敏（产品研发部）"
                    invalid={Boolean(createForm.formState.errors.display_name)}
                    {...createForm.register('display_name')}
                  />
                  {createForm.formState.errors.display_name ? (
                    <p className="text-xs text-danger">
                      {createForm.formState.errors.display_name.message}
                    </p>
                  ) : null}
                </div>

                <div className="space-y-1">
                  <label
                    htmlFor="admin-user-password"
                    className="block text-xs font-medium text-ink"
                  >
                    初始登录密码
                  </label>
                  <Input
                    id="admin-user-password"
                    type="password"
                    placeholder="至少 8 位，需同时包含字母和数字"
                    invalid={Boolean(createForm.formState.errors.password)}
                    {...createForm.register('password')}
                  />
                  {createForm.formState.errors.password ? (
                    <p className="text-xs text-danger">
                      {createForm.formState.errors.password.message}
                    </p>
                  ) : null}
                </div>

                {currentOrgId ? (
                  <div className="space-y-2.5 rounded-md border border-line bg-surface-subtle p-3">
                    <label className="flex items-center gap-2 text-xs font-medium text-ink">
                      <input
                        type="checkbox"
                        className="h-3.5 w-3.5 rounded border-line accent-accent"
                        {...createForm.register('bind_current_org')}
                      />
                      <span>
                        创建时同步加入当前组织（{currentOrgName ?? currentOrgId}）
                      </span>
                    </label>

                    {createForm.watch('bind_current_org') ? (
                      <div className="space-y-1">
                        <label
                          htmlFor="admin-user-org-role"
                          className="block text-[11px] text-ink-muted"
                        >
                          初始组织角色（OrganizationRole）
                        </label>
                        <select
                          id="admin-user-org-role"
                          className="h-8 w-full rounded-md border border-line bg-surface px-2.5 text-xs text-ink focus:outline-2 focus:outline-ring"
                          {...createForm.register('organization_role_code')}
                        >
                          <option value="EMPLOYEE">EMPLOYEE（普通员工）</option>
                          <option value="KNOWLEDGE_MANAGER">KNOWLEDGE_MANAGER（知识库管理员）</option>
                          <option value="ORG_ADMIN">ORG_ADMIN（组织管理员）</option>
                          <option value="SECURITY_AUDITOR">SECURITY_AUDITOR（安全审计员）</option>
                        </select>
                      </div>
                    ) : null}
                  </div>
                ) : null}

                <label className="flex items-start gap-2 rounded-md border border-line bg-surface-subtle p-3 text-xs text-ink">
                  <input
                    type="checkbox"
                    className="mt-0.5 h-3.5 w-3.5 rounded border-line accent-accent"
                    {...createForm.register('grant_system_admin')}
                  />
                  <span>
                    <strong className="font-medium">授予平台 SYSTEM_ADMIN 角色</strong>
                    <span className="mt-0.5 block text-[11px] text-ink-muted">
                      仅授予平台组织/账号/配置管理权，严禁且不会隐式获得受限文档读权。
                    </span>
                  </span>
                </label>

                <DialogFooter>
                  <DialogClose render={<Button variant="outline" type="button">取消</Button>} />
                  <Button type="submit" loading={createMutation.isPending}>
                    确认创建
                  </Button>
                </DialogFooter>
              </form>
            </DialogContent>
          </Dialog>
        </div>
      </CardHeader>

      <CardContent className="space-y-4">
        {feedback ? (
          <Alert
            variant={feedback.variant}
            title={feedback.variant === 'success' ? '操作成功' : '操作失败'}
          >
            {feedback.message}
          </Alert>
        ) : null}

        {usersQuery.isError ? (
          <Alert variant="danger" title="加载平台账号失败">
            {formatIamError(usersQuery.error, '无法加载平台账号列表')}
          </Alert>
        ) : null}

        {/* 检索与状态筛选栏 */}
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <div className="relative flex-1">
            <Search
              className="pointer-events-none absolute top-1/2 left-2.5 h-3.5 w-3.5 -translate-y-1/2 text-ink-muted"
              aria-hidden="true"
            />
            <Input
              value={searchKeyword}
              onChange={(e) => setSearchKeyword(e.target.value)}
              placeholder="按邮箱或姓名搜索平台账号..."
              className="pl-8"
              aria-label="搜索平台账号"
            />
          </div>
          <div className="w-full sm:w-40">
            <Select
              value={statusFilter}
              onValueChange={(val) => {
                if (val === 'ALL' || val === 'ACTIVE' || val === 'DISABLED' || val === 'LOCKED') {
                  setStatusFilter(val);
                }
              }}
            >
              <SelectTrigger aria-label="按账号状态筛选">
                <SelectValue placeholder="全部状态" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="ALL">全部状态</SelectItem>
                <SelectItem value="ACTIVE">仅启用（ACTIVE）</SelectItem>
                <SelectItem value="DISABLED">已停用（DISABLED）</SelectItem>
                <SelectItem value="LOCKED">已锁定（LOCKED）</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>

        {/* 账号台账表格 */}
        {usersQuery.isLoading ? (
          <div className="space-y-2">
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
          </div>
        ) : (
          <div className="overflow-x-auto rounded-md border border-line">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>账号 / 姓名</TableHead>
                  <TableHead>账号状态</TableHead>
                  <TableHead>平台角色</TableHead>
                  <TableHead>所属组织</TableHead>
                  <TableHead className="text-right">治理操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {users.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={5} className="py-8 text-center text-xs text-ink-muted">
                      未找到符合筛选条件的平台账号。
                    </TableCell>
                  </TableRow>
                ) : (
                  users.map((item) => {
                    const isSelf = item.id === currentUser?.id;
                    const isSysAdmin = item.systemRoles.includes('SYSTEM_ADMIN');
                    return (
                      <TableRow key={item.id}>
                        <TableCell>
                          <div className="space-y-0.5">
                            <div className="flex items-center gap-1.5">
                              <span className="font-medium text-ink">{item.displayName}</span>
                              {isSelf ? (
                                <Badge variant="outline" className="text-[10px]">
                                  当前登录
                                </Badge>
                              ) : null}
                            </div>
                            <p className="font-mono text-[11px] text-ink-muted">{item.email}</p>
                          </div>
                        </TableCell>
                        <TableCell>
                          <Badge
                            variant={
                              item.status === 'ACTIVE'
                                ? 'success'
                                : item.status === 'LOCKED'
                                  ? 'warning'
                                  : 'danger'
                            }
                          >
                            {item.status}
                          </Badge>
                        </TableCell>
                        <TableCell>
                          <div className="flex flex-wrap gap-1">
                            {item.systemRoles.length > 0 ? (
                              item.systemRoles.map((role) => (
                                <Badge key={role} variant="warning" className="text-[10px]">
                                  {role}
                                </Badge>
                              ))
                            ) : (
                              <span className="text-xs text-ink-muted">普通平台用户</span>
                            )}
                          </div>
                        </TableCell>
                        <TableCell>
                          <div className="flex flex-wrap gap-1">
                            {item.organizations.length > 0 ? (
                              item.organizations.map((org) => (
                                <Badge key={org.id} variant="neutral" className="text-[10px]">
                                  {org.slug}
                                </Badge>
                              ))
                            ) : (
                              <span className="text-xs text-ink-muted">未绑定组织</span>
                            )}
                          </div>
                        </TableCell>
                        <TableCell className="text-right">
                          <div className="flex flex-wrap items-center justify-end gap-1.5">
                            <Button
                              variant="outline"
                              size="sm"
                              className="h-7 px-2 text-xs"
                              disabled={updateMutation.isPending}
                              onClick={() => {
                                setResetTarget(item);
                                resetForm.reset({ new_password: '' });
                              }}
                            >
                              <KeyRound className="h-3 w-3" aria-hidden="true" />
                              重置密码
                            </Button>

                            <Button
                              variant="outline"
                              size="sm"
                              className="h-7 px-2 text-xs"
                              disabled={updateMutation.isPending || (isSelf && isSysAdmin)}
                              title={isSelf && isSysAdmin ? '禁止撤销自身的 SYSTEM_ADMIN 角色' : undefined}
                              onClick={() =>
                                updateMutation.mutate({
                                  userId: item.id,
                                  patch: { grant_system_admin: !isSysAdmin },
                                })
                              }
                            >
                              <ShieldCheck className="h-3 w-3" aria-hidden="true" />
                              {isSysAdmin ? '撤管理员' : '设管理员'}
                            </Button>

                            <Button
                              variant={item.status === 'ACTIVE' ? 'outline' : 'secondary'}
                              size="sm"
                              className="h-7 px-2 text-xs"
                              disabled={updateMutation.isPending || isSelf}
                              title={isSelf ? '禁止停用当前登录账号' : undefined}
                              onClick={() =>
                                updateMutation.mutate({
                                  userId: item.id,
                                  patch: {
                                    status: item.status === 'ACTIVE' ? 'DISABLED' : 'ACTIVE',
                                  },
                                })
                              }
                            >
                              {item.status === 'ACTIVE' ? (
                                <>
                                  <UserX className="h-3 w-3" aria-hidden="true" />
                                  停用
                                </>
                              ) : (
                                <>
                                  <UserCheck className="h-3 w-3" aria-hidden="true" />
                                  启用
                                </>
                              )}
                            </Button>
                          </div>
                        </TableCell>
                      </TableRow>
                    );
                  })
                )}
              </TableBody>
            </Table>
          </div>
        )}

        {/* 管理员重置密码弹窗 */}
        <Dialog
          open={Boolean(resetTarget)}
          onOpenChange={(open) => {
            if (!open) setResetTarget(null);
          }}
        >
          <DialogContent>
            <DialogHeader>
              <DialogTitle>重置用户登录密码</DialogTitle>
              <DialogDescription>
                为 {resetTarget?.displayName}（{resetTarget?.email}）设置新密码。提交后将立即强制吊销该用户的全部活跃会话。
              </DialogDescription>
            </DialogHeader>
            <form
              onSubmit={resetForm.handleSubmit((values) => {
                if (!resetTarget) return;
                updateMutation.mutate({
                  userId: resetTarget.id,
                  patch: { new_password: values.new_password },
                });
              })}
              className="space-y-4"
              noValidate
            >
              <div className="space-y-1">
                <label htmlFor="admin-reset-pwd" className="block text-xs font-medium text-ink">
                  新密码
                </label>
                <Input
                  id="admin-reset-pwd"
                  type="password"
                  placeholder="至少 8 位，需同时包含字母和数字"
                  invalid={Boolean(resetForm.formState.errors.new_password)}
                  {...resetForm.register('new_password')}
                />
                {resetForm.formState.errors.new_password ? (
                  <p className="text-xs text-danger">
                    {resetForm.formState.errors.new_password.message}
                  </p>
                ) : null}
              </div>
              <DialogFooter>
                <Button variant="outline" type="button" onClick={() => setResetTarget(null)}>
                  取消
                </Button>
                <Button type="submit" loading={updateMutation.isPending}>
                  确认重置并吊销会话
                </Button>
              </DialogFooter>
            </form>
          </DialogContent>
        </Dialog>
      </CardContent>
    </Card>
  );
}
