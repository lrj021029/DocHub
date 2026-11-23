import { getAccessToken, getCsrfToken, getCurrentOrgId } from '@/lib/auth-store';

/**
 * DocHub AI 问答 SSE 标准事件联合类型（严格对应 PRD §5.4 与 TECHNICAL_SPEC §9.7）。
 * 覆盖全部 7 类必备事件：
 * - message_start
 * - retrieval_started
 * - retrieval_completed
 * - citation
 * - token
 * - message_complete
 * - message_error
 */
export interface MessageStartData {
  message_id: string;
  conversation_id?: string;
}

export interface RetrievalStartedData {
  space_ids: string[];
}

export interface RetrievalCompletedData {
  authorized_count: number;
  latency_ms?: number;
}

export interface CitationData {
  citation_id: string;
  document_id?: string;
  document_version_id?: string;
  document_title: string;
  page?: number;
  chunk_id?: string;
  quote?: string;
}

export interface TokenData {
  text: string;
}

export interface MessageCompleteData {
  message_id?: string;
  usage?: {
    prompt_tokens: number;
    completion_tokens: number;
  };
  finish_reason?: string;
}

export interface MessageErrorData {
  code: string;
  message?: string;
  retryable: boolean;
}

export type ChatSSEEvent =
  | { event: 'message_start'; data: MessageStartData }
  | { event: 'retrieval_started'; data: RetrievalStartedData }
  | { event: 'retrieval_completed'; data: RetrievalCompletedData }
  | { event: 'citation'; data: CitationData }
  | { event: 'token'; data: TokenData }
  | { event: 'message_complete'; data: MessageCompleteData }
  | { event: 'message_error'; data: MessageErrorData };

/** 受支持的 7 类标准 SSE 事件名集合 */
export const SUPPORTED_SSE_EVENTS = new Set<ChatSSEEvent['event']>([
  'message_start',
  'retrieval_started',
  'retrieval_completed',
  'citation',
  'token',
  'message_complete',
  'message_error',
]);

/** 禁止出现在 URL Query 中的敏感凭据参数名（安全红线：严禁 URL 拼接 JWT） */
const FORBIDDEN_URL_TOKEN_PARAMS = ['token', 'access_token', 'jwt', 'authorization', 'bearer'];

/** 校验 SSE 目标 URL 不包含任何 JWT / Token 查询参数 */
export function assertNoTokenInUrl(url: string): void {
  const parsed = new URL(url, 'http://localhost');
  for (const key of parsed.searchParams.keys()) {
    if (FORBIDDEN_URL_TOKEN_PARAMS.includes(key.toLowerCase())) {
      throw new Error(`安全拦截：SSE 请求 URL 严禁携带敏感凭据参数 "${key}"`);
    }
  }
}

/** 解析单帧 SSE 文本并提取结构化事件对象 */
function parseSSEBlock(rawBlock: string): ChatSSEEvent | null {
  const lines = rawBlock.split(/\r?\n/);
  let eventName = '';
  const dataLines: string[] = [];

  for (const line of lines) {
    // 以冒号开头的行是 SSE 注释或心跳帧，直接忽略
    if (!line || line.startsWith(':')) {
      continue;
    }
    const separatorIndex = line.indexOf(':');
    const field = separatorIndex === -1 ? line : line.slice(0, separatorIndex);
    let value = separatorIndex === -1 ? '' : line.slice(separatorIndex + 1);
    if (value.startsWith(' ')) {
      value = value.slice(1);
    }

    if (field === 'event') {
      eventName = value.trim();
    } else if (field === 'data') {
      dataLines.push(value);
    }
  }

  if (dataLines.length === 0) {
    return null;
  }

  const rawData = dataLines.join('\n').trim();
  if (!rawData || rawData === '[DONE]') {
    return null;
  }

  let parsedJson: unknown;
  try {
    parsedJson = JSON.parse(rawData);
  } catch {
    return null;
  }

  // 兼容两种规范格式：
  // 1) 标准 SSE：event: token \n data: {"text":"..."}
  // 2) 封装帧：data: {"event":"token","data":{"text":"..."}}
  let resolvedEvent = eventName;
  let resolvedData: unknown = parsedJson;

  if (
    !resolvedEvent &&
    typeof parsedJson === 'object' &&
    parsedJson !== null &&
    'event' in parsedJson &&
    'data' in parsedJson
  ) {
    const envelope = parsedJson as { event: string; data: unknown };
    resolvedEvent = envelope.event;
    resolvedData = envelope.data;
  }

  if (!SUPPORTED_SSE_EVENTS.has(resolvedEvent as ChatSSEEvent['event'])) {
    return null;
  }

  return {
    event: resolvedEvent as ChatSSEEvent['event'],
    data: resolvedData,
  } as ChatSSEEvent;
}

/**
 * 增量 SSE 帧解析器，支持任意 TCP/ReadableStream 分块边界切割。
 */
export function createSSEParser(onEvent: (event: ChatSSEEvent) => void) {
  let buffer = '';

  return {
    /** 喂入新解码的文本分块，立即派发所有完整事件帧 */
    feed(chunkText: string): void {
      buffer += chunkText;
      const frames = buffer.split(/\r?\n\r?\n/);
      // 最后一段可能尚未接收到完整空行分隔符，留待下一块拼接
      buffer = frames.pop() ?? '';

      for (const frame of frames) {
        const trimmed = frame.trim();
        if (!trimmed) continue;
        const parsed = parseSSEBlock(trimmed);
        if (parsed) {
          onEvent(parsed);
        }
      }
    },

    /** 在流结束时冲刷缓冲区剩余的最后一帧 */
    flush(): void {
      const remaining = buffer.trim();
      buffer = '';
      if (!remaining) return;
      const parsed = parseSSEBlock(remaining);
      if (parsed) {
        onEvent(parsed);
      }
    },
  };
}

/**
 * 读取 ReadableStream<Uint8Array> 并逐条回调解析出的 7 类 ChatSSEEvent。
 */
export async function parseSSEStream(
  stream: ReadableStream<Uint8Array>,
  onEvent: (event: ChatSSEEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const reader = stream.getReader();
  const decoder = new TextDecoder('utf-8');
  const parser = createSSEParser(onEvent);

  try {
    while (true) {
      if (signal?.aborted) {
        await reader.cancel();
        break;
      }
      const { value, done } = await reader.read();
      if (done) {
        const tail = decoder.decode();
        if (tail) {
          parser.feed(tail);
        }
        parser.flush();
        break;
      }
      if (value) {
        parser.feed(decoder.decode(value, { stream: true }));
      }
    }
  } finally {
    reader.releaseLock();
  }
}

/** POST SSE 流式请求参数 */
export interface StreamChatOptions {
  /** 目标 SSE 端点，例如 /api/v1/chat/conversations/{id}/messages:stream */
  url: string;
  /** 发送给后端的 JSON 请求体 */
  payload: Record<string, unknown>;
  /** 收到每一条受支持 SSE 事件时的回调 */
  onEvent: (event: ChatSSEEvent) => void;
  /** 可选覆盖当前请求的 Organization ID */
  organizationId?: string | null;
  /** 可选中断信号，用于用户主动停止生成 */
  signal?: AbortSignal;
  /** 自定义 fetch 实现（用于单元测试注入） */
  fetchImpl?: typeof fetch;
}

/**
 * 基于 fetch() + ReadableStream 的 POST SSE 流式客户端。
 * 安全约束：
 * - 强制通过 Authorization: Bearer Header 传递内存 Access Token；
 * - 严禁在 URL Query 拼接任何 JWT；
 * - 携带 X-Organization-Id、X-CSRF-Token 与 credentials: 'include'。
 */
export async function streamChatSSE(options: StreamChatOptions): Promise<void> {
  assertNoTokenInUrl(options.url);

  const fetchImpl = options.fetchImpl ?? globalThis.fetch;
  const headers = new Headers({
    Accept: 'text/event-stream',
    'Content-Type': 'application/json',
  });

  const accessToken = getAccessToken();
  if (accessToken) {
    headers.set('Authorization', `Bearer ${accessToken}`);
  }

  const orgId =
    options.organizationId !== undefined ? options.organizationId : getCurrentOrgId();
  if (orgId) {
    headers.set('X-Organization-Id', orgId);
  }

  const csrfToken = getCsrfToken();
  if (csrfToken) {
    headers.set('X-CSRF-Token', csrfToken);
  }

  const response = await fetchImpl(options.url, {
    method: 'POST',
    headers,
    body: JSON.stringify(options.payload),
    credentials: 'include',
    signal: options.signal,
  });

  if (!response.ok) {
    const errorText = await response.text().catch(() => response.statusText);
    options.onEvent({
      event: 'message_error',
      data: {
        code: `HTTP_${response.status}`,
        message: errorText || '流式问答请求失败',
        retryable: response.status >= 500 || response.status === 429,
      },
    });
    return;
  }

  if (!response.body) {
    options.onEvent({
      event: 'message_error',
      data: {
        code: 'EMPTY_SSE_STREAM',
        message: '服务端未返回可读的 SSE 响应流',
        retryable: true,
      },
    });
    return;
  }

  await parseSSEStream(response.body, options.onEvent, options.signal);
}
