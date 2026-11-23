import { useMemo, useState } from 'react';
import { zodResolver } from '@hookform/resolvers/zod';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Inbox, KeyRound, Plus, ShieldAlert, ShieldCheck, UserCheck } from 'lucide-react';
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
  assignOrgRole,
  createOrgRole,
  fetchOrgMembers,
  fetchOrgRoles,
  fetchPermissions,
  formatIamError,
} from '@/features/iam/api';
import { useAuth } from '@/lib/auth-store';
import type { PermissionItem, RoleItem } from '@/types/domain';

/** 创建组织角色校验 */
const createRoleSchema = z.object({
  code: z
    .string()
    .trim()
    .min(2, '角色代码不少于 2 位')
    .max(48, '角色代码不超过 48 位')
    .regex(/^[A-Z][A-Z0-9_]*$/, '请使用大写字母、数字与下划线（例如 HR_REVIEWER）'),
  name: z.string().trim().min(2, '角色名称不少于 2 个字符').max(64, '角色名称不超过 64 个字符'),
  description: z.string().max(200).optional(),
});

type CreateRoleValues = z.infer<typeof createRoleSchema>;

/** 分配组织角色校验 */
const assignRoleSchema = z.object({
  user_id: z.string().trim().min(1, '请选择要授权的组织成员'),
  valid_until: z.string().optional(),
});

type AssignRoleValues = z.infer<typeof assignRoleSchema>;

export interface RolesPermissionsTabProps {
  orgId: string;
}

/**
 * IAM 选项卡 3：组织角色（OrganizationRole）与全平台权限树。
 * 视觉强制区分 SystemRole 与 OrganizationRole，明确提示系统管理员不自动拥有文档读取权。
 */
export function RolesPermissionsTab({ orgId }: RolesPermissionsTabProps) {
  const queryClient = useQueryClient();
  const { user, hasPermission } = useAuth();
  const [createOpen, setCreateOpen] = useState(false);
  const [assignRoleTarget, setAssignRoleTarget] = useState<RoleItem | null>(null);
  const [selectedPermCodes, setSelectedPermCodes] = useState<string[]>([]);
  const [feedback, setFeedback] = useState<{
    variant: 'success' | 'danger';
    message: string;
  } | null>(null);

  // 前端仅用于体验层显隐；后端接口对每个写操作强制执行 require_permission 校验
  const canManageRoles =
    hasPermission('org:roles:write') ||
    hasPermission('role:manage') ||
    hasPermission('org:manage') ||
    hasPermission('organization:manage');

  const rolesQuery = useQuery({
    queryKey: ['iam', orgId, 'roles'],
    queryFn: () => fetchOrgRoles(orgId),
    enabled: Boolean(orgId),
  });

  const permissionsQuery = useQuery({
    queryKey: ['iam', 'permissions'],
    queryFn: () => fetchPermissions(),
  });

  const membersQuery = useQuery({
    queryKey: ['iam', orgId, 'members', 'ALL'],
    queryFn: () => fetchOrgMembers(orgId),
    enabled: Boolean(orgId),
  });

  const createForm = useForm<CreateRoleValues>({
    resolver: zodResolver(createRoleSchema),
    defaultValues: { code: '', name: '', description: '' },
  });

  const assignForm = useForm<AssignRoleValues>({
    resolver: zodResolver(assignRoleSchema),
    defaultValues: { user_id: '', valid_until: '' },
  });

  const createRoleMutation = useMutation({
    mutationFn: (values: CreateRoleValues) =>
      createOrgRole(orgId, {
        code: values.code,
        name: values.name,
        description: values.description || undefined,
        permission_codes: selectedPermCodes,
      }),
    onSuccess: (created) => {
      setFeedback({ variant: 'success', message: `组织角色「${created.name} (${created.code})」创建成功。` });
      createForm.reset();
      setSelectedPermCodes([]);
      setCreateOpen(false);
      void queryClient.invalidateQueries({ queryKey: ['iam', orgId, 'roles'] });
    },
    onError: (err) => {
      setFeedback({ variant: 'danger', message: formatIamError(err, '创建组织角色失败') });
    },
  });

  const assignRoleMutation = useMutation({
    mutationFn: (values: AssignRoleValues) =>
      assignOrgRole(orgId, assignRoleTarget!.id, {
        user_id: values.user_id,
        valid_until: values.valid_until ? new Date(values.valid_until).toISOString() : null,
      }),
    onSuccess: () => {
      setFeedback({
        variant: 'success',
        message: `已成功为成员授予组织角色「${assignRoleTarget?.name ?? ''}」。`,
      });
      assignForm.reset();
      setAssignRoleTarget(null);
      void queryClient.invalidateQueries({ queryKey: ['iam', orgId, 'members'] });
      void queryClient.invalidateQueries({ queryKey: ['iam', orgId, 'roles'] });
    },
    onError: (err) => {
      setFeedback({ variant: 'danger', message: formatIamError(err, '分配角色失败') });
    },
  });

  /** 将权限清单按类别分组形成权限树视图 */
  const groupedPermissions = useMemo(() => {
    const groups = new Map<string, PermissionItem[]>();
    for (const perm of permissionsQuery.data ?? []) {
      const bucket = groups.get(perm.category) ?? [];
      bucket.push(perm);
      groups.set(perm.category, bucket);
    }
    return Array.from(groups.entries()).sort(([a], [b]) => a.localeCompare(b));
  }, [permissionsQuery.data]);

  const togglePermissionCode = (code: string) => {
    setSelectedPermCodes((prev) =>
      prev.includes(code) ? prev.filter((item) => item !== code) : [...prev, code],
    );
  };

  const systemRoles = rolesQuery.data?.systemRoles ?? [];
  const organizationRoles = rolesQuery.data?.organizationRoles ?? [];

  return (
    <div className="space-y-4">
      {/* 强制展示 SystemRole 与 OrganizationRole 物理分离安全声明 */}
      <Alert variant="warning" title="SystemRole 与 OrganizationRole 严格分离（PRD §3.3）">
        系统角色（如 <span className="font-mono font-semibold">SYSTEM_ADMIN</span>
        ）仅用于平台基础设施与租户运维，<strong>绝不自动拥有任何文档读取权限</strong>
        ；Document ACL 的 Role 主体仅允许引用当前 Organization 的 OrganizationRole。
      </Alert>

      {feedback ? <Alert variant={feedback.variant}>{feedback.message}</Alert> : null}

      {/* 分区 A：平台系统角色（只读安全边界视图） */}
      <Card>
        <CardHeader className="py-3.5">
          <CardTitle className="flex items-center justify-between gap-2">
            <span className="inline-flex items-center gap-2">
              <ShieldAlert className="h-4 w-4 text-warning" aria-hidden="true" />
              平台系统角色（SystemRole — 非知识读取主体）
            </span>
            <Badge variant="warning">平台域隔离</Badge>
          </CardTitle>
          <CardDescription>
            系统级角色与组织角色权限表物理分离；当前账号持有的 SystemRole：
            {user?.systemRoles.length ? user.systemRoles.join('、') : '无'}
          </CardDescription>
        </CardHeader>
        {systemRoles.length > 0 ? (
          <CardContent className="pt-0">
            <div className="flex flex-wrap gap-2">
              {systemRoles.map((role) => (
                <div
                  key={role.id}
                  className="flex items-center gap-2 rounded-lg border border-warning-line bg-warning-bg/40 px-3 py-1.5 text-xs"
                >
                  <Badge variant="warning" className="font-mono">
                    {role.code}
                  </Badge>
                  <span className="font-medium text-ink">{role.name}</span>
                </div>
              ))}
            </div>
          </CardContent>
        ) : null}
      </Card>

      {/* 分区 B：当前组织业务角色（OrganizationRole） */}
      <Card>
        <CardHeader className="flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <CardTitle className="flex items-center gap-2">
              <ShieldCheck className="h-4 w-4 text-info" aria-hidden="true" />
              组织业务角色（OrganizationRole）
            </CardTitle>
            <CardDescription>
              仅作用于当前 Organization，可直接绑定功能权限树并作为 Document ACL 的 Role 授权主体。
            </CardDescription>
          </div>

          {canManageRoles ? (
            <Dialog open={createOpen} onOpenChange={setCreateOpen}>
              <DialogTrigger
                render={
                  <Button size="sm">
                    <Plus className="h-3.5 w-3.5" aria-hidden="true" />
                    新建组织角色
                  </Button>
                }
              />
              <DialogContent className="max-w-lg">
                <DialogHeader>
                  <DialogTitle>新建 OrganizationRole</DialogTitle>
                  <DialogDescription>
                    定义当前组织的业务角色并勾选功能权限代码（后端将二次校验合法性与操作权）。
                  </DialogDescription>
                </DialogHeader>
                <form
                  className="mt-4 space-y-3"
                  onSubmit={createForm.handleSubmit((vals) => createRoleMutation.mutate(vals))}
                  noValidate
                >
                  <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                    <div className="space-y-1">
                      <label htmlFor="role-code" className="text-xs font-medium text-ink">
                        角色代码
                      </label>
                      <Input
                        id="role-code"
                        placeholder="HR_SPECIALIST"
                        invalid={Boolean(createForm.formState.errors.code)}
                        {...createForm.register('code')}
                      />
                      {createForm.formState.errors.code ? (
                        <p className="text-xs text-danger">
                          {createForm.formState.errors.code.message}
                        </p>
                      ) : null}
                    </div>
                    <div className="space-y-1">
                      <label htmlFor="role-name" className="text-xs font-medium text-ink">
                        角色名称
                      </label>
                      <Input
                        id="role-name"
                        placeholder="人力资源专员"
                        invalid={Boolean(createForm.formState.errors.name)}
                        {...createForm.register('name')}
                      />
                      {createForm.formState.errors.name ? (
                        <p className="text-xs text-danger">
                          {createForm.formState.errors.name.message}
                        </p>
                      ) : null}
                    </div>
                  </div>

                  <div className="space-y-1">
                    <label htmlFor="role-desc" className="text-xs font-medium text-ink">
                      职责说明（可选）
                    </label>
                    <Input
                      id="role-desc"
                      placeholder="负责薪酬与员工手册知识维护"
                      {...createForm.register('description')}
                    />
                  </div>

                  <div className="space-y-1.5">
                    <p className="text-xs font-medium text-ink">
                      勾选组织功能权限（已选 {selectedPermCodes.length} 项）
                    </p>
                    <div className="max-h-44 space-y-2 overflow-y-auto rounded-lg border border-line bg-surface-subtle p-3">
                      {(permissionsQuery.data ?? [])
                        .filter((p) => p.scope !== 'SYSTEM')
                        .map((perm) => (
                          <label
                            key={perm.code}
                            className="flex cursor-pointer items-start gap-2 text-xs text-ink"
                          >
                            <input
                              type="checkbox"
                              checked={selectedPermCodes.includes(perm.code)}
                              onChange={() => togglePermissionCode(perm.code)}
                              className="mt-0.5 h-3.5 w-3.5 rounded border-line accent-accent"
                            />
                            <span>
                              <span className="font-mono font-medium">{perm.code}</span>
                              <span className="ml-1.5 text-ink-muted">{perm.description}</span>
                            </span>
                          </label>
                        ))}
                    </div>
                  </div>

                  <DialogFooter>
                    <DialogClose render={<Button variant="outline" size="sm">取消</Button>} />
                    <Button type="submit" size="sm" loading={createRoleMutation.isPending}>
                      创建角色
                    </Button>
                  </DialogFooter>
                </form>
              </DialogContent>
            </Dialog>
          ) : null}
        </CardHeader>

        <CardContent>
          {rolesQuery.isLoading ? (
            <div className="space-y-2" data-testid="roles-loading">
              <Skeleton shape="block" className="h-12 w-full" />
              <Skeleton shape="block" className="h-12 w-full" />
            </div>
          ) : rolesQuery.isError ? (
            <Alert variant="danger" title="加载组织角色失败">
              {formatIamError(rolesQuery.error, '无法读取组织角色清单')}
            </Alert>
          ) : organizationRoles.length === 0 ? (
            <div className="flex flex-col items-center justify-center rounded-lg border border-dashed border-line py-8 text-center">
              <Inbox className="h-5 w-5 text-ink-muted" aria-hidden="true" />
              <p className="mt-2 text-xs font-medium text-ink">当前组织暂无 OrganizationRole</p>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>角色标识 / 名称</TableHead>
                  <TableHead>角色域</TableHead>
                  <TableHead>关联功能权限</TableHead>
                  {canManageRoles ? <TableHead className="text-right">操作</TableHead> : null}
                </TableRow>
              </TableHeader>
              <TableBody>
                {organizationRoles.map((role) => (
                  <TableRow key={role.id}>
                    <TableCell>
                      <p className="font-medium text-ink">{role.name}</p>
                      <p className="font-mono text-[11px] text-ink-muted">{role.code}</p>
                    </TableCell>
                    <TableCell>
                      <Badge variant="info">OrganizationRole</Badge>
                    </TableCell>
                    <TableCell>
                      <div className="flex flex-wrap gap-1">
                        {role.permissions.length > 0 ? (
                          role.permissions.map((code) => (
                            <Badge key={code} variant="outline" className="font-mono text-[10px]">
                              {code}
                            </Badge>
                          ))
                        ) : (
                          <span className="text-xs text-ink-muted">仅数据 ACL 主体</span>
                        )}
                      </div>
                    </TableCell>
                    {canManageRoles ? (
                      <TableCell className="text-right">
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => {
                            assignForm.reset({ user_id: '', valid_until: '' });
                            setAssignRoleTarget(role);
                          }}
                        >
                          <UserCheck className="h-3.5 w-3.5" aria-hidden="true" />
                          分配成员
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

      {/* 分区 C：全平台权限树只读目录 */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <KeyRound className="h-4 w-4 text-ink-muted" aria-hidden="true" />
            功能权限树（GET /api/v1/permissions）
          </CardTitle>
          <CardDescription>
            展示系统全部注册权限代码及其归属域；前端按钮显隐仅作体验优化，所有权限均由后端强校验。
          </CardDescription>
        </CardHeader>
        <CardContent>
          {permissionsQuery.isLoading ? (
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
              <Skeleton shape="block" className="h-24" />
              <Skeleton shape="block" className="h-24" />
            </div>
          ) : permissionsQuery.isError ? (
            <Alert variant="danger" title="加载权限树失败">
              {formatIamError(permissionsQuery.error, '无法获取权限树定义')}
            </Alert>
          ) : groupedPermissions.length === 0 ? (
            <p className="py-6 text-center text-xs text-ink-muted">暂未加载到权限树节点。</p>
          ) : (
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
              {groupedPermissions.map(([category, perms]) => (
                <div
                  key={category}
                  className="rounded-lg border border-line bg-surface-subtle p-3.5 space-y-2"
                >
                  <div className="flex items-center justify-between">
                    <span className="font-mono text-xs font-semibold uppercase text-ink">
                      {category}
                    </span>
                    <Badge variant="neutral">{perms.length} 项</Badge>
                  </div>
                  <ul className="space-y-1.5">
                    {perms.map((perm) => (
                      <li key={perm.code} className="flex items-start justify-between gap-2 text-xs">
                        <div>
                          <span className="font-mono text-[11px] font-medium text-ink">
                            {perm.code}
                          </span>
                          <p className="text-[11px] text-ink-muted">{perm.description}</p>
                        </div>
                        <Badge
                          variant={perm.scope === 'SYSTEM' ? 'warning' : 'outline'}
                          className="text-[10px]"
                        >
                          {perm.scope}
                        </Badge>
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {/* 分配组织角色弹窗 */}
      <Dialog
        open={Boolean(assignRoleTarget)}
        onOpenChange={(open) => {
          if (!open) setAssignRoleTarget(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>为成员分配组织角色</DialogTitle>
            <DialogDescription>
              目标角色：{assignRoleTarget?.name}（{assignRoleTarget?.code}）
            </DialogDescription>
          </DialogHeader>
          <form
            className="mt-4 space-y-3"
            onSubmit={assignForm.handleSubmit((vals) => assignRoleMutation.mutate(vals))}
            noValidate
          >
            <div className="space-y-1">
              <label htmlFor="assign-role-user" className="text-xs font-medium text-ink">
                目标成员
              </label>
              <select
                id="assign-role-user"
                className="flex h-9 w-full rounded-lg border border-line bg-surface-elevated px-3 text-sm text-ink shadow-xs"
                {...assignForm.register('user_id')}
              >
                <option value="">请选择成员...</option>
                {(membersQuery.data ?? []).map((m) => (
                  <option key={m.userId} value={m.userId}>
                    {m.displayName}（{m.email}）
                  </option>
                ))}
              </select>
              {assignForm.formState.errors.user_id ? (
                <p className="text-xs text-danger">
                  {assignForm.formState.errors.user_id.message}
                </p>
              ) : null}
            </div>
            <div className="space-y-1">
              <label htmlFor="assign-role-until" className="text-xs font-medium text-ink">
                到期时间（可选，留空长期有效）
              </label>
              <Input
                id="assign-role-until"
                type="datetime-local"
                {...assignForm.register('valid_until')}
              />
            </div>
            <DialogFooter>
              <DialogClose render={<Button variant="outline" size="sm">取消</Button>} />
              <Button type="submit" size="sm" loading={assignRoleMutation.isPending}>
                确认分配
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  );
}
