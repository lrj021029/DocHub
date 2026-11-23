import { ApiError, apiClient } from '@/lib/api-client';
import { createIdempotencyKey } from '@/features/documents/api';

/** 总览运营指标（GET /api/v1/analytics/overview） */
export interface AnalyticsOverview {
  pv: number;
  uv: number;
  activeConversations: number;
  authorizedSpaces: number;
  activeDocuments: number;
  activeChunks: number;
  faqHitRate: number;
  faqDirectAnswerRate: number;
  staleFaqRatio: number;
  openGapCount: number;
  gapResolutionRate: number;
  p95FirstTokenMs: number | null;
  p95TotalLatencyMs: number | null;
  totalTokens: number;
}

/** FAQ 运营细分指标（GET /api/v1/analytics/faq） */
export interface FaqAnalyticsMetrics {
  candidateCount: number;
  publishedCount: number;
  staleCount: number;
  hitRate: number;
  helpfulRate: number;
}

/** Knowledge Gap 运营细分指标（GET /api/v1/analytics/knowledge-gaps） */
export interface GapAnalyticsMetrics {
  totalGaps: number;
  resolvedGaps: number;
  resolutionRate: number;
  avgResolutionHours: number | null;
}

/** 业务/安全审计分页事件 */
export interface AuditEventItem {
  id: string;
  eventType: string;
  actorEmail: string;
  resourceSummary: string;
  severity?: string;
  reason?: string | null;
  requestId?: string | null;
  createdAt: string;
}

export interface AuditPageResult {
  items: AuditEventItem[];
  nextCursor: string | null;
}

/** 系统 Provider 与外发策略脱敏配置（严禁包含任何明文 API Key） */
export interface ProviderSystemConfig {
  llm: {
    providerType: string;
    baseUrl: string;
    model: string;
    hasApiKeyConfigured: boolean;
  };
  embedding: {
    primaryProvider: 'QWEN_API' | 'LOCAL_BGE' | string;
    qwenModel: string;
    localBgeEnabled: boolean;
    localBgeEndpoint: string;
  };
  reranker: {
    primaryProvider: 'QWEN_API' | 'LOCAL_BGE' | string;
    model: string;
    localBgeEnabled: boolean;
  };
  parser: {
    mineruBaseUrl: string;
    mineruApiVersion: string;
    hasMineruKeyConfigured: boolean;
    localOcrEnabled: boolean;
  };
  egressMatrix: Array<{
    level: 'L0' | 'L1' | 'L2' | 'L3';
    label: string;
    allowExternalParse: boolean;
    allowExternalLlm: boolean;
    note: string;
  }>;
}

/** Benchmark 运行记录 */
export interface BenchmarkRunItem {
  id: string;
  suiteType: string;
  status: string;
  recallAtK: number | null;
  mrr: number | null;
  citationAccuracy: number | null;
  aclViolations: number;
  unauthorizedPromptChunks: number;
  crossOrgLeaks: number;
  createdAt: string;
}

export function formatGovError(error: unknown, fallback = '请求失败'): string {
  if (error instanceof ApiError) {
    if (error.status === 403) {
      return `权限不足（HTTP 403）：${error.message || '当前账号无权查看该治理视图'}`;
    }
    return `${error.message || fallback}（${error.code}）`;
  }
  return error instanceof Error ? error.message : fallback;
}

/** 获取总览指标（GET /api/v1/analytics/overview） */
export async function fetchAnalyticsOverview(orgId: string): Promise<AnalyticsOverview> {
  const raw = await apiClient.get<Record<string, unknown>>('/api/v1/analytics/overview', {
    organizationId: orgId,
  });
  const num = (k1: string, k2?: string): number => {
    const val = raw[k1] ?? (k2 ? raw[k2] : undefined);
    return typeof val === 'number' ? val : 0;
  };
  const optNum = (k1: string, k2?: string): number | null => {
    const val = raw[k1] ?? (k2 ? raw[k2] : undefined);
    return typeof val === 'number' ? val : null;
  };

  return {
    pv: num('pv', 'total_pv'),
    uv: num('uv', 'total_uv'),
    activeConversations: num('active_conversations', 'activeConversations'),
    authorizedSpaces: num('authorized_spaces', 'space_count'),
    activeDocuments: num('active_documents', 'document_count'),
    activeChunks: num('active_chunks', 'chunk_count'),
    faqHitRate: num('faq_hit_rate', 'faqHitRate'),
    faqDirectAnswerRate: num('faq_direct_answer_rate', 'faqDirectAnswerRate'),
    staleFaqRatio: num('stale_faq_ratio', 'staleFaqRatio'),
    openGapCount: num('open_gap_count', 'openGapCount'),
    gapResolutionRate: num('gap_resolution_rate', 'gapResolutionRate'),
    p95FirstTokenMs: optNum('p95_first_token_ms', 'p95FirstTokenMs'),
    p95TotalLatencyMs: optNum('p95_total_latency_ms', 'p95TotalLatencyMs'),
    totalTokens: num('total_tokens', 'totalTokens'),
  };
}

/** 获取 FAQ 指标（GET /api/v1/analytics/faq） */
export async function fetchFaqAnalytics(orgId: string): Promise<FaqAnalyticsMetrics> {
  const raw = await apiClient.get<Record<string, number>>('/api/v1/analytics/faq', {
    organizationId: orgId,
  });
  return {
    candidateCount: raw.candidate_count ?? 0,
    publishedCount: raw.published_count ?? 0,
    staleCount: raw.stale_count ?? 0,
    hitRate: raw.hit_rate ?? 0,
    helpfulRate: raw.helpful_rate ?? 0,
  };
}

/** 获取 Knowledge Gap 指标（GET /api/v1/analytics/knowledge-gaps） */
export async function fetchGapAnalytics(orgId: string): Promise<GapAnalyticsMetrics> {
  const raw = await apiClient.get<Record<string, number | null>>(
    '/api/v1/analytics/knowledge-gaps',
    { organizationId: orgId },
  );
  return {
    totalGaps: Number(raw.total_gaps ?? 0),
    resolvedGaps: Number(raw.resolved_gaps ?? 0),
    resolutionRate: Number(raw.resolution_rate ?? 0),
    avgResolutionHours: raw.avg_resolution_hours ?? null,
  };
}

/** 查询业务审计或安全审计流（Cursor 分页） */
export async function fetchAuditEvents(
  orgId: string,
  kind: 'business' | 'security',
  cursor?: string | null,
): Promise<AuditPageResult> {
  const endpoint =
    kind === 'security' ? '/api/v1/audit/security-events' : '/api/v1/audit/events';
  const qs = new URLSearchParams();
  if (cursor) qs.set('cursor', cursor);
  qs.set('limit', '20');

  interface RawAuditRow {
    id: string;
    event_type?: string;
    action?: string;
    actor_email?: string;
    actor_id?: string;
    resource_summary?: string;
    resource_type?: string;
    resource_id?: string;
    severity?: string;
    reason?: string | null;
    request_id?: string | null;
    created_at?: string;
  }
  type Envelope =
    | RawAuditRow[]
    | { items?: RawAuditRow[]; data?: RawAuditRow[]; next_cursor?: string | null };

  const payload = await apiClient.get<Envelope>(`${endpoint}?${qs.toString()}`, {
    organizationId: orgId,
  });

  const rows = Array.isArray(payload)
    ? payload
    : Array.isArray(payload.items)
      ? payload.items
      : Array.isArray(payload.data)
        ? payload.data
        : [];
  const nextCursor = Array.isArray(payload) ? null : (payload.next_cursor ?? null);

  return {
    nextCursor,
    items: rows.map((r) => ({
      id: r.id,
      eventType: r.event_type ?? r.action ?? 'AUDIT_EVENT',
      actorEmail: r.actor_email ?? r.actor_id ?? 'system',
      resourceSummary:
        r.resource_summary ??
        [r.resource_type, r.resource_id].filter(Boolean).join(':') ??
        '—',
      severity: r.severity,
      reason: r.reason ?? null,
      requestId: r.request_id ?? null,
      createdAt: r.created_at ?? '',
    })),
  };
}

/** 获取系统 Provider 与外发策略脱敏配置（GET /api/v1/providers/config） */
export async function fetchProviderConfig(orgId: string): Promise<ProviderSystemConfig> {
  const raw = await apiClient.get<Partial<ProviderSystemConfig> & Record<string, unknown>>(
    '/api/v1/providers/config',
    { organizationId: orgId },
  );

  // 防御性过滤：绝不解析或保留任何明文 *_api_key / secret 字段
  const llmRaw = (raw.llm ?? {}) as Record<string, unknown>;
  const embRaw = (raw.embedding ?? {}) as Record<string, unknown>;
  const rerankRaw = (raw.reranker ?? {}) as Record<string, unknown>;
  const parserRaw = (raw.parser ?? {}) as Record<string, unknown>;

  return {
    llm: {
      providerType: String(llmRaw.provider_type ?? llmRaw.providerType ?? 'OpenAICompatibleLLMProvider'),
      baseUrl: String(llmRaw.base_url ?? llmRaw.baseUrl ?? 'https://100milionseek.top/v1'),
      model: String(llmRaw.model ?? 'configured-on-backend'),
      hasApiKeyConfigured: Boolean(
        llmRaw.has_api_key_configured ?? llmRaw.hasApiKeyConfigured ?? true,
      ),
    },
    embedding: {
      primaryProvider: String(embRaw.primary_provider ?? embRaw.primaryProvider ?? 'QWEN_API'),
      qwenModel: String(embRaw.qwen_model ?? embRaw.qwenModel ?? 'text-embedding-v3'),
      localBgeEnabled: Boolean(embRaw.local_bge_enabled ?? embRaw.localBgeEnabled ?? false),
      localBgeEndpoint: String(embRaw.local_bge_endpoint ?? embRaw.localBgeEndpoint ?? '未启用'),
    },
    reranker: {
      primaryProvider: String(rerankRaw.primary_provider ?? rerankRaw.primaryProvider ?? 'QWEN_API'),
      model: String(rerankRaw.model ?? 'gte-rerank-v2'),
      localBgeEnabled: Boolean(rerankRaw.local_bge_enabled ?? rerankRaw.localBgeEnabled ?? false),
    },
    parser: {
      mineruBaseUrl: String(parserRaw.mineru_base_url ?? parserRaw.mineruBaseUrl ?? '官方 MinerU API'),
      mineruApiVersion: String(parserRaw.mineru_api_version ?? parserRaw.mineruApiVersion ?? 'v4'),
      hasMineruKeyConfigured: Boolean(
        parserRaw.has_mineru_key_configured ?? parserRaw.hasMineruKeyConfigured ?? true,
      ),
      localOcrEnabled: Boolean(parserRaw.local_ocr_enabled ?? parserRaw.localOcrEnabled ?? false),
    },
    egressMatrix: Array.isArray(raw.egressMatrix)
      ? raw.egressMatrix
      : [
          {
            level: 'L0',
            label: 'Public 公开',
            allowExternalParse: true,
            allowExternalLlm: true,
            note: '允许外部 MinerU / Qwen / LLM Provider',
          },
          {
            level: 'L1',
            label: 'Internal 内部',
            allowExternalParse: true,
            allowExternalLlm: true,
            note: '需当前 Organization 明确授权后允许外发',
          },
          {
            level: 'L2',
            label: 'Confidential 机密',
            allowExternalParse: false,
            allowExternalLlm: false,
            note: '默认本地处理（Local BGE / Local OCR），未启用则 Fail-Closed',
          },
          {
            level: 'L3',
            label: 'Restricted 受限',
            allowExternalParse: false,
            allowExternalLlm: false,
            note: '严格禁止任何外部模型与解析服务外发',
          },
        ],
  };
}

/** 更新 Provider 路由与本地开关（PATCH /api/v1/providers/config，不传任何 Secret） */
export async function updateProviderConfig(
  orgId: string,
  input: {
    embedding_primary_provider?: string;
    local_bge_enabled?: boolean;
    local_ocr_enabled?: boolean;
    llm_base_url?: string;
    llm_model?: string;
  },
): Promise<void> {
  await apiClient.patch<unknown>('/api/v1/providers/config', input, {
    organizationId: orgId,
  });
}

/** 查询 Benchmark 运行列表（GET /api/v1/benchmarks） */
export async function fetchBenchmarks(orgId: string): Promise<BenchmarkRunItem[]> {
  interface RawRun {
    id: string;
    suite_type?: string;
    status?: string;
    recall_at_k?: number | null;
    mrr?: number | null;
    citation_accuracy?: number | null;
    acl_violations?: number;
    unauthorized_prompt_chunks?: number;
    cross_org_leaks?: number;
    created_at?: string;
  }
  type Envelope = RawRun[] | { items?: RawRun[]; data?: RawRun[] };
  const payload = await apiClient.get<Envelope>('/api/v1/benchmarks', {
    organizationId: orgId,
  });
  const list = Array.isArray(payload)
    ? payload
    : Array.isArray(payload.items)
      ? payload.items
      : Array.isArray(payload.data)
        ? payload.data
        : [];

  return list.map((r) => ({
    id: r.id,
    suiteType: r.suite_type ?? 'RAG_ACL_REGRESSION',
    status: (r.status ?? 'COMPLETED').toUpperCase(),
    recallAtK: r.recall_at_k ?? null,
    mrr: r.mrr ?? null,
    citationAccuracy: r.citation_accuracy ?? null,
    aclViolations: r.acl_violations ?? 0,
    unauthorizedPromptChunks: r.unauthorized_prompt_chunks ?? 0,
    crossOrgLeaks: r.cross_org_leaks ?? 0,
    createdAt: r.created_at ?? '',
  }));
}

/** 一键触发 Benchmark 运行（POST /api/v1/benchmarks） */
export async function triggerBenchmarkRun(
  orgId: string,
  suiteType: 'RAG_ACL_REGRESSION' | 'PARSER_REGRESSION' = 'RAG_ACL_REGRESSION',
): Promise<void> {
  await apiClient.post<unknown>(
    '/api/v1/benchmarks',
    { suite_type: suiteType },
    {
      organizationId: orgId,
      idempotencyKey: createIdempotencyKey('bench-run'),
    },
  );
}
