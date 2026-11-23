/**
 * DocHub 前端共享领域类型定义（阶段 0 + Slice 1 IAM 契约）
 * 与 PRD.md 及 TECHNICAL_SPEC.md 第 5/9/10/11/12 节保持一致。
 */

/** 当前用户所属的组织摘要 */
export interface OrganizationSummary {
  id: string;
  name: string;
  slug: string;
  status: 'ACTIVE' | 'SUSPENDED' | 'ARCHIVED';
  roleNames: string[];
}

/** 当前用户的有效部门成员关系摘要（来自 /auth/me） */
export interface UserDepartmentSummary {
  departmentId: string;
  departmentName: string;
  isPrimary: boolean;
  membershipType: string;
  validFrom?: string | null;
  validUntil?: string | null;
}

/** 当前登录用户身份信息（不含任何 Secret） */
export interface AuthUser {
  id: string;
  email: string;
  displayName: string;
  status?: string;
  systemRoles: string[];
  organizationRoles: string[];
  permissions: string[];
  departments?: UserDepartmentSummary[];
}

/** 组织成员记录 */
export interface OrganizationMemberItem {
  id: string;
  organizationId: string;
  userId: string;
  email: string;
  displayName: string;
  status: 'ACTIVE' | 'DISABLED' | 'INVITED' | 'REMOVED';
  joinedAt?: string | null;
  organizationRoles: string[];
  systemRoles: string[];
  departments: string[];
}

/** 部门节点模型 */
export interface DepartmentNode {
  id: string;
  organizationId: string;
  parentId: string | null;
  name: string;
  status: 'ACTIVE' | 'DISABLED' | 'ARCHIVED';
  depth?: number;
  memberCount?: number;
  children?: DepartmentNode[];
}

/** 部门成员兼职关系记录 */
export interface DepartmentMembershipItem {
  id: string;
  organizationId: string;
  departmentId: string;
  userId: string;
  userEmail?: string;
  userDisplayName?: string;
  membershipType: 'FULL_TIME' | 'PART_TIME' | 'TEMPORARY' | string;
  isPrimary: boolean;
  status: 'ACTIVE' | 'INACTIVE' | 'EXPIRED' | string;
  validFrom?: string | null;
  validUntil?: string | null;
}

/** 权限定义节点（来自 GET /api/v1/permissions） */
export interface PermissionItem {
  id: string;
  code: string;
  name: string;
  description: string;
  category: string;
  scope: 'SYSTEM' | 'ORGANIZATION' | string;
}

/** 角色定义（严格区分 SystemRole 与 OrganizationRole） */
export interface RoleItem {
  id: string;
  organizationId?: string | null;
  code: string;
  name: string;
  description?: string;
  status: 'ACTIVE' | 'DISABLED' | string;
  roleScope: 'SYSTEM' | 'ORGANIZATION';
  permissions: string[];
  memberCount?: number;
}

/** 敏感等级枚举（L0-L3，遵循外发安全红线） */
export type SensitivityLevel = 'L0' | 'L1' | 'L2' | 'L3';

/** 文档解析分阶段任务状态（各阶段独立状态机，严禁单状态覆盖） */
export type ImportJobStatus =
  | 'CREATED'
  | 'UPLOADING'
  | 'VERIFYING'
  | 'QUARANTINED'
  | 'REJECTED'
  | 'FAILED';

export type ParseJobStatus =
  | 'QUEUED'
  | 'SUBMITTING'
  | 'SUBMITTED'
  | 'RUNNING'
  | 'SUCCEEDED'
  | 'FAILED'
  | 'CANCEL_REQUESTED'
  | 'CANCELLED'
  | 'UNKNOWN'
  | 'BLOCKED_POLICY';

/** 四维 Document ACL 主体类型（Allow OR 模型） */
export type AclSubjectType = 'GLOBAL' | 'DEPARTMENT' | 'ROLE' | 'USER';

/** FAQ 三轴解耦状态（遵循 ADR-001 裁决与 TECHNICAL_SPEC §10.5） */
export type FaqLifecycleState = 'CANDIDATE' | 'PENDING_REVIEW' | 'REJECTED' | 'ARCHIVED';
export type FaqPublicationState = 'UNPUBLISHED' | 'PUBLISHED' | 'DISABLED';
export type FaqSourceHealthState = 'HEALTHY' | 'STALE' | 'EXPIRED';

/** Knowledge Gap 任务状态机 */
export type KnowledgeGapStatus =
  | 'DETECTED'
  | 'TRIAGED'
  | 'ASSIGNED'
  | 'IN_PROGRESS'
  | 'CONTENT_SUBMITTED'
  | 'REVIEWING'
  | 'RESOLVED'
  | 'REJECTED'
  | 'DUPLICATED'
  | 'CLOSED'
  | 'REOPENED';

/** 后端就绪探针（/api/v1/readyz）响应结构 */
export interface ReadinessStatus {
  status: 'ok' | 'ready' | 'degraded' | 'unavailable';
  checks?: Record<string, string | boolean | { status: string; detail?: string }>;
  timestamp?: string;
}
