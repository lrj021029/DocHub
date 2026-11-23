import { ApiError, apiClient } from '@/lib/api-client';
import { createIdempotencyKey } from '@/features/documents/api';
import type { AclSubjectType } from '@/types/domain';

/** 部门继承模式：动态追踪 Closure Table 或固化授权时刻快照 */
export type DepartmentInheritanceMode = 'DYNAMIC' | 'SNAPSHOT';

/** 单条四维 ACL 授权规则 */
export interface AclGrantItem {
  id?: string;
  subjectType: AclSubjectType;
  subjectId: string | null;
  subjectLabel?: string;
  includeDescendants: boolean;
  inheritanceMode: DepartmentInheritanceMode;
  validFrom?: string | null;
  validUntil?: string | null;
}

/** 文档当前 ACL 聚合状态（GET /api/v1/documents/{document_id}/acl） */
export interface DocumentAclState {
  documentId: string;
  revision: number;
  status: string;
  grants: AclGrantItem[];
  updatedAt?: string | null;
}

/** 不可变 ACLRevision 历史与变更 Diff 条目 */
export interface AclRevisionItem {
  id: string;
  revision: number;
  changedBy: string;
  changedByEmail?: string;
  reason: string;
  createdAt: string;
  addedGrants: string[];
  removedGrants: string[];
  snapshotCount?: number;
}

interface RawGrantRecord {
  id?: string;
  subject_type?: string;
  subjectType?: string;
  subject_id?: string | null;
  subjectId?: string | null;
  subject_label?: string;
  subjectLabel?: string;
  include_descendants?: boolean;
  includeDescendants?: boolean;
  inheritance_mode?: string;
  inheritanceMode?: string;
  valid_from?: string | null;
  validFrom?: string | null;
  valid_until?: string | null;
  validUntil?: string | null;
}

interface RawDocumentAclResponse {
  document_id?: string;
  documentId?: string;
  revision?: number;
  status?: string;
  grants?: RawGrantRecord[];
  updated_at?: string | null;
}

interface RawRevisionRecord {
  id: string;
  revision: number;
  changed_by?: string;
  changedBy?: string;
  changed_by_email?: string;
  changedByEmail?: string;
  reason?: string;
  created_at?: string;
  createdAt?: string;
  diff?: {
    added?: Array<string | RawGrantRecord>;
    removed?: Array<string | RawGrantRecord>;
  };
  diff_json?: {
    added?: Array<string | RawGrantRecord>;
    removed?: Array<string | RawGrantRecord>;
  };
  snapshot_json?: { grants?: RawGrantRecord[] } | RawGrantRecord[];
}

/** 将原始 Grant 对象渲染为人类可读摘要（供 Diff 展示） */
export function describeGrantEntry(entry: string | RawGrantRecord): string {
  if (typeof entry === 'string') return entry;
  const type = (entry.subjectType ?? entry.subject_type ?? 'USER').toUpperCase();
  if (type === 'GLOBAL') return 'GLOBAL（当前 Space 全员）';
  const id = entry.subjectLabel ?? entry.subject_label ?? entry.subjectId ?? entry.subject_id ?? '—';
  if (type === 'DEPARTMENT') {
    const desc = (entry.includeDescendants ?? entry.include_descendants) ? '含子部门' : '仅本部门';
    const mode = (entry.inheritanceMode ?? entry.inheritance_mode ?? 'DYNAMIC').toUpperCase();
    return `DEPARTMENT:${id}（${desc} / ${mode}）`;
  }
  if (type === 'USER' && (entry.validUntil ?? entry.valid_until)) {
    const until = (entry.validUntil ?? entry.valid_until ?? '').slice(0, 16).replace('T', ' ');
    return `USER:${id}（限时至 ${until}）`;
  }
  return `${type}:${id}`;
}

function normalizeGrant(raw: RawGrantRecord): AclGrantItem {
  const rawType = (raw.subjectType ?? raw.subject_type ?? 'USER').toUpperCase();
  const subjectType: AclSubjectType =
    rawType === 'GLOBAL' || rawType === 'DEPARTMENT' || rawType === 'ROLE' ? rawType : 'USER';
  const rawMode = (raw.inheritanceMode ?? raw.inheritance_mode ?? 'DYNAMIC').toUpperCase();

  return {
    id: raw.id,
    subjectType,
    subjectId: subjectType === 'GLOBAL' ? null : (raw.subjectId ?? raw.subject_id ?? null),
    subjectLabel: raw.subjectLabel ?? raw.subject_label,
    includeDescendants: Boolean(raw.includeDescendants ?? raw.include_descendants),
    inheritanceMode: rawMode === 'SNAPSHOT' ? 'SNAPSHOT' : 'DYNAMIC',
    validFrom: raw.validFrom ?? raw.valid_from ?? null,
    validUntil: raw.validUntil ?? raw.valid_until ?? null,
  };
}

/** 判断是否为乐观锁版本冲突（HTTP 409） */
export function isAclRevisionConflictError(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409;
}

/** 格式化 ACL 接口错误信息 */
export function formatAclError(error: unknown, fallback = 'ACL 操作失败'): string {
  if (error instanceof ApiError) {
    if (error.status === 409) {
      return `乐观锁版本冲突（HTTP 409）：该文档 ACL 已被其他管理员更新，请先刷新最新 Revision 后重新核对并提交。`;
    }
    if (error.status === 403) {
      return `无权管理该文档 ACL（HTTP 403）：${error.message || '后端权限校验已拒绝'}`;
    }
    return `${error.message || fallback}（${error.code}）`;
  }
  return error instanceof Error ? error.message : fallback;
}

/** 获取文档当前 ACL 状态（GET /api/v1/documents/{document_id}/acl） */
export async function fetchDocumentAcl(
  orgId: string,
  documentId: string,
): Promise<DocumentAclState> {
  const raw = await apiClient.get<RawDocumentAclResponse>(
    `/api/v1/documents/${encodeURIComponent(documentId)}/acl`,
    { organizationId: orgId },
  );
  return {
    documentId: raw.documentId ?? raw.document_id ?? documentId,
    revision: raw.revision ?? 0,
    status: raw.status ?? 'ACTIVE',
    grants: (raw.grants ?? []).map(normalizeGrant),
    updatedAt: raw.updated_at ?? null,
  };
}

/** 获取文档 ACLRevision 历史时间线与 Diff（GET /api/v1/documents/{document_id}/acl/revisions） */
export async function fetchDocumentAclRevisions(
  orgId: string,
  documentId: string,
): Promise<AclRevisionItem[]> {
  type Envelope = RawRevisionRecord[] | { items?: RawRevisionRecord[]; data?: RawRevisionRecord[] };
  const payload = await apiClient.get<Envelope>(
    `/api/v1/documents/${encodeURIComponent(documentId)}/acl/revisions`,
    { organizationId: orgId },
  );
  const rawList = Array.isArray(payload)
    ? payload
    : Array.isArray(payload.items)
      ? payload.items
      : Array.isArray(payload.data)
        ? payload.data
        : [];

  return rawList.map((rev) => {
    const diff = rev.diff ?? rev.diff_json ?? {};
    const snapshotGrants = Array.isArray(rev.snapshot_json)
      ? rev.snapshot_json
      : rev.snapshot_json?.grants;
    return {
      id: rev.id,
      revision: rev.revision,
      changedBy: rev.changedBy ?? rev.changed_by ?? '管理员',
      changedByEmail: rev.changedByEmail ?? rev.changed_by_email,
      reason: rev.reason ?? '未填写原因',
      createdAt: rev.createdAt ?? rev.created_at ?? '',
      addedGrants: (diff.added ?? []).map(describeGrantEntry),
      removedGrants: (diff.removed ?? []).map(describeGrantEntry),
      snapshotCount: snapshotGrants?.length,
    };
  });
}

/**
 * 唯一 ACL 写入口（遵循 ADR-001 裁决）：
 * POST /api/v1/documents/{document_id}/acl/revisions
 * 强制携带 expected_revision（乐观锁）、reason（变更审计理由）与 Idempotency-Key 请求头。
 */
export async function submitDocumentAclRevision(
  orgId: string,
  documentId: string,
  input: {
    expected_revision: number;
    reason: string;
    grants: Array<{
      subject_type: AclSubjectType;
      subject_id: string | null;
      include_descendants?: boolean;
      inheritance_mode?: DepartmentInheritanceMode;
      valid_from?: string | null;
      valid_until?: string | null;
    }>;
  },
): Promise<DocumentAclState> {
  const raw = await apiClient.post<RawDocumentAclResponse>(
    `/api/v1/documents/${encodeURIComponent(documentId)}/acl/revisions`,
    input,
    {
      organizationId: orgId,
      idempotencyKey: createIdempotencyKey(`acl-rev-${documentId}`),
    },
  );
  return {
    documentId: raw.documentId ?? raw.document_id ?? documentId,
    revision: raw.revision ?? input.expected_revision + 1,
    status: raw.status ?? 'ACTIVE',
    grants: (raw.grants ?? []).map(normalizeGrant),
    updatedAt: raw.updated_at ?? null,
  };
}
