import { ApiError, apiClient } from '@/lib/api-client';
import type {
  DepartmentMembershipItem,
  DepartmentNode,
  OrganizationMemberItem,
  PermissionItem,
  RoleItem,
} from '@/types/domain';

/** 后端列表响应兼容包裹结构 */
type ListEnvelope<T> = T[] | { items?: T[]; data?: T[]; total?: number };

/** 解包后端分页对象或数组响应 */
function unwrapList<T>(payload: ListEnvelope<T>): T[] {
  if (Array.isArray(payload)) return payload;
  if (Array.isArray(payload.items)) return payload.items;
  if (Array.isArray(payload.data)) return payload.data;
  return [];
}

/** 原始成员记录结构 */
interface RawMemberRecord {
  id: string;
  organization_id?: string;
  organizationId?: string;
  user_id?: string;
  userId?: string;
  email?: string;
  display_name?: string;
  displayName?: string;
  user?: {
    id?: string;
    email?: string;
    display_name?: string;
    displayName?: string;
  };
  status?: string;
  joined_at?: string | null;
  joinedAt?: string | null;
  organization_roles?: Array<string | { code?: string; name?: string }>;
  organizationRoles?: string[];
  roles?: Array<string | { code?: string; name?: string }>;
  system_roles?: Array<string | { code?: string; name?: string }>;
  systemRoles?: string[];
  departments?: Array<string | { name?: string }>;
}

/** 原始部门记录结构 */
interface RawDepartmentRecord {
  id: string;
  organization_id?: string;
  organizationId?: string;
  parent_id?: string | null;
  parentId?: string | null;
  name: string;
  status?: string;
  depth?: number;
  member_count?: number;
  memberCount?: number;
  children?: RawDepartmentRecord[];
}

/** 原始部门成员关系记录结构 */
interface RawDepartmentMembershipRecord {
  id: string;
  organization_id?: string;
  organizationId?: string;
  department_id?: string;
  departmentId?: string;
  user_id?: string;
  userId?: string;
  user_email?: string;
  userEmail?: string;
  user_display_name?: string;
  userDisplayName?: string;
  user?: { email?: string; display_name?: string; displayName?: string };
  membership_type?: string;
  membershipType?: string;
  is_primary?: boolean;
  isPrimary?: boolean;
  status?: string;
  valid_from?: string | null;
  validFrom?: string | null;
  valid_until?: string | null;
  validUntil?: string | null;
}

/** 原始权限项结构 */
interface RawPermissionRecord {
  id?: string;
  code: string;
  name?: string;
  description?: string;
  category?: string;
  module?: string;
  scope?: string;
}

/** 原始角色记录结构 */
interface RawRoleRecord {
  id: string;
  organization_id?: string | null;
  organizationId?: string | null;
  code: string;
  name: string;
  description?: string;
  status?: string;
  role_scope?: string;
  roleScope?: string;
  scope?: string;
  is_system?: boolean;
  permissions?: Array<string | { code?: string }>;
  permission_codes?: string[];
  member_count?: number;
  memberCount?: number;
}

/** 格式化 IAM 请求错误信息（针对 403 无权限场景提供明确中文反馈） */
export function formatIamError(error: unknown, fallbackMessage = '操作失败，请稍后重试'): string {
  if (error instanceof ApiError) {
    if (error.status === 403) {
      return `无权限执行此操作（HTTP 403）：${error.message || '当前账号缺少所需组织权限，后端已拦截该请求。'}`;
    }
    if (error.status === 409) {
      return `数据冲突（HTTP 409）：${error.message || '存在重复记录或层级环路约束。'}`;
    }
    return `${error.message || fallbackMessage}（${error.code}）`;
  }
  if (error instanceof Error && error.message) {
    return error.message;
  }
  return fallbackMessage;
}

/** 标准化组织成员数据 */
function normalizeMember(raw: RawMemberRecord, fallbackOrgId: string): OrganizationMemberItem {
  const email = raw.email ?? raw.user?.email ?? '';
  const displayName =
    raw.displayName ??
    raw.display_name ??
    raw.user?.displayName ??
    raw.user?.display_name ??
    (email ? email.split('@')[0] ?? email : raw.user_id ?? raw.id);

  const normalizeStrings = (arr?: Array<string | { code?: string; name?: string }>): string[] =>
    (arr ?? [])
      .map((item) => (typeof item === 'string' ? item : (item.code ?? item.name ?? '')))
      .filter(Boolean);

  const rawStatus = (raw.status ?? 'ACTIVE').toUpperCase();
  const status: OrganizationMemberItem['status'] =
    rawStatus === 'DISABLED' || rawStatus === 'INVITED' || rawStatus === 'REMOVED'
      ? rawStatus
      : 'ACTIVE';

  return {
    id: raw.id,
    organizationId: raw.organizationId ?? raw.organization_id ?? fallbackOrgId,
    userId: raw.userId ?? raw.user_id ?? raw.user?.id ?? raw.id,
    email,
    displayName,
    status,
    joinedAt: raw.joinedAt ?? raw.joined_at ?? null,
    organizationRoles:
      raw.organizationRoles ?? normalizeStrings(raw.organization_roles ?? raw.roles),
    systemRoles: raw.systemRoles ?? normalizeStrings(raw.system_roles),
    departments: (raw.departments ?? [])
      .map((d) => (typeof d === 'string' ? d : (d.name ?? '')))
      .filter(Boolean),
  };
}

/** 标准化部门节点 */
function normalizeDepartment(raw: RawDepartmentRecord, fallbackOrgId: string): DepartmentNode {
  const rawStatus = (raw.status ?? 'ACTIVE').toUpperCase();
  const status: DepartmentNode['status'] =
    rawStatus === 'DISABLED' || rawStatus === 'ARCHIVED' ? rawStatus : 'ACTIVE';

  return {
    id: raw.id,
    organizationId: raw.organizationId ?? raw.organization_id ?? fallbackOrgId,
    parentId: raw.parentId !== undefined ? raw.parentId : (raw.parent_id ?? null),
    name: raw.name,
    status,
    depth: raw.depth,
    memberCount: raw.memberCount ?? raw.member_count,
    children: raw.children?.map((child) => normalizeDepartment(child, fallbackOrgId)),
  };
}

/** 扁平化带层级的部门树条目，供缩进树视图渲染 */
export interface FlattenedDepartmentRow {
  node: DepartmentNode;
  depth: number;
  pathLabel: string;
}

/**
 * 将后端返回的扁平邻接列表或预嵌套树统一组装为带缩进深度的有序列表（内置环路保护）。
 */
export function buildFlattenedDepartmentTree(nodes: DepartmentNode[]): FlattenedDepartmentRow[] {
  const byId = new Map<string, DepartmentNode>();
  const childrenMap = new Map<string | null, DepartmentNode[]>();

  const collect = (items: DepartmentNode[]) => {
    for (const item of items) {
      if (!byId.has(item.id)) {
        byId.set(item.id, item);
      }
      if (item.children && item.children.length > 0) {
        collect(item.children);
      }
    }
  };
  collect(nodes);

  for (const node of byId.values()) {
    const parentKey = node.parentId && byId.has(node.parentId) ? node.parentId : null;
    const bucket = childrenMap.get(parentKey) ?? [];
    bucket.push(node);
    childrenMap.set(parentKey, bucket);
  }

  const rows: FlattenedDepartmentRow[] = [];
  const visited = new Set<string>();

  const walk = (parentId: string | null, depth: number, ancestorNames: string[]) => {
    const children = (childrenMap.get(parentId) ?? []).sort((a, b) =>
      a.name.localeCompare(b.name, 'zh-CN'),
    );
    for (const child of children) {
      if (visited.has(child.id)) continue;
      visited.add(child.id);
      const nextAncestors = [...ancestorNames, child.name];
      rows.push({
        node: child,
        depth,
        pathLabel: nextAncestors.join(' / '),
      });
      walk(child.id, depth + 1, nextAncestors);
    }
  };

  walk(null, 0, []);
  return rows;
}

/** 查询当前组织的成员清单（GET /api/v1/organizations/{org_id}/members） */
export async function fetchOrgMembers(
  orgId: string,
  params?: { search?: string; status?: string },
): Promise<OrganizationMemberItem[]> {
  const query = new URLSearchParams();
  if (params?.search?.trim()) query.set('search', params.search.trim());
  if (params?.status && params.status !== 'ALL') query.set('status', params.status);
  const qs = query.toString();
  const url = `/api/v1/organizations/${encodeURIComponent(orgId)}/members${qs ? `?${qs}` : ''}`;

  const payload = await apiClient.get<ListEnvelope<RawMemberRecord>>(url, {
    organizationId: orgId,
  });
  return unwrapList(payload).map((item) => normalizeMember(item, orgId));
}

/** 邀请或添加组织成员（POST /api/v1/organizations/{org_id}/members） */
export async function inviteOrgMember(
  orgId: string,
  input: { email: string; display_name: string; password?: string; role_ids?: string[] },
): Promise<OrganizationMemberItem> {
  const payload = await apiClient.post<RawMemberRecord>(
    `/api/v1/organizations/${encodeURIComponent(orgId)}/members`,
    input,
    { organizationId: orgId },
  );
  return normalizeMember(payload, orgId);
}

/** 切换组织成员启用/停用状态（PATCH /api/v1/organizations/{org_id}/members/{member_id}） */
export async function updateOrgMemberStatus(
  orgId: string,
  memberId: string,
  status: 'ACTIVE' | 'DISABLED',
): Promise<OrganizationMemberItem> {
  const payload = await apiClient.patch<RawMemberRecord>(
    `/api/v1/organizations/${encodeURIComponent(orgId)}/members/${encodeURIComponent(memberId)}`,
    { status },
    { organizationId: orgId },
  );
  return normalizeMember(payload, orgId);
}

/** 查询组织部门树（GET /api/v1/organizations/{org_id}/departments） */
export async function fetchDepartments(orgId: string): Promise<DepartmentNode[]> {
  const payload = await apiClient.get<ListEnvelope<RawDepartmentRecord>>(
    `/api/v1/organizations/${encodeURIComponent(orgId)}/departments`,
    { organizationId: orgId },
  );
  return unwrapList(payload).map((item) => normalizeDepartment(item, orgId));
}

/** 新建部门或子部门（POST /api/v1/organizations/{org_id}/departments） */
export async function createDepartment(
  orgId: string,
  input: { name: string; parent_id?: string | null },
): Promise<DepartmentNode> {
  const payload = await apiClient.post<RawDepartmentRecord>(
    `/api/v1/organizations/${encodeURIComponent(orgId)}/departments`,
    {
      name: input.name,
      parent_id: input.parent_id || null,
    },
    { organizationId: orgId },
  );
  return normalizeDepartment(payload, orgId);
}

/** 更新部门名称或调整父部门（PATCH /api/v1/organizations/{org_id}/departments/{dept_id}） */
export async function updateDepartment(
  orgId: string,
  deptId: string,
  input: { name?: string; parent_id?: string | null; status?: string },
): Promise<DepartmentNode> {
  const payload = await apiClient.patch<RawDepartmentRecord>(
    `/api/v1/organizations/${encodeURIComponent(orgId)}/departments/${encodeURIComponent(deptId)}`,
    input,
    { organizationId: orgId },
  );
  return normalizeDepartment(payload, orgId);
}

/** 查询指定部门的成员兼职关系（GET /api/v1/organizations/{org_id}/departments/{dept_id}/memberships） */
export async function fetchDepartmentMemberships(
  orgId: string,
  deptId: string,
): Promise<DepartmentMembershipItem[]> {
  const payload = await apiClient.get<ListEnvelope<RawDepartmentMembershipRecord>>(
    `/api/v1/organizations/${encodeURIComponent(orgId)}/departments/${encodeURIComponent(deptId)}/memberships`,
    { organizationId: orgId },
  );
  return unwrapList(payload).map((raw) => ({
    id: raw.id,
    organizationId: raw.organizationId ?? raw.organization_id ?? orgId,
    departmentId: raw.departmentId ?? raw.department_id ?? deptId,
    userId: raw.userId ?? raw.user_id ?? '',
    userEmail: raw.userEmail ?? raw.user_email ?? raw.user?.email,
    userDisplayName:
      raw.userDisplayName ??
      raw.user_display_name ??
      raw.user?.displayName ??
      raw.user?.display_name,
    membershipType: raw.membershipType ?? raw.membership_type ?? 'FULL_TIME',
    isPrimary: Boolean(raw.isPrimary ?? raw.is_primary),
    status: raw.status ?? 'ACTIVE',
    validFrom: raw.validFrom ?? raw.valid_from ?? null,
    validUntil: raw.validUntil ?? raw.valid_until ?? null,
  }));
}

/** 为成员分配多部门兼职与有效期（POST /api/v1/organizations/{org_id}/departments/{dept_id}/memberships） */
export async function assignDepartmentMembership(
  orgId: string,
  deptId: string,
  input: {
    user_id: string;
    is_primary: boolean;
    membership_type: string;
    valid_from?: string | null;
    valid_until?: string | null;
  },
): Promise<DepartmentMembershipItem> {
  const raw = await apiClient.post<RawDepartmentMembershipRecord>(
    `/api/v1/organizations/${encodeURIComponent(orgId)}/departments/${encodeURIComponent(deptId)}/memberships`,
    input,
    { organizationId: orgId },
  );
  return {
    id: raw.id,
    organizationId: raw.organizationId ?? raw.organization_id ?? orgId,
    departmentId: raw.departmentId ?? raw.department_id ?? deptId,
    userId: raw.userId ?? raw.user_id ?? input.user_id,
    userEmail: raw.userEmail ?? raw.user_email ?? raw.user?.email,
    userDisplayName: raw.userDisplayName ?? raw.user_display_name ?? raw.user?.display_name,
    membershipType: raw.membershipType ?? raw.membership_type ?? input.membership_type,
    isPrimary: Boolean(raw.isPrimary ?? raw.is_primary ?? input.is_primary),
    status: raw.status ?? 'ACTIVE',
    validFrom: raw.validFrom ?? raw.valid_from ?? input.valid_from ?? null,
    validUntil: raw.validUntil ?? raw.valid_until ?? input.valid_until ?? null,
  };
}

/** 移除部门成员关系（DELETE /api/v1/organizations/{org_id}/departments/{dept_id}/memberships/{membership_id}） */
export async function removeDepartmentMembership(
  orgId: string,
  deptId: string,
  membershipId: string,
): Promise<void> {
  await apiClient.delete<void>(
    `/api/v1/organizations/${encodeURIComponent(orgId)}/departments/${encodeURIComponent(deptId)}/memberships/${encodeURIComponent(membershipId)}`,
    { organizationId: orgId },
  );
}

/** 查询全平台权限树（GET /api/v1/permissions） */
export async function fetchPermissions(): Promise<PermissionItem[]> {
  const payload = await apiClient.get<ListEnvelope<RawPermissionRecord>>('/api/v1/permissions');
  return unwrapList(payload).map((raw) => {
    const [prefix] = raw.code.split(':');
    return {
      id: raw.id ?? raw.code,
      code: raw.code,
      name: raw.name ?? raw.code,
      description: raw.description ?? '受后端强校验保护的功能权限',
      category: raw.category ?? raw.module ?? prefix ?? 'general',
      scope: (raw.scope ?? (prefix === 'system' ? 'SYSTEM' : 'ORGANIZATION')).toUpperCase(),
    };
  });
}

/** 查询当前组织角色清单（同时兼容后端附带的 system_roles 分离字段） */
export async function fetchOrgRoles(orgId: string): Promise<{
  organizationRoles: RoleItem[];
  systemRoles: RoleItem[];
}> {
  type RolesResponse =
    | RawRoleRecord[]
    | {
        items?: RawRoleRecord[];
        organization_roles?: RawRoleRecord[];
        system_roles?: RawRoleRecord[];
      };

  const payload = await apiClient.get<RolesResponse>(
    `/api/v1/organizations/${encodeURIComponent(orgId)}/roles`,
    { organizationId: orgId },
  );

  const normalizeRole = (raw: RawRoleRecord, defaultScope: 'SYSTEM' | 'ORGANIZATION'): RoleItem => {
    const scopeHint = (raw.roleScope ?? raw.role_scope ?? raw.scope ?? '').toUpperCase();
    const roleScope: 'SYSTEM' | 'ORGANIZATION' =
      raw.is_system || scopeHint === 'SYSTEM' ? 'SYSTEM' : defaultScope;
    const perms =
      raw.permission_codes ??
      (raw.permissions ?? [])
        .map((p) => (typeof p === 'string' ? p : (p.code ?? '')))
        .filter(Boolean);

    return {
      id: raw.id,
      organizationId: raw.organizationId ?? raw.organization_id ?? (roleScope === 'ORGANIZATION' ? orgId : null),
      code: raw.code,
      name: raw.name,
      description: raw.description,
      status: raw.status ?? 'ACTIVE',
      roleScope,
      permissions: perms,
      memberCount: raw.memberCount ?? raw.member_count,
    };
  };

  if (Array.isArray(payload)) {
    const normalized = payload.map((item) => normalizeRole(item, 'ORGANIZATION'));
    return {
      organizationRoles: normalized.filter((r) => r.roleScope === 'ORGANIZATION'),
      systemRoles: normalized.filter((r) => r.roleScope === 'SYSTEM'),
    };
  }

  const orgRaw = payload.organization_roles ?? payload.items ?? [];
  const sysRaw = payload.system_roles ?? [];

  const orgNormalized = orgRaw.map((item) => normalizeRole(item, 'ORGANIZATION'));
  const sysNormalized = [
    ...sysRaw.map((item) => normalizeRole(item, 'SYSTEM')),
    ...orgNormalized.filter((r) => r.roleScope === 'SYSTEM'),
  ];

  return {
    organizationRoles: orgNormalized.filter((r) => r.roleScope === 'ORGANIZATION'),
    systemRoles: sysNormalized,
  };
}

/** 创建组织业务角色（POST /api/v1/organizations/{org_id}/roles） */
export async function createOrgRole(
  orgId: string,
  input: { code: string; name: string; description?: string; permission_codes: string[] },
): Promise<RoleItem> {
  const raw = await apiClient.post<RawRoleRecord>(
    `/api/v1/organizations/${encodeURIComponent(orgId)}/roles`,
    input,
    { organizationId: orgId },
  );
  return {
    id: raw.id,
    organizationId: raw.organizationId ?? raw.organization_id ?? orgId,
    code: raw.code,
    name: raw.name,
    description: raw.description,
    status: raw.status ?? 'ACTIVE',
    roleScope: 'ORGANIZATION',
    permissions: raw.permission_codes ?? input.permission_codes,
  };
}

/** 为成员分配组织角色（POST /api/v1/organizations/{org_id}/roles/{role_id}/assign） */
export async function assignOrgRole(
  orgId: string,
  roleId: string,
  input: { user_id: string; valid_from?: string | null; valid_until?: string | null },
): Promise<void> {
  await apiClient.post<unknown>(
    `/api/v1/organizations/${encodeURIComponent(orgId)}/roles/${encodeURIComponent(roleId)}/assign`,
    input,
    { organizationId: orgId },
  );
}

/** 平台账号所属组织简要结构 */
export interface PlatformUserOrgBrief {
  id: string;
  name: string;
  slug: string;
  memberStatus: string;
}

/** 平台级用户管理台账条目 */
export interface PlatformUserItem {
  id: string;
  email: string;
  displayName: string;
  status: 'ACTIVE' | 'DISABLED' | 'LOCKED';
  systemRoles: string[];
  organizations: PlatformUserOrgBrief[];
  createdAt: string;
}

/** 平台级用户分页响应 */
export interface PlatformUserListResult {
  items: PlatformUserItem[];
  total: number;
  offset: number;
  limit: number;
}

interface RawPlatformUserRecord {
  id: string;
  email: string;
  display_name?: string;
  displayName?: string;
  status?: string;
  system_roles?: string[];
  systemRoles?: string[];
  organizations?: Array<{
    id: string;
    name: string;
    slug: string;
    member_status?: string;
    memberStatus?: string;
  }>;
  created_at?: string;
  createdAt?: string;
}

function normalizePlatformUser(raw: RawPlatformUserRecord): PlatformUserItem {
  const upperStatus = (raw.status ?? 'ACTIVE').toUpperCase();
  const status: PlatformUserItem['status'] =
    upperStatus === 'DISABLED' || upperStatus === 'LOCKED' ? upperStatus : 'ACTIVE';
  return {
    id: raw.id,
    email: raw.email,
    displayName: raw.displayName ?? raw.display_name ?? raw.email,
    status,
    systemRoles: raw.systemRoles ?? raw.system_roles ?? [],
    organizations: (raw.organizations ?? []).map((org) => ({
      id: org.id,
      name: org.name,
      slug: org.slug,
      memberStatus: org.memberStatus ?? org.member_status ?? 'ACTIVE',
    })),
    createdAt: raw.createdAt ?? raw.created_at ?? '',
  };
}

/** 查询全平台用户台账（GET /api/v1/users，需 system:user:manage 权限） */
export async function fetchPlatformUsers(params?: {
  q?: string;
  status?: string;
  offset?: number;
  limit?: number;
}): Promise<PlatformUserListResult> {
  const query = new URLSearchParams();
  if (params?.q?.trim()) query.set('q', params.q.trim());
  if (params?.status && params.status !== 'ALL') query.set('status', params.status);
  if (params?.offset !== undefined) query.set('offset', String(params.offset));
  if (params?.limit !== undefined) query.set('limit', String(params.limit));
  const qs = query.toString();
  const payload = await apiClient.get<{
    items?: RawPlatformUserRecord[];
    total?: number;
    offset?: number;
    limit?: number;
  }>(`/api/v1/users${qs ? `?${qs}` : ''}`);
  const items = (payload.items ?? []).map(normalizePlatformUser);
  return {
    items,
    total: payload.total ?? items.length,
    offset: payload.offset ?? 0,
    limit: payload.limit ?? 50,
  };
}

/** 平台管理员新建账号（POST /api/v1/users，需 system:user:manage 权限） */
export async function createPlatformUser(input: {
  email: string;
  display_name: string;
  password: string;
  grant_system_admin?: boolean;
  organization_id?: string | null;
  organization_role_code?: string | null;
}): Promise<PlatformUserItem> {
  const raw = await apiClient.post<RawPlatformUserRecord>('/api/v1/users', input);
  return normalizePlatformUser(raw);
}

/** 平台管理员更新账号资料/状态/密码/系统角色（PATCH /api/v1/users/{user_id}） */
export async function updatePlatformUser(
  userId: string,
  input: {
    display_name?: string;
    status?: 'ACTIVE' | 'DISABLED' | 'LOCKED';
    new_password?: string;
    grant_system_admin?: boolean;
  },
): Promise<PlatformUserItem> {
  const raw = await apiClient.patch<RawPlatformUserRecord>(
    `/api/v1/users/${encodeURIComponent(userId)}`,
    input,
  );
  return normalizePlatformUser(raw);
}

/** 当前用户自助修改密码（POST /api/v1/auth/change-password） */
export async function changeOwnPassword(input: {
  current_password: string;
  new_password: string;
}): Promise<{ status: string; revoked_other_sessions: number }> {
  return apiClient.post<{ status: string; revoked_other_sessions: number }>(
    '/api/v1/auth/change-password',
    input,
  );
}
