import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Building2,
  Globe2,
  Plus,
  RefreshCw,
  Save,
  ShieldAlert,
  ShieldCheck,
  Trash2,
  UserCheck,
  Users,
} from 'lucide-react';
import { useSearchParams } from 'react-router-dom';
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
} from '@/components/ui';
import { AclRevisionTimeline } from '@/features/acl/AclRevisionTimeline';
import {
  fetchDocumentAcl,
  fetchDocumentAclRevisions,
  formatAclError,
  isAclRevisionConflictError,
  submitDocumentAclRevision,
  type DepartmentInheritanceMode,
} from '@/features/acl/api';
import { fetchDocuments } from '@/features/documents/api';
import {
  buildFlattenedDepartmentTree,
  fetchDepartments,
  fetchOrgMembers,
  fetchOrgRoles,
} from '@/features/iam/api';
import { useAuth } from '@/lib/auth-store';

/** 本地编辑态的部门授权条目 */
interface DeptGrantDraft {
  departmentId: string;
  includeDescendants: boolean;
  inheritanceMode: DepartmentInheritanceMode;
}

/** 本地编辑态的个人限时/永久授权条目 */
interface UserGrantDraft {
  userId: string;
  validUntil: string;
}

/**
 * PRD 模块 6（Slice 2 真实对接）：文档四维权限 ACL 配置与修订历史。
 * 严格遵循 ADR-001 裁决：
 * - 四维 Allow OR 模型（GLOBAL / DEPARTMENT / ROLE / USER），默认拒绝、无 Deny Override；
 * - 唯一写入口 POST /api/v1/documents/{document_id}/acl/revisions，强制携带 expected_revision、reason 与 Idempotency-Key；
 * - 遇到 409 乐观锁冲突时提供清晰的刷新恢复提示。
 */
export function AclPage() {
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const { currentOrgId, hasPermission } = useAuth();

  const initialDocParam = searchParams.get('documentId') ?? '';
  const [selectedDocId, setSelectedDocId] = useState<string>(initialDocParam);

  // 四维 Allow OR 本地草稿状态
  const [globalEnabled, setGlobalEnabled] = useState(false);
  const [deptGrants, setDeptGrants] = useState<DeptGrantDraft[]>([]);
  const [selectedRoleIds, setSelectedRoleIds] = useState<string[]>([]);
  const [userGrants, setUserGrants] = useState<UserGrantDraft[]>([]);

  // 新增部门/个人授权草稿控件状态
  const [pendingDeptId, setPendingDeptId] = useState('');
  const [pendingIncludeDesc, setPendingIncludeDesc] = useState(true);
  const [pendingInheritMode, setPendingInheritMode] =
    useState<DepartmentInheritanceMode>('DYNAMIC');
  const [pendingUserId, setPendingUserId] = useState('');
  const [pendingUserUntil, setPendingUserUntil] = useState('');

  // 必填审计理由与提交反馈
  const [reason, setReason] = useState('');
  const [reasonError, setReasonError] = useState<string | null>(null);
  const [conflictNotice, setConflictNotice] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<{ variant: 'success' | 'danger'; message: string } | null>(
    null,
  );

  // 前端仅用于体验层按钮显隐；后端对 ACL 写入口始终强制校验
  const canEditAcl =
    hasPermission('acl:write') ||
    hasPermission('acl:manage') ||
    hasPermission('document:acl:write') ||
    hasPermission('org:manage');

  const documentsQuery = useQuery({
    queryKey: ['documents', currentOrgId, 'acl-selector'],
    queryFn: () => fetchDocuments(currentOrgId!, { limit: 100 }),
    enabled: Boolean(currentOrgId),
  });

  const departmentsQuery = useQuery({
    queryKey: ['iam', currentOrgId, 'departments'],
    queryFn: () => fetchDepartments(currentOrgId!),
    enabled: Boolean(currentOrgId),
  });

  const rolesQuery = useQuery({
    queryKey: ['iam', currentOrgId, 'roles'],
    queryFn: () => fetchOrgRoles(currentOrgId!),
    enabled: Boolean(currentOrgId),
  });

  const membersQuery = useQuery({
    queryKey: ['iam', currentOrgId, 'members', 'ALL'],
    queryFn: () => fetchOrgMembers(currentOrgId!),
    enabled: Boolean(currentOrgId),
  });

  const documents = documentsQuery.data?.items ?? [];
  const activeDocId = selectedDocId || documents[0]?.id || '';
  const activeDoc = documents.find((d) => d.id === activeDocId) ?? null;

  const aclQuery = useQuery({
    queryKey: ['acl', currentOrgId, activeDocId],
    queryFn: () => fetchDocumentAcl(currentOrgId!, activeDocId),
    enabled: Boolean(currentOrgId && activeDocId),
  });

  const revisionsQuery = useQuery({
    queryKey: ['acl', currentOrgId, activeDocId, 'revisions'],
    queryFn: () => fetchDocumentAclRevisions(currentOrgId!, activeDocId),
    enabled: Boolean(currentOrgId && activeDocId),
  });

  // 当后端返回最新 ACL 时同步到本地四维草稿编辑器
  useEffect(() => {
    const acl = aclQuery.data;
    if (!acl) return;

    setGlobalEnabled(acl.grants.some((g) => g.subjectType === 'GLOBAL'));
    setDeptGrants(
      acl.grants
        .filter((g) => g.subjectType === 'DEPARTMENT' && g.subjectId)
        .map((g) => ({
          departmentId: g.subjectId!,
          includeDescendants: g.includeDescendants,
          inheritanceMode: g.inheritanceMode,
        })),
    );
    setSelectedRoleIds(
      acl.grants
        .filter((g) => g.subjectType === 'ROLE' && g.subjectId)
        .map((g) => g.subjectId!),
    );
    setUserGrants(
      acl.grants
        .filter((g) => g.subjectType === 'USER' && g.subjectId)
        .map((g) => ({
          userId: g.subjectId!,
          validUntil: g.validUntil ? g.validUntil.slice(0, 16) : '',
        })),
    );
  }, [aclQuery.data]);

  const flattenedDepts = useMemo(
    () => buildFlattenedDepartmentTree(departmentsQuery.data ?? []),
    [departmentsQuery.data],
  );
  const deptNameMap = useMemo(
    () => new Map(flattenedDepts.map((r) => [r.node.id, r.pathLabel])),
    [flattenedDepts],
  );
  const memberMap = useMemo(
    () => new Map((membersQuery.data ?? []).map((m) => [m.userId, `${m.displayName} (${m.email})`])),
    [membersQuery.data],
  );

  // 严格只允许选择当前组织的 OrganizationRole，显式屏蔽 SystemRole
  const orgRolesOnly = rolesQuery.data?.organizationRoles ?? [];

  const submitMutation = useMutation({
    mutationFn: async () => {
      if (!currentOrgId || !activeDocId) {
        throw new Error('请先选择目标文档');
      }
      const expectedRevision = aclQuery.data?.revision ?? 0;

      const grants: Parameters<typeof submitDocumentAclRevision>[2]['grants'] = [];
      if (globalEnabled) {
        grants.push({ subject_type: 'GLOBAL', subject_id: null });
      }
      for (const dg of deptGrants) {
        grants.push({
          subject_type: 'DEPARTMENT',
          subject_id: dg.departmentId,
          include_descendants: dg.includeDescendants,
          inheritance_mode: dg.inheritanceMode,
        });
      }
      for (const roleId of selectedRoleIds) {
        grants.push({ subject_type: 'ROLE', subject_id: roleId });
      }
      for (const ug of userGrants) {
        grants.push({
          subject_type: 'USER',
          subject_id: ug.userId,
          valid_until: ug.validUntil ? new Date(ug.validUntil).toISOString() : null,
        });
      }

      return submitDocumentAclRevision(currentOrgId, activeDocId, {
        expected_revision: expectedRevision,
        reason: reason.trim(),
        grants,
      });
    },
    onSuccess: (updated) => {
      setConflictNotice(null);
      setReason('');
      setReasonError(null);
      setFeedback({
        variant: 'success',
        message: `ACL 已提交生效，当前修订号更新为 Revision #${updated.revision}，已触发 Outbox 异步投影同步。`,
      });
      void queryClient.invalidateQueries({ queryKey: ['acl', currentOrgId, activeDocId] });
      void queryClient.invalidateQueries({
        queryKey: ['acl', currentOrgId, activeDocId, 'revisions'],
      });
    },
    onError: (err) => {
      if (isAclRevisionConflictError(err)) {
        setConflictNotice(formatAclError(err));
        setFeedback(null);
      } else {
        setConflictNotice(null);
        setFeedback({ variant: 'danger', message: formatAclError(err, '提交 ACL 变更失败') });
      }
    },
  });

  const handleDocumentChange = (nextDocId: string) => {
    setSelectedDocId(nextDocId);
    setConflictNotice(null);
    setFeedback(null);
    if (nextDocId) {
      setSearchParams({ documentId: nextDocId });
    }
  };

  const handleAddDeptGrant = () => {
    if (!pendingDeptId) return;
    setDeptGrants((prev) => {
      const exists = prev.some((d) => d.departmentId === pendingDeptId);
      if (exists) {
        return prev.map((d) =>
          d.departmentId === pendingDeptId
            ? {
                departmentId: pendingDeptId,
                includeDescendants: pendingIncludeDesc,
                inheritanceMode: pendingInheritMode,
              }
            : d,
        );
      }
      return [
        ...prev,
        {
          departmentId: pendingDeptId,
          includeDescendants: pendingIncludeDesc,
          inheritanceMode: pendingInheritMode,
        },
      ];
    });
    setPendingDeptId('');
  };

  const handleAddUserGrant = () => {
    if (!pendingUserId) return;
    setUserGrants((prev) => {
      const exists = prev.some((u) => u.userId === pendingUserId);
      if (exists) {
        return prev.map((u) =>
          u.userId === pendingUserId ? { userId: pendingUserId, validUntil: pendingUserUntil } : u,
        );
      }
      return [...prev, { userId: pendingUserId, validUntil: pendingUserUntil }];
    });
    setPendingUserId('');
    setPendingUserUntil('');
  };

  const handleSubmitAcl = () => {
    setFeedback(null);
    const trimmedReason = reason.trim();
    if (trimmedReason.length < 2) {
      setReasonError('变更理由（reason）为必填审计字段，请至少输入 2 个字符说明授权变更原因。');
      return;
    }
    setReasonError(null);
    submitMutation.mutate();
  };

  const handleRefreshLatestAcl = async () => {
    setConflictNotice(null);
    await Promise.all([aclQuery.refetch(), revisionsQuery.refetch()]);
  };

  return (
    <div className="space-y-5">
      {/* 页头 */}
      <div className="flex flex-col gap-3 border-b border-line pb-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="space-y-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="outline" className="font-mono text-[10px]">
              PRD-06
            </Badge>
            <h1 className="text-xl font-semibold tracking-tight text-ink">
              文档四维权限 ACL 配置
            </h1>
            <Badge variant="neutral">Allow OR 模型 · 无 Deny Override</Badge>
            <Badge variant="warning">默认拒绝</Badge>
          </div>
          <p className="text-sm text-ink-secondary">
            统一通过 <span className="font-mono">POST /api/v1/documents/&#123;id&#125;/acl/revisions</span>{' '}
            提交乐观锁修订；PostgreSQL 最终鉴权在 Reranker/LLM 前强制执行。
          </p>
        </div>

        <Button
          variant="outline"
          size="sm"
          onClick={() => void handleRefreshLatestAcl()}
          disabled={aclQuery.isFetching || !activeDocId}
        >
          <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
          刷新最新 Revision
        </Button>
      </div>

      {!currentOrgId ? (
        <Alert variant="warning" title="尚未绑定当前 Organization">
          请先在顶部切换器绑定组织后再配置文档 ACL。
        </Alert>
      ) : (
        <div className="space-y-5">
          {/* 文档选择器与当前 Revision 显示 */}
          <Card>
            <CardContent className="flex flex-col gap-3 py-4 md:flex-row md:items-center md:justify-between">
              <div className="flex-1 space-y-1">
                <label htmlFor="acl-doc-select" className="text-xs font-medium text-ink">
                  选择要配置 ACL 的文档
                </label>
                <select
                  id="acl-doc-select"
                  value={activeDocId}
                  onChange={(event) => handleDocumentChange(event.target.value)}
                  className="flex h-9 w-full max-w-xl rounded-lg border border-line bg-surface-elevated px-3 text-sm text-ink shadow-xs"
                >
                  {documents.length === 0 ? (
                    <option value="">当前组织暂无文档</option>
                  ) : (
                    documents.map((doc) => (
                      <option key={doc.id} value={doc.id}>
                        {doc.title}（{doc.status} · {doc.sensitivityLevel}）
                      </option>
                    ))
                  )}
                </select>
              </div>

              {activeDoc ? (
                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant="info" className="font-mono" data-testid="acl-current-revision">
                    当前 Revision #{aclQuery.data?.revision ?? 0}
                  </Badge>
                  <Badge variant={activeDoc.status === 'ACTIVE' ? 'success' : 'warning'}>
                    {activeDoc.status}
                  </Badge>
                  <Badge variant="outline">{activeDoc.sensitivityLevel}</Badge>
                </div>
              ) : null}
            </CardContent>
          </Card>

          {/* 409 乐观锁冲突专属警示与一键刷新 */}
          {conflictNotice ? (
            <Alert
              variant="danger"
              title="乐观锁冲突（expected_revision 已过期）"
              icon={<ShieldAlert className="h-4 w-4" />}
            >
              <p>{conflictNotice}</p>
              <div className="mt-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => void handleRefreshLatestAcl()}
                >
                  <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
                  立即刷新最新 ACL Revision
                </Button>
              </div>
            </Alert>
          ) : null}

          {feedback ? <Alert variant={feedback.variant}>{feedback.message}</Alert> : null}

          {!activeDocId ? null : aclQuery.isLoading ? (
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              <Skeleton shape="block" className="h-44" />
              <Skeleton shape="block" className="h-44" />
            </div>
          ) : aclQuery.isError ? (
            <Alert variant="danger" title="加载文档 ACL 失败">
              {formatAclError(aclQuery.error, '无法读取当前 ACL 配置')}
            </Alert>
          ) : (
            <div className="space-y-4">
              {/* 四维 Allow OR 编辑器 */}
              <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                {/* 维度 1：GLOBAL */}
                <Card>
                  <CardHeader className="py-3.5">
                    <CardTitle className="flex items-center justify-between">
                      <span className="inline-flex items-center gap-2">
                        <Globe2 className="h-4 w-4 text-info" aria-hidden="true" />
                        维度 1：GLOBAL（空间内全局可读）
                      </span>
                      <Badge variant="outline" className="font-mono text-[10px]">
                        GLOBAL
                      </Badge>
                    </CardTitle>
                  </CardHeader>
                  <CardContent className="space-y-3">
                    <label className="flex cursor-pointer items-center gap-2.5 text-sm font-medium text-ink">
                      <input
                        type="checkbox"
                        checked={globalEnabled}
                        disabled={!canEditAcl}
                        onChange={(event) => setGlobalEnabled(event.target.checked)}
                        className="h-4 w-4 rounded border-line accent-accent"
                      />
                      开启当前 Knowledge Space 内全局可读
                    </label>
                    <Alert variant="info">
                      仅对当前 Knowledge Space 内满足入口条件的用户可读，<strong>非跨组织公开</strong>。
                    </Alert>
                  </CardContent>
                </Card>

                {/* 维度 2：DEPARTMENT */}
                <Card>
                  <CardHeader className="py-3.5">
                    <CardTitle className="flex items-center justify-between">
                      <span className="inline-flex items-center gap-2">
                        <Building2 className="h-4 w-4 text-ink-muted" aria-hidden="true" />
                        维度 2：DEPARTMENT（部门与继承模式）
                      </span>
                      <Badge variant="outline" className="font-mono text-[10px]">
                        DEPARTMENT
                      </Badge>
                    </CardTitle>
                  </CardHeader>
                  <CardContent className="space-y-3">
                    {canEditAcl ? (
                      <div className="space-y-2 rounded-lg border border-line bg-surface-subtle p-3">
                        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                          <select
                            aria-label="选择授权部门"
                            value={pendingDeptId}
                            onChange={(event) => setPendingDeptId(event.target.value)}
                            className="flex h-8 w-full rounded-md border border-line bg-surface-elevated px-2.5 text-xs text-ink"
                          >
                            <option value="">选择部门...</option>
                            {flattenedDepts.map((row) => (
                              <option key={row.node.id} value={row.node.id}>
                                {'— '.repeat(row.depth)}
                                {row.node.name}
                              </option>
                            ))}
                          </select>

                          <select
                            aria-label="选择部门继承模式"
                            value={pendingInheritMode}
                            onChange={(event) =>
                              setPendingInheritMode(event.target.value as DepartmentInheritanceMode)
                            }
                            className="flex h-8 w-full rounded-md border border-line bg-surface-elevated px-2.5 text-xs text-ink"
                          >
                            <option value="DYNAMIC">DYNAMIC（动态追踪部门树）</option>
                            <option value="SNAPSHOT">SNAPSHOT（固化授权时刻后代快照）</option>
                          </select>
                        </div>

                        <div className="flex items-center justify-between gap-2">
                          <label className="inline-flex cursor-pointer items-center gap-1.5 text-xs text-ink">
                            <input
                              type="checkbox"
                              checked={pendingIncludeDesc}
                              onChange={(event) => setPendingIncludeDesc(event.target.checked)}
                              className="h-3.5 w-3.5 rounded border-line accent-accent"
                            />
                            包含后代子部门（include_descendants）
                          </label>
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={handleAddDeptGrant}
                            disabled={!pendingDeptId}
                          >
                            <Plus className="h-3.5 w-3.5" aria-hidden="true" />
                            添加部门
                          </Button>
                        </div>
                      </div>
                    ) : null}

                    {deptGrants.length === 0 ? (
                      <p className="text-xs text-ink-muted">暂未配置部门维度授权。</p>
                    ) : (
                      <ul className="space-y-1.5">
                        {deptGrants.map((dg) => (
                          <li
                            key={dg.departmentId}
                            className="flex items-center justify-between rounded-md border border-line px-2.5 py-1.5 text-xs"
                          >
                            <div className="flex flex-wrap items-center gap-1.5">
                              <span className="font-medium text-ink">
                                {deptNameMap.get(dg.departmentId) ?? dg.departmentId}
                              </span>
                              <Badge variant="neutral" className="text-[10px]">
                                {dg.includeDescendants ? '含后代' : '仅本部门'}
                              </Badge>
                              <Badge variant="outline" className="font-mono text-[10px]">
                                {dg.inheritanceMode}
                              </Badge>
                            </div>
                            {canEditAcl ? (
                              <button
                                type="button"
                                aria-label="移除部门授权"
                                className="text-ink-muted hover:text-danger"
                                onClick={() =>
                                  setDeptGrants((prev) =>
                                    prev.filter((item) => item.departmentId !== dg.departmentId),
                                  )
                                }
                              >
                                <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                              </button>
                            ) : null}
                          </li>
                        ))}
                      </ul>
                    )}
                  </CardContent>
                </Card>

                {/* 维度 3：ROLE（仅 OrganizationRole，显式屏蔽 SystemRole） */}
                <Card>
                  <CardHeader className="py-3.5">
                    <CardTitle className="flex items-center justify-between">
                      <span className="inline-flex items-center gap-2">
                        <Users className="h-4 w-4 text-ink-muted" aria-hidden="true" />
                        维度 3：ROLE（仅 OrganizationRole）
                      </span>
                      <Badge variant="outline" className="font-mono text-[10px]">
                        ROLE
                      </Badge>
                    </CardTitle>
                    <CardDescription>
                      仅允许选择当前组织的 OrganizationRole；平台级 SystemRole 已被显式屏蔽，不可作为文档读取主体。
                    </CardDescription>
                  </CardHeader>
                  <CardContent>
                    {orgRolesOnly.length === 0 ? (
                      <p className="text-xs text-ink-muted">当前组织暂无可勾选的 OrganizationRole。</p>
                    ) : (
                      <div className="flex flex-wrap gap-2">
                        {orgRolesOnly.map((role) => {
                          const checked = selectedRoleIds.includes(role.id);
                          return (
                            <label
                              key={role.id}
                              className={`inline-flex cursor-pointer items-center gap-2 rounded-lg border px-3 py-1.5 text-xs transition-colors ${
                                checked
                                  ? 'border-accent bg-surface-muted font-medium text-ink'
                                  : 'border-line bg-surface-elevated text-ink-secondary hover:border-line-strong'
                              }`}
                            >
                              <input
                                type="checkbox"
                                checked={checked}
                                disabled={!canEditAcl}
                                onChange={() =>
                                  setSelectedRoleIds((prev) =>
                                    prev.includes(role.id)
                                      ? prev.filter((id) => id !== role.id)
                                      : [...prev, role.id],
                                  )
                                }
                                className="h-3.5 w-3.5 rounded border-line accent-accent"
                              />
                              <span>{role.name}</span>
                              <span className="font-mono text-[10px] text-ink-muted">
                                ({role.code})
                              </span>
                            </label>
                          );
                        })}
                      </div>
                    )}
                  </CardContent>
                </Card>

                {/* 维度 4：USER（个人永久或限时授权） */}
                <Card>
                  <CardHeader className="py-3.5">
                    <CardTitle className="flex items-center justify-between">
                      <span className="inline-flex items-center gap-2">
                        <UserCheck className="h-4 w-4 text-ink-muted" aria-hidden="true" />
                        维度 4：USER（个人永久 / 限时授权）
                      </span>
                      <Badge variant="outline" className="font-mono text-[10px]">
                        USER
                      </Badge>
                    </CardTitle>
                  </CardHeader>
                  <CardContent className="space-y-3">
                    {canEditAcl ? (
                      <div className="grid grid-cols-1 gap-2 rounded-lg border border-line bg-surface-subtle p-3 sm:grid-cols-3">
                        <select
                          aria-label="选择个人授权用户"
                          value={pendingUserId}
                          onChange={(event) => setPendingUserId(event.target.value)}
                          className="flex h-8 w-full rounded-md border border-line bg-surface-elevated px-2.5 text-xs text-ink"
                        >
                          <option value="">选择成员...</option>
                          {(membersQuery.data ?? []).map((m) => (
                            <option key={m.userId} value={m.userId}>
                              {m.displayName}（{m.email}）
                            </option>
                          ))}
                        </select>
                        <Input
                          aria-label="个人授权到期时间"
                          type="datetime-local"
                          value={pendingUserUntil}
                          onChange={(event) => setPendingUserUntil(event.target.value)}
                          className="h-8 text-xs"
                        />
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={handleAddUserGrant}
                          disabled={!pendingUserId}
                        >
                          <Plus className="h-3.5 w-3.5" aria-hidden="true" />
                          添加个人授权
                        </Button>
                      </div>
                    ) : null}

                    {userGrants.length === 0 ? (
                      <p className="text-xs text-ink-muted">暂未配置个人维度授权。</p>
                    ) : (
                      <ul className="space-y-1.5">
                        {userGrants.map((ug) => (
                          <li
                            key={ug.userId}
                            className="flex items-center justify-between rounded-md border border-line px-2.5 py-1.5 text-xs"
                          >
                            <div className="flex flex-wrap items-center gap-1.5">
                              <span className="font-medium text-ink">
                                {memberMap.get(ug.userId) ?? ug.userId}
                              </span>
                              <Badge variant={ug.validUntil ? 'warning' : 'success'} className="text-[10px]">
                                {ug.validUntil ? `限时至 ${ug.validUntil.replace('T', ' ')}` : '永久有效'}
                              </Badge>
                            </div>
                            {canEditAcl ? (
                              <button
                                type="button"
                                aria-label="移除个人授权"
                                className="text-ink-muted hover:text-danger"
                                onClick={() =>
                                  setUserGrants((prev) =>
                                    prev.filter((item) => item.userId !== ug.userId),
                                  )
                                }
                              >
                                <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                              </button>
                            ) : null}
                          </li>
                        ))}
                      </ul>
                    )}
                  </CardContent>
                </Card>
              </div>

              {/* 变更理由与提交栏 */}
              {canEditAcl ? (
                <Card>
                  <CardContent className="flex flex-col gap-3 py-4 sm:flex-row sm:items-end">
                    <div className="flex-1 space-y-1">
                      <label htmlFor="acl-change-reason" className="text-xs font-medium text-ink">
                        变更理由（reason，必填审计字段）
                      </label>
                      <Input
                        id="acl-change-reason"
                        value={reason}
                        invalid={Boolean(reasonError)}
                        onChange={(event) => {
                          setReason(event.target.value);
                          if (reasonError) setReasonError(null);
                        }}
                        placeholder="例如：按财务部审批单，向 HR 角色与研发部开放差旅制度文档"
                      />
                      {reasonError ? <p className="text-xs text-danger">{reasonError}</p> : null}
                    </div>
                    <Button
                      size="md"
                      loading={submitMutation.isPending}
                      onClick={handleSubmitAcl}
                    >
                      <Save className="h-4 w-4" aria-hidden="true" />
                      提交 ACL Revision（expected #{aclQuery.data?.revision ?? 0}）
                    </Button>
                  </CardContent>
                </Card>
              ) : (
                <Alert variant="info" icon={<ShieldCheck className="h-4 w-4" />}>
                  当前账号仅具备只读视图权限；如需修改文档 ACL，请联系具备 ACL 管理权限的知识管理员。
                </Alert>
              )}

              {/* ACLRevision 审计历史与 Diff */}
              <AclRevisionTimeline
                revisions={revisionsQuery.data ?? []}
                isLoading={revisionsQuery.isLoading}
                isError={revisionsQuery.isError}
                error={revisionsQuery.error}
              />
            </div>
          )}
        </div>
      )}
    </div>
  );
}
