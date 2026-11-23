/**
 * DocHub 前端共享领域类型定义（阶段 0 基线契约）
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

/** 当前登录用户身份信息（不含任何 Secret） */
export interface AuthUser {
  id: string;
  email: string;
  displayName: string;
  systemRoles: string[];
  organizationRoles: string[];
  permissions: string[];
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
