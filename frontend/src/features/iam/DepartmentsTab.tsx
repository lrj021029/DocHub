import { useMemo, useState } from 'react';
import { zodResolver } from '@hookform/resolvers/zod';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  FolderTree,
  GitBranch,
  Inbox,
  MoveRight,
  Plus,
  Trash2,
  UserPlus,
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
  Skeleton,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui';
import {
  assignDepartmentMembership,
  buildFlattenedDepartmentTree,
  createDepartment,
  fetchDepartmentMemberships,
  fetchDepartments,
  fetchOrgMembers,
  formatIamError,
  removeDepartmentMembership,
  updateDepartment,
} from '@/features/iam/api';
import { useAuth } from '@/lib/auth-store';
import { cn } from '@/lib/cn';

/** 新建部门校验 */
const createDeptSchema = z.object({
  name: z.string().trim().min(2, '部门名称不少于 2 个字符').max(64, '部门名称不超过 64 个字符'),
  parent_id: z.string().optional(),
});

type CreateDeptValues = z.infer<typeof createDeptSchema>;

/** 调整父部门校验 */
const moveDeptSchema = z.object({
  name: z.string().trim().min(2, '部门名称不少于 2 个字符').max(64, '部门名称不超过 64 个字符'),
  parent_id: z.string().optional(),
});

type MoveDeptValues = z.infer<typeof moveDeptSchema>;

/** 分配多部门兼职关系校验 */
const assignMembershipSchema = z
  .object({
    user_id: z.string().trim().min(1, '请选择目标组织成员'),
    membership_type: z.enum(['FULL_TIME', 'PART_TIME', 'TEMPORARY']),
    is_primary: z.boolean(),
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

type AssignMembershipValues = z.infer<typeof assignMembershipSchema>;

export interface DepartmentsTabProps {
  orgId: string;
}

/**
 * IAM 选项卡 2：部门树与多部门兼职管理。
 * 对接 GET/POST/PATCH /api/v1/organizations/{org_id}/departments 与 /memberships。
 */
export function DepartmentsTab({ orgId }: DepartmentsTabProps) {
  const queryClient = useQueryClient();
  const { hasPermission } = useAuth();
  const [selectedDeptId, setSelectedDeptId] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [moveOpen, setMoveOpen] = useState(false);
  const [assignOpen, setAssignOpen] = useState(false);
  const [feedback, setFeedback] = useState<{
    variant: 'success' | 'danger';
    message: string;
  } | null>(null);

  // 前端仅用于体验层按钮显隐；后端对所有写操作执行强制权限与环路检测
  const canManageDepartments =
    hasPermission('org:departments:write') ||
    hasPermission('department:manage') ||
    hasPermission('org:manage') ||
    hasPermission('organization:manage');

  const departmentsQuery = useQuery({
    queryKey: ['iam', orgId, 'departments'],
    queryFn: () => fetchDepartments(orgId),
    enabled: Boolean(orgId),
  });

  const membersQuery = useQuery({
    queryKey: ['iam', orgId, 'members', 'ALL'],
    queryFn: () => fetchOrgMembers(orgId),
    enabled: Boolean(orgId),
  });

  const flattenedRows = useMemo(
    () => buildFlattenedDepartmentTree(departmentsQuery.data ?? []),
    [departmentsQuery.data],
  );

  const activeDeptId = selectedDeptId ?? flattenedRows[0]?.node.id ?? null;
  const activeDeptRow = flattenedRows.find((r) => r.node.id === activeDeptId) ?? null;

  const membershipsQuery = useQuery({
    queryKey: ['iam', orgId, 'department-memberships', activeDeptId],
    queryFn: () => fetchDepartmentMemberships(orgId, activeDeptId!),
    enabled: Boolean(orgId && activeDeptId),
  });

  const createForm = useForm<CreateDeptValues>({
    resolver: zodResolver(createDeptSchema),
    defaultValues: { name: '', parent_id: '' },
  });

  const moveForm = useForm<MoveDeptValues>({
    resolver: zodResolver(moveDeptSchema),
    defaultValues: { name: '', parent_id: '' },
  });

  const membershipForm = useForm<AssignMembershipValues>({
    resolver: zodResolver(assignMembershipSchema),
    defaultValues: {
      user_id: '',
      membership_type: 'FULL_TIME',
      is_primary: false,
      valid_from: '',
      valid_until: '',
    },
  });

  const createDeptMutation = useMutation({
    mutationFn: (values: CreateDeptValues) =>
      createDepartment(orgId, {
        name: values.name,
        parent_id: values.parent_id || null,
      }),
    onSuccess: (created) => {
      setFeedback({ variant: 'success', message: `部门「${created.name}」创建成功，Closure Table 已同步。` });
      createForm.reset();
      setCreateOpen(false);
      setSelectedDeptId(created.id);
      void queryClient.invalidateQueries({ queryKey: ['iam', orgId, 'departments'] });
    },
    onError: (err) => {
      setFeedback({ variant: 'danger', message: formatIamError(err, '创建部门失败') });
    },
  });

  const moveDeptMutation = useMutation({
    mutationFn: (values: MoveDeptValues) =>
      updateDepartment(orgId, activeDeptId!, {
        name: values.name,
        parent_id: values.parent_id || null,
      }),
    onSuccess: () => {
      setFeedback({ variant: 'success', message: '部门信息与父级层级已更新。' });
      setMoveOpen(false);
      void queryClient.invalidateQueries({ queryKey: ['iam', orgId, 'departments'] });
    },
    onError: (err) => {
      setFeedback({ variant: 'danger', message: formatIamError(err, '调整父部门失败') });
    },
  });

  const assignMembershipMutation = useMutation({
    mutationFn: (values: AssignMembershipValues) =>
      assignDepartmentMembership(orgId, activeDeptId!, {
        user_id: values.user_id,
        is_primary: values.is_primary,
        membership_type: values.membership_type,
        valid_from: values.valid_from ? new Date(values.valid_from).toISOString() : null,
        valid_until: values.valid_until ? new Date(values.valid_until).toISOString() : null,
      }),
    onSuccess: () => {
      setFeedback({ variant: 'success', message: '成员多部门归属关系已生效。' });
      membershipForm.reset();
      setAssignOpen(false);
      void queryClient.invalidateQueries({
        queryKey: ['iam', orgId, 'department-memberships', activeDeptId],
      });
    },
    onError: (err) => {
      setFeedback({ variant: 'danger', message: formatIamError(err, '分配部门成员失败') });
    },
  });

  const removeMembershipMutation = useMutation({
    mutationFn: (membershipId: string) =>
      removeDepartmentMembership(orgId, activeDeptId!, membershipId),
    onSuccess: () => {
      setFeedback({ variant: 'success', message: '已移除该部门兼职关系。' });
      void queryClient.invalidateQueries({
        queryKey: ['iam', orgId, 'department-memberships', activeDeptId],
      });
    },
    onError: (err) => {
      setFeedback({ variant: 'danger', message: formatIamError(err, '移除部门关系失败') });
    },
  });

  const openMoveDialog = () => {
    if (!activeDeptRow) return;
    moveForm.reset({
      name: activeDeptRow.node.name,
      parent_id: activeDeptRow.node.parentId ?? '',
    });
    setMoveOpen(true);
  };

  return (
    <div className="space-y-4">
      {feedback ? <Alert variant={feedback.variant}>{feedback.message}</Alert> : null}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-5">
        {/* 左侧：部门层级树 */}
        <Card className="lg:col-span-2">
          <CardHeader className="flex-row items-center justify-between space-y-0">
            <div>
              <CardTitle className="flex items-center gap-2">
                <FolderTree className="h-4 w-4 text-ink-muted" aria-hidden="true" />
                部门层级树
              </CardTitle>
              <CardDescription>基于 Closure Table 维护，支持多级子部门与防环校验。</CardDescription>
            </div>

            {canManageDepartments ? (
              <Dialog open={createOpen} onOpenChange={setCreateOpen}>
                <DialogTrigger
                  render={
                    <Button size="sm">
                      <Plus className="h-3.5 w-3.5" aria-hidden="true" />
                      新建部门
                    </Button>
                  }
                />
                <DialogContent>
                  <DialogHeader>
                    <DialogTitle>新建部门 / 子部门</DialogTitle>
                    <DialogDescription>
                      选择上级部门以挂载为子节点，留空则创建为组织根部门。
                    </DialogDescription>
                  </DialogHeader>
                  <form
                    className="mt-4 space-y-3"
                    onSubmit={createForm.handleSubmit((vals) => createDeptMutation.mutate(vals))}
                    noValidate
                  >
                    <div className="space-y-1">
                      <label htmlFor="dept-name" className="text-xs font-medium text-ink">
                        部门名称
                      </label>
                      <Input
                        id="dept-name"
                        placeholder="例如：财务审计组"
                        invalid={Boolean(createForm.formState.errors.name)}
                        {...createForm.register('name')}
                      />
                      {createForm.formState.errors.name ? (
                        <p className="text-xs text-danger">
                          {createForm.formState.errors.name.message}
                        </p>
                      ) : null}
                    </div>

                    <div className="space-y-1">
                      <label htmlFor="dept-parent" className="text-xs font-medium text-ink">
                        上级部门
                      </label>
                      <select
                        id="dept-parent"
                        className="flex h-9 w-full rounded-lg border border-line bg-surface-elevated px-3 text-sm text-ink shadow-xs focus-visible:outline-2 focus-visible:outline-ring"
                        {...createForm.register('parent_id')}
                      >
                        <option value="">（无上级 — 作为根部门）</option>
                        {flattenedRows.map((row) => (
                          <option key={row.node.id} value={row.node.id}>
                            {'— '.repeat(row.depth)}
                            {row.node.name}
                          </option>
                        ))}
                      </select>
                    </div>

                    <DialogFooter>
                      <DialogClose render={<Button variant="outline" size="sm">取消</Button>} />
                      <Button type="submit" size="sm" loading={createDeptMutation.isPending}>
                        确认创建
                      </Button>
                    </DialogFooter>
                  </form>
                </DialogContent>
              </Dialog>
            ) : null}
          </CardHeader>

          <CardContent>
            {departmentsQuery.isLoading ? (
              <div className="space-y-2" data-testid="departments-loading">
                <Skeleton shape="line" className="w-2/3" />
                <Skeleton shape="line" className="w-4/5" />
                <Skeleton shape="line" className="w-1/2" />
              </div>
            ) : departmentsQuery.isError ? (
              <Alert variant="danger" title="加载部门树失败">
                {formatIamError(departmentsQuery.error, '无法读取部门层级')}
              </Alert>
            ) : flattenedRows.length === 0 ? (
              <div className="flex flex-col items-center justify-center rounded-lg border border-dashed border-line py-8 text-center">
                <Inbox className="h-5 w-5 text-ink-muted" aria-hidden="true" />
                <p className="mt-2 text-xs font-medium text-ink">当前组织尚未建立部门树</p>
              </div>
            ) : (
              <ul role="tree" aria-label="组织部门树" className="space-y-1">
                {flattenedRows.map((row) => {
                  const isSelected = row.node.id === activeDeptId;
                  return (
                    <li key={row.node.id} role="treeitem" aria-selected={isSelected}>
                      <button
                        type="button"
                        onClick={() => setSelectedDeptId(row.node.id)}
                        style={{ paddingLeft: `${row.depth * 18 + 10}px` }}
                        className={cn(
                          'flex w-full items-center justify-between rounded-lg py-2 pr-3 text-left text-xs transition-colors',
                          isSelected
                            ? 'bg-accent text-ink-inverted font-medium shadow-xs'
                            : 'text-ink-secondary hover:bg-surface-muted hover:text-ink',
                        )}
                      >
                        <span className="flex items-center gap-1.5 truncate">
                          <GitBranch className="h-3.5 w-3.5 shrink-0 opacity-75" aria-hidden="true" />
                          <span className="truncate">{row.node.name}</span>
                        </span>
                        <Badge
                          variant={isSelected ? 'neutral' : 'outline'}
                          className="ml-2 text-[10px]"
                        >
                          L{row.depth}
                        </Badge>
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </CardContent>
        </Card>

        {/* 右侧：选中部门的父级调整与多部门兼职成员关系 */}
        <Card className="lg:col-span-3">
          <CardHeader className="flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <CardTitle>
                {activeDeptRow ? `部门详情：${activeDeptRow.pathLabel}` : '多部门兼职成员'}
              </CardTitle>
              <CardDescription>
                用户可同时隶属多个部门（全职/兼职/临时），所有当前有效成员关系取并集参与 Document ACL 授权。
              </CardDescription>
            </div>

            {canManageDepartments && activeDeptRow ? (
              <div className="flex flex-wrap items-center gap-2">
                <Button variant="outline" size="sm" onClick={openMoveDialog}>
                  <MoveRight className="h-3.5 w-3.5" aria-hidden="true" />
                  调整父部门
                </Button>

                <Dialog open={assignOpen} onOpenChange={setAssignOpen}>
                  <DialogTrigger
                    render={
                      <Button size="sm">
                        <UserPlus className="h-3.5 w-3.5" aria-hidden="true" />
                        分配部门成员
                      </Button>
                    }
                  />
                  <DialogContent>
                    <DialogHeader>
                      <DialogTitle>分配多部门兼职成员</DialogTitle>
                      <DialogDescription>
                        目标部门：{activeDeptRow.pathLabel}。可勾选主部门（is_primary）并配置限时有效期。
                      </DialogDescription>
                    </DialogHeader>

                    <form
                      className="mt-4 space-y-3"
                      onSubmit={membershipForm.handleSubmit((vals) =>
                        assignMembershipMutation.mutate(vals),
                      )}
                      noValidate
                    >
                      <div className="space-y-1">
                        <label htmlFor="membership-user" className="text-xs font-medium text-ink">
                          选择组织成员
                        </label>
                        <select
                          id="membership-user"
                          className="flex h-9 w-full rounded-lg border border-line bg-surface-elevated px-3 text-sm text-ink shadow-xs focus-visible:outline-2 focus-visible:outline-ring"
                          {...membershipForm.register('user_id')}
                        >
                          <option value="">请选择成员...</option>
                          {(membersQuery.data ?? []).map((m) => (
                            <option key={m.userId} value={m.userId}>
                              {m.displayName}（{m.email}）
                            </option>
                          ))}
                        </select>
                        {membershipForm.formState.errors.user_id ? (
                          <p className="text-xs text-danger">
                            {membershipForm.formState.errors.user_id.message}
                          </p>
                        ) : null}
                      </div>

                      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                        <div className="space-y-1">
                          <label htmlFor="membership-type" className="text-xs font-medium text-ink">
                            关系类型
                          </label>
                          <select
                            id="membership-type"
                            className="flex h-9 w-full rounded-lg border border-line bg-surface-elevated px-3 text-sm text-ink shadow-xs"
                            {...membershipForm.register('membership_type')}
                          >
                            <option value="FULL_TIME">主职（FULL_TIME）</option>
                            <option value="PART_TIME">兼职（PART_TIME）</option>
                            <option value="TEMPORARY">临时借调（TEMPORARY）</option>
                          </select>
                        </div>

                        <div className="flex items-end pb-2">
                          <label className="inline-flex cursor-pointer items-center gap-2 text-xs font-medium text-ink">
                            <input
                              type="checkbox"
                              className="h-4 w-4 rounded border-line accent-accent"
                              {...membershipForm.register('is_primary')}
                            />
                            设为主要部门（is_primary）
                          </label>
                        </div>
                      </div>

                      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                        <div className="space-y-1">
                          <label htmlFor="membership-valid-from" className="text-xs font-medium text-ink">
                            生效时间（valid_from，可选）
                          </label>
                          <Input
                            id="membership-valid-from"
                            type="datetime-local"
                            {...membershipForm.register('valid_from')}
                          />
                        </div>
                        <div className="space-y-1">
                          <label htmlFor="membership-valid-until" className="text-xs font-medium text-ink">
                            到期时间（valid_until，可选）
                          </label>
                          <Input
                            id="membership-valid-until"
                            type="datetime-local"
                            invalid={Boolean(membershipForm.formState.errors.valid_until)}
                            {...membershipForm.register('valid_until')}
                          />
                          {membershipForm.formState.errors.valid_until ? (
                            <p className="text-xs text-danger">
                              {membershipForm.formState.errors.valid_until.message}
                            </p>
                          ) : null}
                        </div>
                      </div>

                      <DialogFooter>
                        <DialogClose render={<Button variant="outline" size="sm">取消</Button>} />
                        <Button
                          type="submit"
                          size="sm"
                          loading={assignMembershipMutation.isPending}
                        >
                          保存分配
                        </Button>
                      </DialogFooter>
                    </form>
                  </DialogContent>
                </Dialog>
              </div>
            ) : null}
          </CardHeader>

          <CardContent>
            {!activeDeptRow ? (
              <p className="py-8 text-center text-xs text-ink-muted">
                请先在左侧选择一个部门查看其成员与兼职有效期。
              </p>
            ) : membershipsQuery.isLoading ? (
              <div className="space-y-2">
                <Skeleton shape="block" className="h-10 w-full" />
                <Skeleton shape="block" className="h-10 w-full" />
              </div>
            ) : membershipsQuery.isError ? (
              <Alert variant="danger" title="加载部门成员失败">
                {formatIamError(membershipsQuery.error, '无法加载部门成员关系')}
              </Alert>
            ) : (membershipsQuery.data ?? []).length === 0 ? (
              <div className="flex flex-col items-center justify-center rounded-lg border border-dashed border-line py-8 text-center">
                <Inbox className="h-5 w-5 text-ink-muted" aria-hidden="true" />
                <p className="mt-2 text-xs font-medium text-ink">该部门下暂无成员归属记录</p>
              </div>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>成员</TableHead>
                    <TableHead>类型 / 主部门</TableHead>
                    <TableHead>有效期</TableHead>
                    <TableHead>状态</TableHead>
                    {canManageDepartments ? <TableHead className="text-right">操作</TableHead> : null}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {(membershipsQuery.data ?? []).map((item) => (
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
                        <div className="flex flex-wrap items-center gap-1">
                          <Badge variant="outline">{item.membershipType}</Badge>
                          {item.isPrimary ? <Badge variant="info">主部门</Badge> : null}
                        </div>
                      </TableCell>
                      <TableCell className="text-xs text-ink-secondary">
                        {item.validUntil ? `至 ${item.validUntil.slice(0, 16).replace('T', ' ')}` : '长期有效'}
                      </TableCell>
                      <TableCell>
                        <Badge variant={item.status === 'ACTIVE' ? 'success' : 'warning'}>
                          {item.status}
                        </Badge>
                      </TableCell>
                      {canManageDepartments ? (
                        <TableCell className="text-right">
                          <Button
                            variant="ghost"
                            size="sm"
                            aria-label="移除部门成员关系"
                            onClick={() => removeMembershipMutation.mutate(item.id)}
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
          </CardContent>
        </Card>
      </div>

      {/* 调整父部门弹窗 */}
      <Dialog open={moveOpen} onOpenChange={setMoveOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>编辑部门与调整上级节点</DialogTitle>
            <DialogDescription>
              后端事务性重建 Closure Table 并执行防环校验，严禁将部门移动至自身或后代节点下。
            </DialogDescription>
          </DialogHeader>
          <form
            className="mt-4 space-y-3"
            onSubmit={moveForm.handleSubmit((vals) => moveDeptMutation.mutate(vals))}
            noValidate
          >
            <div className="space-y-1">
              <label htmlFor="move-dept-name" className="text-xs font-medium text-ink">
                部门名称
              </label>
              <Input
                id="move-dept-name"
                invalid={Boolean(moveForm.formState.errors.name)}
                {...moveForm.register('name')}
              />
              {moveForm.formState.errors.name ? (
                <p className="text-xs text-danger">{moveForm.formState.errors.name.message}</p>
              ) : null}
            </div>
            <div className="space-y-1">
              <label htmlFor="move-dept-parent" className="text-xs font-medium text-ink">
                目标上级部门
              </label>
              <select
                id="move-dept-parent"
                className="flex h-9 w-full rounded-lg border border-line bg-surface-elevated px-3 text-sm text-ink shadow-xs"
                {...moveForm.register('parent_id')}
              >
                <option value="">（设为根部门）</option>
                {flattenedRows
                  .filter((r) => r.node.id !== activeDeptId)
                  .map((r) => (
                    <option key={r.node.id} value={r.node.id}>
                      {'— '.repeat(r.depth)}
                      {r.node.name}
                    </option>
                  ))}
              </select>
            </div>
            <DialogFooter>
              <DialogClose render={<Button variant="outline" size="sm">取消</Button>} />
              <Button type="submit" size="sm" loading={moveDeptMutation.isPending}>
                保存调整
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  );
}
