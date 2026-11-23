import { ApiError, apiClient } from '@/lib/api-client';
import { createIdempotencyKey } from '@/features/documents/api';
import type {
  FaqLifecycleState,
  FaqPublicationState,
  FaqSourceHealthState,
} from '@/types/domain';

/** FAQ 候选对象 */
export interface FaqCandidateItem {
  id: string;
  faqId?: string | null;
  clusterId?: string | null;
  question: string;
  answerDraft: string;
  frequencyCount: number;
  uniqueUserCount: number;
  confidenceScore: number;
  lifecycleState: FaqLifecycleState;
  sourceCitationCount: number;
  createdBy?: string | null;
  createdAt?: string | null;
}

/** 已发布/受管 FAQ 三轴状态台账对象（遵循 ADR-001 三轴解耦模型） */
export interface PublishedFaqItem {
  id: string;
  organizationId: string;
  spaceId?: string | null;
  question: string;
  answerSummary: string;
  versionNo: number;
  /** 轴 1：生命周期状态 */
  lifecycleState: FaqLifecycleState;
  /** 轴 2：发布状态（UNPUBLISHED / PUBLISHED / DISABLED） */
  publicationState: FaqPublicationState;
  /** 轴 3：来源健康度（HEALTHY / STALE / EXPIRED） */
  sourceHealthState: FaqSourceHealthState;
  isManual: boolean;
  maintainer?: string | null;
  expiresAt?: string | null;
  cacheStatus?: string;
  cacheHitCount?: number;
  aclRevision?: number;
}

type ListEnvelope<T> = T[] | { items?: T[]; data?: T[] };

function unwrapList<T>(payload: ListEnvelope<T>): T[] {
  if (Array.isArray(payload)) return payload;
  if (Array.isArray(payload.items)) return payload.items;
  if (Array.isArray(payload.data)) return payload.data;
  return [];
}

/** 格式化 FAQ 模块错误信息 */
export function formatFaqError(error: unknown, fallback = 'FAQ 操作失败'): string {
  if (error instanceof ApiError) {
    if (error.status === 403) {
      return `操作被拒绝（HTTP 403）：${error.message || '缺少审批权限，或创建者禁止审核自己提交的 FAQ'}`;
    }
    return `${error.message || fallback}（${error.code}）`;
  }
  return error instanceof Error ? error.message : fallback;
}

/** 手动触发 FAQ 挖掘（POST /api/v1/faq-mining/runs） */
export async function triggerFaqMiningRun(
  orgId: string,
  input: { space_id?: string | null; window_days?: number } = {},
): Promise<{ runId?: string; status: string }> {
  const res = await apiClient.post<{ run_id?: string; id?: string; status?: string }>(
    '/api/v1/faq-mining/runs',
    input,
    {
      organizationId: orgId,
      idempotencyKey: createIdempotencyKey('faq-mining-run'),
    },
  );
  return { runId: res.run_id ?? res.id, status: res.status ?? 'QUEUED' };
}

/** 获取 FAQ 挖掘候选队列（GET /api/v1/faq-candidates） */
export async function fetchFaqCandidates(orgId: string): Promise<FaqCandidateItem[]> {
  interface RawCandidate {
    id: string;
    faq_id?: string | null;
    cluster_id?: string | null;
    question?: string;
    normalized_question?: string;
    answer_draft?: string;
    generated_answer?: string;
    frequency_count?: number;
    total_frequency?: number;
    unique_user_count?: number;
    unique_users?: number;
    confidence_score?: number;
    confidence?: number;
    lifecycle_state?: string;
    candidate_status?: string;
    source_citation_count?: number;
    citations?: unknown[];
    created_by?: string | null;
    created_at?: string | null;
  }

  const payload = await apiClient.get<ListEnvelope<RawCandidate>>('/api/v1/faq-candidates', {
    organizationId: orgId,
  });

  return unwrapList(payload).map((raw) => {
    const state = (raw.lifecycle_state ?? raw.candidate_status ?? 'CANDIDATE').toUpperCase();
    const lifecycleState: FaqLifecycleState =
      state === 'PENDING_REVIEW' || state === 'REJECTED' || state === 'ARCHIVED'
        ? state
        : 'CANDIDATE';

    return {
      id: raw.id,
      faqId: raw.faq_id ?? raw.id,
      clusterId: raw.cluster_id ?? null,
      question: raw.question ?? raw.normalized_question ?? '未命名问题簇',
      answerDraft: raw.answer_draft ?? raw.generated_answer ?? '',
      frequencyCount: raw.frequency_count ?? raw.total_frequency ?? 0,
      uniqueUserCount: raw.unique_user_count ?? raw.unique_users ?? 0,
      confidenceScore: raw.confidence_score ?? raw.confidence ?? 0,
      lifecycleState,
      sourceCitationCount: raw.source_citation_count ?? raw.citations?.length ?? 0,
      createdBy: raw.created_by ?? null,
      createdAt: raw.created_at ?? null,
    };
  });
}

/** 将候选提交人工审核（POST /api/v1/faq-candidates/{id}/submit-review） */
export async function submitFaqCandidateForReview(
  orgId: string,
  candidateId: string,
): Promise<void> {
  await apiClient.post<unknown>(
    `/api/v1/faq-candidates/${encodeURIComponent(candidateId)}/submit-review`,
    {},
    {
      organizationId: orgId,
      idempotencyKey: createIdempotencyKey(`faq-submit-${candidateId}`),
    },
  );
}

/** 审核决策（POST /api/v1/faq/{id}/reviews） */
export async function submitFaqReviewDecision(
  orgId: string,
  faqId: string,
  input: { decision: 'APPROVE' | 'REJECT'; comments: string },
): Promise<void> {
  await apiClient.post<unknown>(
    `/api/v1/faq/${encodeURIComponent(faqId)}/reviews`,
    input,
    {
      organizationId: orgId,
      idempotencyKey: createIdempotencyKey(`faq-review-${faqId}`),
    },
  );
}

/** 发布不可变 FAQVersion（POST /api/v1/faq/{id}/publish） */
export async function publishFaqVersion(orgId: string, faqId: string): Promise<void> {
  await apiClient.post<unknown>(
    `/api/v1/faq/${encodeURIComponent(faqId)}/publish`,
    {},
    {
      organizationId: orgId,
      idempotencyKey: createIdempotencyKey(`faq-pub-${faqId}`),
    },
  );
}

/** 停用已发布 FAQ（POST /api/v1/faq/{id}/disable） */
export async function disableFaqItem(orgId: string, faqId: string): Promise<void> {
  await apiClient.post<unknown>(
    `/api/v1/faq/${encodeURIComponent(faqId)}/disable`,
    {},
    {
      organizationId: orgId,
      idempotencyKey: createIdempotencyKey(`faq-disable-${faqId}`),
    },
  );
}

/** 获取 FAQ 三轴状态台账（GET /api/v1/faq） */
export async function fetchFaqList(orgId: string): Promise<PublishedFaqItem[]> {
  interface RawFaq {
    id: string;
    organization_id?: string;
    space_id?: string | null;
    question?: string;
    answer_summary?: string;
    answer?: string;
    version_no?: number;
    lifecycle_state?: string;
    publication_state?: string;
    source_health_state?: string;
    is_manual?: boolean;
    source_type?: string;
    maintainer?: string | null;
    expires_at?: string | null;
    cache_status?: string;
    cache_hit_count?: number;
    acl_revision?: number;
  }

  const payload = await apiClient.get<ListEnvelope<RawFaq>>('/api/v1/faq', {
    organizationId: orgId,
  });

  return unwrapList(payload).map((raw) => {
    const life = (raw.lifecycle_state ?? 'ARCHIVED').toUpperCase();
    const pub = (raw.publication_state ?? 'UNPUBLISHED').toUpperCase();
    const health = (raw.source_health_state ?? 'HEALTHY').toUpperCase();

    return {
      id: raw.id,
      organizationId: raw.organization_id ?? orgId,
      spaceId: raw.space_id ?? null,
      question: raw.question ?? '未命名 FAQ',
      answerSummary: raw.answer_summary ?? raw.answer ?? '',
      versionNo: raw.version_no ?? 1,
      lifecycleState:
        life === 'CANDIDATE' || life === 'PENDING_REVIEW' || life === 'REJECTED'
          ? life
          : 'ARCHIVED',
      publicationState:
        pub === 'PUBLISHED' || pub === 'DISABLED' ? pub : 'UNPUBLISHED',
      sourceHealthState:
        health === 'STALE' || health === 'EXPIRED' ? health : 'HEALTHY',
      isManual: Boolean(raw.is_manual || raw.source_type === 'MANUAL'),
      maintainer: raw.maintainer ?? null,
      expiresAt: raw.expires_at ?? null,
      cacheStatus: raw.cache_status ?? 'SYNCED',
      cacheHitCount: raw.cache_hit_count ?? 0,
      aclRevision: raw.acl_revision ?? 1,
    };
  });
}

/** 手工创建 MANUAL FAQ（POST /api/v1/faq） */
export async function createManualFaq(
  orgId: string,
  input: {
    question: string;
    answer: string;
    evidence: string;
    maintainer: string;
    expires_at: string;
    space_id?: string | null;
  },
): Promise<PublishedFaqItem> {
  const created = await apiClient.post<{ id: string }>(
    '/api/v1/faq',
    {
      ...input,
      source_type: 'MANUAL',
      is_manual: true,
    },
    {
      organizationId: orgId,
      idempotencyKey: createIdempotencyKey('faq-manual'),
    },
  );
  return {
    id: created.id,
    organizationId: orgId,
    spaceId: input.space_id ?? null,
    question: input.question,
    answerSummary: input.answer,
    versionNo: 1,
    lifecycleState: 'PENDING_REVIEW',
    publicationState: 'UNPUBLISHED',
    sourceHealthState: 'HEALTHY',
    isManual: true,
    maintainer: input.maintainer,
    expiresAt: input.expires_at,
  };
}
