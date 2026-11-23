import { ApiError, apiClient } from '@/lib/api-client';
import type { SensitivityLevel } from '@/types/domain';

/** 文档逻辑状态枚举 */
export type DocumentLifecycleStatus = 'QUARANTINED' | 'ACTIVE' | 'ARCHIVED';

/** 文档台账条目 */
export interface DocumentLedgerItem {
  id: string;
  organizationId: string;
  spaceId: string;
  spaceName?: string;
  title: string;
  sourceType: string;
  status: DocumentLifecycleStatus;
  currentVersionId: string | null;
  currentVersionNo: number;
  sensitivityLevel: SensitivityLevel;
  aclConfigured?: boolean;
  latestParseStatus?: string | null;
  createdAt?: string | null;
  updatedAt?: string | null;
}

/** 文档不可变版本记录 */
export interface DocumentVersionItem {
  id: string;
  documentId: string;
  versionNo: number;
  filename: string;
  mimeType: string;
  sizeBytes: number;
  fileSha256: string;
  createdAt?: string | null;
}

/** 单个子阶段状态详情 */
export interface PipelineStageDetail {
  stage: 'import' | 'parse' | 'chunking' | 'embedding' | 'index';
  label: string;
  status: string;
  provider?: string | null;
  externalTaskId?: string | null;
  errorCode?: string | null;
  errorDetail?: string | null;
  updatedAt?: string | null;
}

/** 解析结构块预览 */
export interface ParsedBlockPreview {
  id: string;
  blockType: string;
  pageNumber?: number | null;
  headingPath?: string | null;
  text: string;
}

/** Parent-Child Chunk 预览 */
export interface ChunkPreviewItem {
  id: string;
  parentChunkId?: string | null;
  tokenCount: number;
  pageRange?: string | null;
  contentText: string;
}

/** 文档版本五阶段真实处理状态响应 */
export interface VersionProcessingStatus {
  documentId?: string;
  versionId: string;
  stages: PipelineStageDetail[];
  blocks: ParsedBlockPreview[];
  chunks: ChunkPreviewItem[];
  canPublish?: boolean;
  aclConfigured?: boolean;
}

type ListEnvelope<T> = T[] | { items?: T[]; data?: T[]; total?: number };

function unwrapList<T>(payload: ListEnvelope<T>): { items: T[]; total: number } {
  if (Array.isArray(payload)) {
    return { items: payload, total: payload.length };
  }
  const items = Array.isArray(payload.items)
    ? payload.items
    : Array.isArray(payload.data)
      ? payload.data
      : [];
  return { items, total: payload.total ?? items.length };
}

interface RawDocumentRecord {
  id: string;
  organization_id?: string;
  organizationId?: string;
  space_id?: string;
  spaceId?: string;
  space_name?: string;
  spaceName?: string;
  title: string;
  source_type?: string;
  sourceType?: string;
  status?: string;
  current_version_id?: string | null;
  currentVersionId?: string | null;
  current_version_no?: number;
  currentVersionNo?: number;
  sensitivity_level?: string;
  sensitivityLevel?: string;
  acl_configured?: boolean;
  aclConfigured?: boolean;
  latest_parse_status?: string | null;
  latestParseStatus?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

interface RawVersionRecord {
  id: string;
  document_id?: string;
  documentId?: string;
  version_no?: number;
  versionNo?: number;
  filename?: string;
  mime_type?: string;
  mimeType?: string;
  size_bytes?: number;
  sizeBytes?: number;
  file_sha256?: string;
  fileSha256?: string;
  created_at?: string | null;
}

/** 生成幂等键（用于文档上传确认、批次导入与发布写操作） */
export function createIdempotencyKey(prefix: string): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return `${prefix}-${crypto.randomUUID()}`;
  }
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

/** 格式化文档中心接口错误信息 */
export function formatDocumentError(error: unknown, fallback = '文档操作失败'): string {
  if (error instanceof ApiError) {
    if (error.status === 403) {
      return `无权执行此操作（HTTP 403）：${error.message || '缺少文档管理或访问权限'}`;
    }
    if (error.status === 409 || error.status === 422) {
      return `前置条件未满足（HTTP ${error.status}）：${error.message || '文档尚未完成 ACL 配置或解析校验'}`;
    }
    return `${error.message || fallback}（${error.code}）`;
  }
  return error instanceof Error ? error.message : fallback;
}

function normalizeDocument(raw: RawDocumentRecord, fallbackOrgId: string): DocumentLedgerItem {
  const rawStatus = (raw.status ?? 'QUARANTINED').toUpperCase();
  const status: DocumentLifecycleStatus =
    rawStatus === 'ACTIVE' || rawStatus === 'ARCHIVED' ? rawStatus : 'QUARANTINED';
  const rawSens = (raw.sensitivityLevel ?? raw.sensitivity_level ?? 'L1').toUpperCase();
  const sensitivityLevel: SensitivityLevel =
    rawSens === 'L0' || rawSens === 'L2' || rawSens === 'L3' ? rawSens : 'L1';

  return {
    id: raw.id,
    organizationId: raw.organizationId ?? raw.organization_id ?? fallbackOrgId,
    spaceId: raw.spaceId ?? raw.space_id ?? '',
    spaceName: raw.spaceName ?? raw.space_name,
    title: raw.title,
    sourceType: raw.sourceType ?? raw.source_type ?? 'UPLOAD',
    status,
    currentVersionId: raw.currentVersionId ?? raw.current_version_id ?? null,
    currentVersionNo: raw.currentVersionNo ?? raw.current_version_no ?? 1,
    sensitivityLevel,
    aclConfigured: raw.aclConfigured ?? raw.acl_configured,
    latestParseStatus: raw.latestParseStatus ?? raw.latest_parse_status ?? null,
    createdAt: raw.created_at ?? null,
    updatedAt: raw.updated_at ?? null,
  };
}

/** 查询文档台账列表（GET /api/v1/documents） */
export async function fetchDocuments(
  orgId: string,
  params: {
    spaceId?: string;
    status?: string;
    sensitivityLevel?: string;
    search?: string;
    sortBy?: string;
    sortOrder?: 'asc' | 'desc';
    offset?: number;
    limit?: number;
  } = {},
): Promise<{ items: DocumentLedgerItem[]; total: number }> {
  const qs = new URLSearchParams();
  if (params.spaceId && params.spaceId !== 'ALL') qs.set('space_id', params.spaceId);
  if (params.status && params.status !== 'ALL') qs.set('status', params.status);
  if (params.sensitivityLevel && params.sensitivityLevel !== 'ALL') {
    qs.set('sensitivity_level', params.sensitivityLevel);
  }
  if (params.search?.trim()) qs.set('search', params.search.trim());
  if (params.sortBy) qs.set('sort_by', params.sortBy);
  if (params.sortOrder) qs.set('sort_order', params.sortOrder);
  if (params.offset !== undefined) qs.set('offset', String(params.offset));
  if (params.limit !== undefined) qs.set('limit', String(params.limit));

  const queryStr = qs.toString();
  const payload = await apiClient.get<ListEnvelope<RawDocumentRecord>>(
    `/api/v1/documents${queryStr ? `?${queryStr}` : ''}`,
    { organizationId: orgId },
  );
  const { items, total } = unwrapList(payload);
  return {
    items: items.map((item) => normalizeDocument(item, orgId)),
    total,
  };
}

/**
 * 单文件上传：先创建上传会话（/spaces/{space_id}/documents/uploads），
 * 若后端返回预签名 URL 则直传对象存储后调用 /complete，否则直接由 Multipart/FormData 代理上传。
 */
export async function uploadSingleDocument(
  orgId: string,
  spaceId: string,
  file: File,
  sensitivityLevel: SensitivityLevel,
): Promise<{ documentId?: string; versionId?: string; status?: string }> {
  const formData = new FormData();
  formData.append('file', file);
  formData.append('title', file.name);
  formData.append('sensitivity_level', sensitivityLevel);

  const session = await apiClient.post<{
    upload_id?: string;
    document_id?: string;
    version_id?: string;
    presigned_url?: string;
    status?: string;
  }>(`/api/v1/spaces/${encodeURIComponent(spaceId)}/documents/uploads`, formData, {
    organizationId: orgId,
    idempotencyKey: createIdempotencyKey('doc-upload'),
  });

  if (session.upload_id) {
    if (session.presigned_url) {
      await fetch(session.presigned_url, {
        method: 'PUT',
        body: file,
        headers: { 'Content-Type': file.type || 'application/octet-stream' },
      });
    }
    const completed = await apiClient.post<{
      document_id?: string;
      version_id?: string;
      status?: string;
    }>(
      `/api/v1/documents/uploads/${encodeURIComponent(session.upload_id)}/complete`,
      { filename: file.name, size_bytes: file.size },
      {
        organizationId: orgId,
        idempotencyKey: createIdempotencyKey('doc-complete'),
      },
    );
    return {
      documentId: completed.document_id ?? session.document_id,
      versionId: completed.version_id ?? session.version_id,
      status: completed.status ?? 'QUARANTINED',
    };
  }

  return {
    documentId: session.document_id,
    versionId: session.version_id,
    status: session.status ?? 'QUARANTINED',
  };
}

/** 批量文件/文件夹导入（POST /api/v1/spaces/{space_id}/documents/import-batches） */
export async function createImportBatch(
  orgId: string,
  spaceId: string,
  files: File[],
  sensitivityLevel: SensitivityLevel,
): Promise<{ batchId?: string; acceptedCount: number }> {
  const formData = new FormData();
  formData.append('sensitivity_level', sensitivityLevel);
  for (const file of files) {
    formData.append('files', file, file.webkitRelativePath || file.name);
  }
  const result = await apiClient.post<{ batch_id?: string; accepted_count?: number }>(
    `/api/v1/spaces/${encodeURIComponent(spaceId)}/documents/import-batches`,
    formData,
    {
      organizationId: orgId,
      idempotencyKey: createIdempotencyKey('doc-batch'),
    },
  );
  return {
    batchId: result.batch_id,
    acceptedCount: result.accepted_count ?? files.length,
  };
}

/** 获取文档不可变版本列表（GET /api/v1/documents/{document_id}/versions） */
export async function fetchDocumentVersions(
  orgId: string,
  documentId: string,
): Promise<DocumentVersionItem[]> {
  const payload = await apiClient.get<ListEnvelope<RawVersionRecord>>(
    `/api/v1/documents/${encodeURIComponent(documentId)}/versions`,
    { organizationId: orgId },
  );
  return unwrapList(payload).items.map((raw) => ({
    id: raw.id,
    documentId: raw.documentId ?? raw.document_id ?? documentId,
    versionNo: raw.versionNo ?? raw.version_no ?? 1,
    filename: raw.filename ?? '未命名文件',
    mimeType: raw.mimeType ?? raw.mime_type ?? 'application/octet-stream',
    sizeBytes: raw.sizeBytes ?? raw.size_bytes ?? 0,
    fileSha256: raw.fileSha256 ?? raw.file_sha256 ?? '—',
    createdAt: raw.created_at ?? null,
  }));
}

/** 获取文档版本五阶段真实处理状态与切片预览（GET /api/v1/document-versions/{version_id}/processing-status） */
export async function fetchVersionProcessingStatus(
  orgId: string,
  versionId: string,
): Promise<VersionProcessingStatus> {
  interface RawStageObj {
    status?: string;
    provider?: string | null;
    parser_provider?: string | null;
    external_task_id?: string | null;
    error_code?: string | null;
    error_detail?: string | null;
    updated_at?: string | null;
  }

  interface RawProcessingResponse {
    document_id?: string;
    version_id?: string;
    import_job?: RawStageObj | null;
    parse_job?: RawStageObj | null;
    chunking_job?: RawStageObj | null;
    embedding_job?: RawStageObj | null;
    index_job?: RawStageObj | null;
    stages?: Array<RawStageObj & { stage: string; label?: string }>;
    blocks?: Array<{
      id: string;
      block_type?: string;
      blockType?: string;
      page_number?: number | null;
      heading_path?: string | null;
      text?: string;
      content_text?: string;
    }>;
    chunks?: Array<{
      id: string;
      parent_chunk_id?: string | null;
      token_count?: number;
      page_range?: string | null;
      content_text?: string;
      text?: string;
    }>;
    can_publish?: boolean;
    acl_configured?: boolean;
  }

  const raw = await apiClient.get<RawProcessingResponse>(
    `/api/v1/document-versions/${encodeURIComponent(versionId)}/processing-status`,
    { organizationId: orgId },
  );

  const toStage = (
    stage: PipelineStageDetail['stage'],
    label: string,
    obj?: RawStageObj | null,
  ): PipelineStageDetail => ({
    stage,
    label,
    status: (obj?.status ?? 'PENDING').toUpperCase(),
    provider: obj?.provider ?? obj?.parser_provider ?? null,
    externalTaskId: obj?.external_task_id ?? null,
    errorCode: obj?.error_code ?? null,
    errorDetail: obj?.error_detail ?? null,
    updatedAt: obj?.updated_at ?? null,
  });

  const stages: PipelineStageDetail[] = Array.isArray(raw.stages)
    ? raw.stages.map((s) =>
        toStage(s.stage as PipelineStageDetail['stage'], s.label ?? s.stage, s),
      )
    : [
        toStage('import', 'ImportJob（上传与校验）', raw.import_job),
        toStage('parse', 'ParseJob（MinerU / 轻量解析）', raw.parse_job),
        toStage('chunking', 'ChunkingJob（Parent-Child 切片）', raw.chunking_job),
        toStage('embedding', 'EmbeddingJob（向量化）', raw.embedding_job),
        toStage('index', 'IndexJob（Qdrant 索引构建）', raw.index_job),
      ];

  return {
    documentId: raw.document_id,
    versionId: raw.version_id ?? versionId,
    stages,
    blocks: (raw.blocks ?? []).map((b) => ({
      id: b.id,
      blockType: b.blockType ?? b.block_type ?? 'paragraph',
      pageNumber: b.page_number ?? null,
      headingPath: b.heading_path ?? null,
      text: b.text ?? b.content_text ?? '',
    })),
    chunks: (raw.chunks ?? []).map((c) => ({
      id: c.id,
      parentChunkId: c.parent_chunk_id ?? null,
      tokenCount: c.token_count ?? 0,
      pageRange: c.page_range ?? null,
      contentText: c.content_text ?? c.text ?? '',
    })),
    canPublish: raw.can_publish,
    aclConfigured: raw.acl_configured,
  };
}

/** 触发文档版本重新解析/切片/索引（POST /api/v1/document-versions/{version_id}/reprocess） */
export async function reprocessDocumentVersion(
  orgId: string,
  versionId: string,
  fromStage: 'parse' | 'chunking' | 'embedding' | 'index' = 'parse',
): Promise<void> {
  await apiClient.post<unknown>(
    `/api/v1/document-versions/${encodeURIComponent(versionId)}/reprocess`,
    { from_stage: fromStage },
    {
      organizationId: orgId,
      idempotencyKey: createIdempotencyKey('doc-reprocess'),
    },
  );
}

/** 发布文档版本并解除 Quarantine（POST /api/v1/document-versions/{version_id}/publish） */
export async function publishDocumentVersion(
  orgId: string,
  versionId: string,
): Promise<void> {
  await apiClient.post<unknown>(
    `/api/v1/document-versions/${encodeURIComponent(versionId)}/publish`,
    {},
    {
      organizationId: orgId,
      idempotencyKey: createIdempotencyKey('doc-publish'),
    },
  );
}
