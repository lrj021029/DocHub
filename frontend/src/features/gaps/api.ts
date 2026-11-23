import { ApiError, apiClient } from '@/lib/api-client';
import { createIdempotencyKey } from '@/features/documents/api';
import type { KnowledgeGapStatus } from '@/types/domain';

/** Knowledge Gap 补充任务记录 */
export interface KnowledgeGapTaskItem {
  id: string;
  gapId: string;
  assigneeId?: string | null;
  assigneeName?: string | null;
  priority: 'LOW' | 'MEDIUM' | 'HIGH' | 'URGENT' | string;
  status: KnowledgeGapStatus;
  dueAt?: string | null;
  relatedDocumentId?: string | null;
  regressionPassed?: boolean | null;
}

/** Knowledge Gap 聚合对象 */
export interface KnowledgeGapItem {
  id: string;
  organizationId: string;
  spaceId?: string | null;
  spaceName?: string | null;
  topic: string;
  eventType: 'NO_RETRIEVAL' | 'LOW_RELEVANCE' | 'INSUFFICIENT_AUTHORIZED_EVIDENCE' | string;
  occurrenceCount: number;
  status: KnowledgeGapStatus;
  priority: string;
  tasks: KnowledgeGapTaskItem[];
  lastSeenAt?: string | null;
}

/** 11 态 Knowledge Gap 状态机枚举 */
export const KNOWLEDGE_GAP_STATUSES: readonly KnowledgeGapStatus[] = [
  'DETECTED',
  'TRIAGED',
  'ASSIGNED',
  'IN_PROGRESS',
  'CONTENT_SUBMITTED',
  'REVIEWING',
  'RESOLVED',
  'REJECTED',
  'DUPLICATED',
  'CLOSED',
  'REOPENED',
] as const;

type ListEnvelope<T> = T[] | { items?: T[]; data?: T[] };

function unwrapList<T>(payload: ListEnvelope<T>): T[] {
  if (Array.isArray(payload)) return payload;
  if (Array.isArray(payload.items)) return payload.items;
  if (Array.isArray(payload.data)) return payload.data;
  return [];
}

/** 格式化 Gap 接口错误信息 */
export function formatGapError(error: unknown, fallback = 'Knowledge Gap 操作失败'): string {
  if (error instanceof ApiError) {
    if (error.status === 409 || error.status === 422) {
      return `闭环门禁拦截（HTTP ${error.status}）：${error.message || '必须关联补充文档并通过回归验证后方可标记 RESOLVED'}`;
    }
    return `${error.message || fallback}（${error.code}）`;
  }
  return error instanceof Error ? error.message : fallback;
}

/** 获取 Knowledge Gap 清单（GET /api/v1/knowledge-gaps） */
export async function fetchKnowledgeGaps(orgId: string): Promise<KnowledgeGapItem[]> {
  interface RawTask {
    id: string;
    gap_id?: string;
    assignee_id?: string | null;
    assignee_name?: string | null;
    priority?: string;
    status?: string;
    due_at?: string | null;
    related_document_id?: string | null;
    regression_passed?: boolean | null;
  }
  interface RawGap {
    id: string;
    organization_id?: string;
    space_id?: string | null;
    space_name?: string | null;
    topic?: string;
    normalized_question?: string;
    event_type?: string;
    occurrence_count?: number;
    status?: string;
    priority?: string;
    tasks?: RawTask[];
    last_seen_at?: string | null;
  }

  const payload = await apiClient.get<ListEnvelope<RawGap>>('/api/v1/knowledge-gaps', {
    organizationId: orgId,
  });

  return unwrapList(payload).map((raw) => ({
    id: raw.id,
    organizationId: raw.organization_id ?? orgId,
    spaceId: raw.space_id ?? null,
    spaceName: raw.space_name ?? null,
    topic: raw.topic ?? raw.normalized_question ?? '未命名知识缺口',
    eventType: raw.event_type ?? 'NO_RETRIEVAL',
    occurrenceCount: raw.occurrence_count ?? 1,
    status: (raw.status ?? 'DETECTED').toUpperCase() as KnowledgeGapStatus,
    priority: (raw.priority ?? 'MEDIUM').toUpperCase(),
    lastSeenAt: raw.last_seen_at ?? null,
    tasks: (raw.tasks ?? []).map((t) => ({
      id: t.id,
      gapId: t.gap_id ?? raw.id,
      assigneeId: t.assignee_id ?? null,
      assigneeName: t.assignee_name ?? null,
      priority: (t.priority ?? 'MEDIUM').toUpperCase(),
      status: (t.status ?? 'ASSIGNED').toUpperCase() as KnowledgeGapStatus,
      dueAt: t.due_at ?? null,
      relatedDocumentId: t.related_document_id ?? null,
      regressionPassed: t.regression_passed ?? null,
    })),
  }));
}

/** 创建 Knowledge Gap 补充任务（POST /api/v1/knowledge-gaps/{id}/tasks） */
export async function createKnowledgeGapTask(
  orgId: string,
  gapId: string,
  input: {
    assignee_id: string;
    priority: string;
    due_at?: string | null;
    related_document_id?: string | null;
  },
): Promise<void> {
  await apiClient.post<unknown>(
    `/api/v1/knowledge-gaps/${encodeURIComponent(gapId)}/tasks`,
    input,
    {
      organizationId: orgId,
      idempotencyKey: createIdempotencyKey(`gap-task-${gapId}`),
    },
  );
}

/** 更新补充任务、关联文档或触发回归验证关闭（PATCH /api/v1/knowledge-gap-tasks/{id}） */
export async function updateKnowledgeGapTask(
  orgId: string,
  taskId: string,
  input: {
    status?: KnowledgeGapStatus;
    related_document_id?: string | null;
    run_regression?: boolean;
  },
): Promise<{ status?: string; regression_passed?: boolean }> {
  return apiClient.patch<{ status?: string; regression_passed?: boolean }>(
    `/api/v1/knowledge-gap-tasks/${encodeURIComponent(taskId)}`,
    input,
    { organizationId: orgId },
  );
}
