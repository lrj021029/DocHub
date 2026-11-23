import { ApiError, apiClient } from '@/lib/api-client';
import { createIdempotencyKey } from '@/features/documents/api';
import type { CitationData } from '@/lib/sse-client';

/** 会话列表摘要 */
export interface ConversationSummary {
  id: string;
  organizationId: string;
  title: string;
  spaceIds: string[];
  status: string;
  createdAt?: string | null;
  updatedAt?: string | null;
}

/** 回答段落模型（支持历史会话按当前权限动态重鉴权） */
export interface MessageSegmentItem {
  id: string;
  sequence: number;
  segmentType: string;
  content: string;
  /** 若为 true，说明当前用户对该来源已无权限或无法拆分的回答已被整体隐藏 */
  redacted: boolean;
  redactedReason?: string | null;
}

/** 会话消息条目 */
export interface ChatMessageItem {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  status?: string;
  redacted?: boolean;
  segments: MessageSegmentItem[];
  citations: CitationData[];
  feedback?: 'up' | 'down' | null;
  createdAt?: string | null;
}

/** 会话详情（含动态重鉴权后的历史消息与选定 Space） */
export interface ConversationDetail {
  id: string;
  title: string;
  spaceIds: string[];
  messages: ChatMessageItem[];
}

/** 引用详情与 PDF 页面定位（GET /api/v1/citations/{citation_id} 动态重鉴权返回） */
export interface CitationDetail {
  citationId: string;
  documentId?: string;
  documentVersionId?: string;
  documentTitle: string;
  pageNumber?: number | null;
  headingPath?: string | null;
  quoteText: string;
  pageImageUrl?: string | null;
  bbox?: { x: number; y: number; width: number; height: number } | [number, number, number, number] | null;
  aclRevision?: number | null;
  authorized: boolean;
  deniedReason?: string | null;
}

type ListEnvelope<T> = T[] | { items?: T[]; data?: T[] };

function unwrapList<T>(payload: ListEnvelope<T>): T[] {
  if (Array.isArray(payload)) return payload;
  if (Array.isArray(payload.items)) return payload.items;
  if (Array.isArray(payload.data)) return payload.data;
  return [];
}

/** 格式化问答错误文案 */
export function formatChatError(error: unknown, fallback = '问答请求失败'): string {
  if (error instanceof ApiError) {
    if (error.status === 403) {
      return `权限不足（HTTP 403）：${error.message || '当前用户已无权访问该资源，系统已拦截展示'}`;
    }
    return `${error.message || fallback}（${error.code}）`;
  }
  return error instanceof Error ? error.message : fallback;
}

interface RawConversation {
  id: string;
  organization_id?: string;
  title?: string;
  space_ids?: string[];
  status?: string;
  created_at?: string | null;
  updated_at?: string | null;
}

/** 获取历史会话列表（GET /api/v1/chat/conversations） */
export async function fetchConversations(orgId: string): Promise<ConversationSummary[]> {
  const payload = await apiClient.get<ListEnvelope<RawConversation>>('/api/v1/chat/conversations', {
    organizationId: orgId,
  });
  return unwrapList(payload).map((c) => ({
    id: c.id,
    organizationId: c.organization_id ?? orgId,
    title: c.title || '新知识问答会话',
    spaceIds: c.space_ids ?? [],
    status: c.status ?? 'ACTIVE',
    createdAt: c.created_at ?? null,
    updatedAt: c.updated_at ?? null,
  }));
}

/** 创建新会话（POST /api/v1/chat/conversations） */
export async function createConversation(
  orgId: string,
  input: { title?: string; space_ids: string[] },
): Promise<ConversationSummary> {
  const raw = await apiClient.post<RawConversation>('/api/v1/chat/conversations', input, {
    organizationId: orgId,
    idempotencyKey: createIdempotencyKey('chat-conv'),
  });
  return {
    id: raw.id,
    organizationId: raw.organization_id ?? orgId,
    title: raw.title || input.title || '新知识问答会话',
    spaceIds: raw.space_ids ?? input.space_ids,
    status: raw.status ?? 'ACTIVE',
    createdAt: raw.created_at ?? null,
  };
}

/** 获取会话详情并触发历史消息动态重鉴权（GET /api/v1/chat/conversations/{id}） */
export async function fetchConversationDetail(
  orgId: string,
  conversationId: string,
): Promise<ConversationDetail> {
  interface RawSegment {
    id?: string;
    sequence?: number;
    segment_type?: string;
    content?: string;
    redacted?: boolean;
    redacted_reason?: string | null;
  }
  interface RawCitation {
    citation_id?: string;
    id?: string;
    document_id?: string;
    document_version_id?: string;
    document_title?: string;
    page?: number;
    page_number?: number;
    chunk_id?: string;
    quote?: string;
  }
  interface RawMessage {
    id: string;
    role?: string;
    content?: string;
    status?: string;
    redacted?: boolean;
    segments?: RawSegment[];
    citations?: RawCitation[];
    feedback?: string | null;
    created_at?: string | null;
  }
  interface RawDetail {
    id: string;
    title?: string;
    space_ids?: string[];
    messages?: RawMessage[];
  }

  const raw = await apiClient.get<RawDetail>(
    `/api/v1/chat/conversations/${encodeURIComponent(conversationId)}`,
    { organizationId: orgId },
  );

  return {
    id: raw.id,
    title: raw.title || '知识问答会话',
    spaceIds: raw.space_ids ?? [],
    messages: (raw.messages ?? []).map((m) => ({
      id: m.id,
      role: m.role === 'user' || m.role === 'system' ? m.role : 'assistant',
      content: m.content ?? '',
      status: m.status,
      redacted: Boolean(m.redacted),
      segments: (m.segments ?? []).map((seg, idx) => ({
        id: seg.id ?? `${m.id}-seg-${idx}`,
        sequence: seg.sequence ?? idx,
        segmentType: seg.segment_type ?? 'TEXT',
        content: seg.content ?? '',
        redacted: Boolean(seg.redacted),
        redactedReason: seg.redacted_reason ?? null,
      })),
      citations: (m.citations ?? []).map((c) => ({
        citation_id: c.citation_id ?? c.id ?? '',
        document_id: c.document_id,
        document_version_id: c.document_version_id,
        document_title: c.document_title ?? '授权文档引用',
        page: c.page ?? c.page_number,
        chunk_id: c.chunk_id,
        quote: c.quote,
      })),
      feedback: m.feedback === 'up' || m.feedback === 'down' ? m.feedback : null,
      createdAt: m.created_at ?? null,
    })),
  };
}

/** 取消正在生成的回答（POST /api/v1/chat/messages/{id}/cancel） */
export async function cancelChatMessage(orgId: string, messageId: string): Promise<void> {
  await apiClient.post<unknown>(
    `/api/v1/chat/messages/${encodeURIComponent(messageId)}/cancel`,
    {},
    { organizationId: orgId },
  );
}

/** 删除当前用户的指定历史会话（DELETE /api/v1/chat/conversations/{id}，软删除保留审计证据） */
export async function deleteConversation(orgId: string, conversationId: string): Promise<void> {
  await apiClient.delete<unknown>(
    `/api/v1/chat/conversations/${encodeURIComponent(conversationId)}`,
    { organizationId: orgId },
  );
}

/** 提交回答有用/无用反馈（POST /api/v1/chat/messages/{id}/feedback） */
export async function submitChatFeedback(
  orgId: string,
  messageId: string,
  rating: 'up' | 'down',
  comment?: string,
): Promise<void> {
  await apiClient.post<unknown>(
    `/api/v1/chat/messages/${encodeURIComponent(messageId)}/feedback`,
    { rating, comment },
    { organizationId: orgId },
  );
}

/**
 * 点击引用卡片时实时向后端重新鉴权并获取原页定位/Bounding Box（GET /api/v1/citations/{citation_id}）。
 */
export async function fetchCitationDetail(
  orgId: string,
  citationId: string,
): Promise<CitationDetail> {
  interface RawCitationDetail {
    citation_id?: string;
    id?: string;
    document_id?: string;
    document_version_id?: string;
    document_title?: string;
    page_number?: number | null;
    page?: number | null;
    heading_path?: string | null;
    quote_text?: string;
    quote?: string;
    page_image_url?: string | null;
    bbox_json?: CitationDetail['bbox'];
    bbox?: CitationDetail['bbox'];
    acl_revision?: number | null;
    authorized?: boolean;
    denied_reason?: string | null;
  }

  try {
    const raw = await apiClient.get<RawCitationDetail>(
      `/api/v1/citations/${encodeURIComponent(citationId)}`,
      { organizationId: orgId },
    );
    return {
      citationId: raw.citation_id ?? raw.id ?? citationId,
      documentId: raw.document_id,
      documentVersionId: raw.document_version_id,
      documentTitle: raw.document_title ?? '引用文档',
      pageNumber: raw.page_number ?? raw.page ?? null,
      headingPath: raw.heading_path ?? null,
      quoteText: raw.quote_text ?? raw.quote ?? '',
      pageImageUrl: raw.page_image_url ?? null,
      bbox: raw.bbox ?? raw.bbox_json ?? null,
      aclRevision: raw.acl_revision ?? null,
      authorized: raw.authorized !== false,
      deniedReason: raw.denied_reason ?? null,
    };
  } catch (error) {
    if (error instanceof ApiError && error.status === 403) {
      return {
        citationId,
        documentTitle: '受限引用（权限已撤销）',
        quoteText: '',
        authorized: false,
        deniedReason: error.message || '您当前已不再具备该引用来源文档的访问权限。',
      };
    }
    throw error;
  }
}
