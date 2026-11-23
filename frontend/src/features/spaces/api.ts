import { ApiError, apiClient } from '@/lib/api-client';

/** Knowledge Space 可见性枚举 */
export type SpaceVisibility = 'PUBLIC' | 'PRIVATE';

/** Knowledge Space 回答策略枚举 */
export type SpaceAnswerPolicy = 'STRICT_EVIDENCE' | 'GROUNDED_WITH_GENERAL_CONTEXT';

/** Knowledge Space 领域模型 */
export interface KnowledgeSpaceItem {
  id: string;
  organizationId: string;
  name: string;
  description: string;
  visibility: SpaceVisibility;
  answerPolicy: SpaceAnswerPolicy;
  defaultEmbeddingProfileId: string | null;
  embeddingProfileName: string;
  status: 'ACTIVE' | 'ARCHIVED' | 'DISABLED' | string;
  configVersion: number;
  createdAt?: string | null;
}

/** 私有 Space 显式成员记录 */
export interface SpaceMemberItem {
  id: string;
  spaceId: string;
  userId: string;
  userEmail?: string;
  userDisplayName?: string;
  status: 'ACTIVE' | 'REVOKED' | 'EXPIRED' | string;
  validFrom?: string | null;
  validUntil?: string | null;
}

type ListEnvelope<T> = T[] | { items?: T[]; data?: T[]; total?: number };

function unwrapList<T>(payload: ListEnvelope<T>): T[] {
  if (Array.isArray(payload)) return payload;
  if (Array.isArray(payload.items)) return payload.items;
  if (Array.isArray(payload.data)) return payload.data;
  return [];
}

interface RawSpaceRecord {
  id: string;
  organization_id?: string;
  organizationId?: string;
  name: string;
  description?: string | null;
  visibility?: string;
  answer_policy?: string;
  answerPolicy?: string;
  default_embedding_profile_id?: string | null;
  defaultEmbeddingProfileId?: string | null;
  embedding_profile_name?: string | null;
  embeddingProfileName?: string | null;
  status?: string;
  config_version?: number;
  configVersion?: number;
  created_at?: string | null;
}

interface RawSpaceMemberRecord {
  id?: string;
  space_id?: string;
  spaceId?: string;
  user_id?: string;
  userId?: string;
  email?: string;
  user_email?: string;
  userEmail?: string;
  display_name?: string;
  user_display_name?: string;
  userDisplayName?: string;
  user?: { id?: string; email?: string; display_name?: string };
  status?: string;
  valid_from?: string | null;
  validFrom?: string | null;
  valid_until?: string | null;
  validUntil?: string | null;
}

/** 格式化 Space 接口错误信息 */
export function formatSpaceError(error: unknown, fallback = '操作失败，请稍后重试'): string {
  if (error instanceof ApiError) {
    if (error.status === 403) {
      return `无权执行此操作（HTTP 403）：${error.message || '缺少 Knowledge Space 管理权限'}`;
    }
    if (error.status === 409) {
      return `配置冲突（HTTP 409）：${error.message || 'Space 名称或版本存在冲突'}`;
    }
    return `${error.message || fallback}（${error.code}）`;
  }
  return error instanceof Error ? error.message : fallback;
}

function normalizeSpace(raw: RawSpaceRecord, fallbackOrgId: string): KnowledgeSpaceItem {
  const vis = (raw.visibility ?? 'PRIVATE').toUpperCase();
  const policy = (raw.answerPolicy ?? raw.answer_policy ?? 'STRICT_EVIDENCE').toUpperCase();
  const profileId = raw.defaultEmbeddingProfileId ?? raw.default_embedding_profile_id ?? null;

  return {
    id: raw.id,
    organizationId: raw.organizationId ?? raw.organization_id ?? fallbackOrgId,
    name: raw.name,
    description: raw.description ?? '',
    visibility: vis === 'PUBLIC' ? 'PUBLIC' : 'PRIVATE',
    answerPolicy:
      policy === 'GROUNDED_WITH_GENERAL_CONTEXT'
        ? 'GROUNDED_WITH_GENERAL_CONTEXT'
        : 'STRICT_EVIDENCE',
    defaultEmbeddingProfileId: profileId,
    embeddingProfileName:
      raw.embeddingProfileName ??
      raw.embedding_profile_name ??
      (profileId ? `Profile (${profileId.slice(0, 8)})` : '默认 Qwen Embedding Profile'),
    status: (raw.status ?? 'ACTIVE').toUpperCase(),
    configVersion: raw.configVersion ?? raw.config_version ?? 1,
    createdAt: raw.created_at ?? null,
  };
}

function normalizeSpaceMember(raw: RawSpaceMemberRecord, fallbackSpaceId: string): SpaceMemberItem {
  const userId = raw.userId ?? raw.user_id ?? raw.user?.id ?? '';
  return {
    id: raw.id ?? `${fallbackSpaceId}-${userId}`,
    spaceId: raw.spaceId ?? raw.space_id ?? fallbackSpaceId,
    userId,
    userEmail: raw.userEmail ?? raw.user_email ?? raw.email ?? raw.user?.email,
    userDisplayName:
      raw.userDisplayName ?? raw.user_display_name ?? raw.display_name ?? raw.user?.display_name,
    status: (raw.status ?? 'ACTIVE').toUpperCase(),
    validFrom: raw.validFrom ?? raw.valid_from ?? null,
    validUntil: raw.validUntil ?? raw.valid_until ?? null,
  };
}

/** 获取当前组织下的 Knowledge Space 列表（GET /api/v1/organizations/{org_id}/spaces） */
export async function fetchKnowledgeSpaces(orgId: string): Promise<KnowledgeSpaceItem[]> {
  const payload = await apiClient.get<ListEnvelope<RawSpaceRecord>>(
    `/api/v1/organizations/${encodeURIComponent(orgId)}/spaces`,
    { organizationId: orgId },
  );
  return unwrapList(payload).map((item) => normalizeSpace(item, orgId));
}

/** 新建 Knowledge Space（POST /api/v1/organizations/{org_id}/spaces） */
export async function createKnowledgeSpace(
  orgId: string,
  input: {
    name: string;
    description?: string;
    visibility: SpaceVisibility;
    answer_policy: SpaceAnswerPolicy;
    default_embedding_profile_id?: string | null;
  },
): Promise<KnowledgeSpaceItem> {
  const payload = await apiClient.post<RawSpaceRecord>(
    `/api/v1/organizations/${encodeURIComponent(orgId)}/spaces`,
    input,
    { organizationId: orgId },
  );
  return normalizeSpace(payload, orgId);
}

/** 编辑 Knowledge Space 配置（PATCH /api/v1/organizations/{org_id}/spaces/{space_id}） */
export async function updateKnowledgeSpace(
  orgId: string,
  spaceId: string,
  input: {
    name?: string;
    description?: string;
    visibility?: SpaceVisibility;
    answer_policy?: SpaceAnswerPolicy;
    default_embedding_profile_id?: string | null;
    status?: string;
  },
): Promise<KnowledgeSpaceItem> {
  const payload = await apiClient.patch<RawSpaceRecord>(
    `/api/v1/organizations/${encodeURIComponent(orgId)}/spaces/${encodeURIComponent(spaceId)}`,
    input,
    { organizationId: orgId },
  );
  return normalizeSpace(payload, orgId);
}

/** 查询私有 Space 显式成员清单（GET /api/v1/spaces/{space_id}/members） */
export async function fetchSpaceMembers(
  orgId: string,
  spaceId: string,
): Promise<SpaceMemberItem[]> {
  const payload = await apiClient.get<ListEnvelope<RawSpaceMemberRecord>>(
    `/api/v1/spaces/${encodeURIComponent(spaceId)}/members`,
    { organizationId: orgId },
  );
  return unwrapList(payload).map((item) => normalizeSpaceMember(item, spaceId));
}

/**
 * 更新私有 Space 显式成员（PATCH /api/v1/spaces/{space_id}/members）。
 * 支持新增/续期成员（upsert）与移除成员（remove_user_ids）。
 */
export async function patchSpaceMembers(
  orgId: string,
  spaceId: string,
  input: {
    upsert?: Array<{
      user_id: string;
      valid_from?: string | null;
      valid_until?: string | null;
      status?: 'ACTIVE' | 'REVOKED';
    }>;
    remove_user_ids?: string[];
  },
): Promise<SpaceMemberItem[]> {
  const payload = await apiClient.patch<ListEnvelope<RawSpaceMemberRecord>>(
    `/api/v1/spaces/${encodeURIComponent(spaceId)}/members`,
    input,
    { organizationId: orgId },
  );
  return unwrapList(payload).map((item) => normalizeSpaceMember(item, spaceId));
}
