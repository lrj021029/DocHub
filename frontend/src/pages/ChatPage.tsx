import { useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  BookOpen,
  Lock,
  MessageSquarePlus,
  MessageSquareText,
  Send,
  ShieldAlert,
  Square,
  ThumbsDown,
  ThumbsUp,
  Trash2,
} from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  Alert,
  Badge,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  Input,
  Skeleton,
} from '@/components/ui';
import {
  cancelChatMessage,
  createConversation,
  deleteConversation,
  fetchCitationDetail,
  fetchConversationDetail,
  fetchConversations,
  formatChatError,
  submitChatFeedback,
  type ChatMessageItem,
  type CitationDetail,
} from '@/features/chat/api';
import { CitationPreviewDialog } from '@/features/chat/CitationPreviewDialog';
import { fetchKnowledgeSpaces } from '@/features/spaces/api';
import { useAuth } from '@/lib/auth-store';
import { cn } from '@/lib/cn';
import { streamChatSSE, type ChatSSEEvent, type CitationData } from '@/lib/sse-client';

/**
 * PRD 模块 7（Slice 4 & 5 真实对接）：AI 问答工作台。
 * 特性：
 * - 左侧历史会话栏（GET/POST /api/v1/chat/conversations）+ 多 Space 复选器；
 * - POST Fetch SSE 流式输出（严禁 URL 携带 JWT），实时展示检索阶段、Markdown 渲染、停止生成与反馈；
 * - 历史会话动态重鉴权渲染：对 redacted=true 的段落或整体失权消息展示 Notion 柔和锁定 Callout；
 * - 点击引用卡片调用 GET /api/v1/citations/{citation_id} 实时重鉴权并弹出定位高亮弹窗。
 */
export function ChatPage() {
  const queryClient = useQueryClient();
  const { currentOrgId } = useAuth();

  const [activeConversationId, setActiveConversationId] = useState<string | null>(null);
  const [selectedSpaceIds, setSelectedSpaceIds] = useState<string[]>([]);
  const [question, setQuestion] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [streamStage, setStreamStage] = useState('就绪');
  const [streamingMessageId, setStreamingMessageId] = useState<string | null>(null);
  const [liveMessageId, setLiveMessageId] = useState<string | null>(null);
  const [liveAnswerText, setLiveAnswerText] = useState('');
  const [liveCitations, setLiveCitations] = useState<CitationData[]>([]);
  const [liveUserQuestion, setLiveUserQuestion] = useState<string | null>(null);
  const [streamError, setStreamError] = useState<string | null>(null);

  // 引用点击重鉴权弹窗状态
  const [citationModalOpen, setCitationModalOpen] = useState(false);
  const [citationLoading, setCitationLoading] = useState(false);
  const [citationError, setCitationError] = useState<string | null>(null);
  const [citationDetail, setCitationDetail] = useState<CitationDetail | null>(null);

  const abortRef = useRef<AbortController | null>(null);

  const spacesQuery = useQuery({
    queryKey: ['spaces', currentOrgId],
    queryFn: () => fetchKnowledgeSpaces(currentOrgId!),
    enabled: Boolean(currentOrgId),
  });

  const conversationsQuery = useQuery({
    queryKey: ['chat', currentOrgId, 'conversations'],
    queryFn: () => fetchConversations(currentOrgId!),
    enabled: Boolean(currentOrgId),
  });

  const conversations = conversationsQuery.data ?? [];
  const currentConvId = activeConversationId ?? conversations[0]?.id ?? null;

  const detailQuery = useQuery({
    queryKey: ['chat', currentOrgId, 'conversation', currentConvId],
    queryFn: () => fetchConversationDetail(currentOrgId!, currentConvId!),
    enabled: Boolean(currentOrgId && currentConvId),
  });

  const spaces = spacesQuery.data ?? [];
  const activeSpaceSelection = useMemo(() => {
    if (selectedSpaceIds.length > 0) return selectedSpaceIds;
    if (detailQuery.data?.spaceIds?.length) return detailQuery.data.spaceIds;
    return spaces[0] ? [spaces[0].id] : [];
  }, [detailQuery.data?.spaceIds, selectedSpaceIds, spaces]);

  const persistedMessages = detailQuery.data?.messages ?? [];
  // 当后端持久化详情已包含本轮流式消息 ID（或相同问答内容）时，自动交接并隐藏本地流式预览，杜绝重复渲染一轮 QA
  const isLiveTurnPersisted = useMemo(() => {
    if (streaming || !liveUserQuestion) return false;
    if (liveMessageId && persistedMessages.some((m) => m.id === liveMessageId)) {
      return true;
    }
    if (persistedMessages.length >= 2) {
      const lastMsg = persistedMessages[persistedMessages.length - 1];
      const prevMsg = persistedMessages[persistedMessages.length - 2];
      if (
        prevMsg?.role === 'user' &&
        prevMsg.content.trim() === liveUserQuestion.trim() &&
        lastMsg?.role === 'assistant'
      ) {
        return true;
      }
    }
    return false;
  }, [liveMessageId, liveUserQuestion, persistedMessages, streaming]);

  useEffect(() => {
    if (isLiveTurnPersisted) {
      setLiveMessageId(null);
      setLiveAnswerText('');
      setLiveCitations([]);
      setLiveUserQuestion(null);
    }
  }, [isLiveTurnPersisted]);

  useEffect(() => {
    setLiveMessageId(null);
    setLiveAnswerText('');
    setLiveCitations([]);
    setLiveUserQuestion(null);
    setStreamError(null);
  }, [currentConvId]);

  const createConvMutation = useMutation({
    mutationFn: () =>
      createConversation(currentOrgId!, {
        title: '新知识检索会话',
        space_ids: activeSpaceSelection,
      }),
    onSuccess: (created) => {
      setActiveConversationId(created.id);
      void queryClient.invalidateQueries({ queryKey: ['chat', currentOrgId, 'conversations'] });
    },
    onError: (err) => {
      setStreamError(formatChatError(err, '创建新会话失败'));
    },
  });

  const deleteConvMutation = useMutation({
    mutationFn: (conversationId: string) => deleteConversation(currentOrgId!, conversationId),
    onSuccess: (_data, deletedId) => {
      if (currentConvId === deletedId) {
        setActiveConversationId(null);
        setLiveMessageId(null);
        setLiveAnswerText('');
        setLiveCitations([]);
        setLiveUserQuestion(null);
      }
      queryClient.removeQueries({
        queryKey: ['chat', currentOrgId, 'conversation', deletedId],
      });
      void queryClient.invalidateQueries({ queryKey: ['chat', currentOrgId, 'conversations'] });
    },
    onError: (err) => {
      setStreamError(formatChatError(err, '删除会话失败'));
    },
  });

  const feedbackMutation = useMutation({
    mutationFn: ({ messageId, rating }: { messageId: string; rating: 'up' | 'down' }) =>
      submitChatFeedback(currentOrgId!, messageId, rating),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: ['chat', currentOrgId, 'conversation', currentConvId],
      });
    },
  });

  const toggleSpace = (spaceId: string) => {
    setSelectedSpaceIds((prev) => {
      const base = prev.length > 0 ? prev : activeSpaceSelection;
      return base.includes(spaceId)
        ? base.filter((id) => id !== spaceId)
        : [...base, spaceId];
    });
  };

  /** 点击引用卡片时执行实时重鉴权 */
  const handleInspectCitation = async (citationId: string) => {
    if (!currentOrgId) return;
    setCitationModalOpen(true);
    setCitationLoading(true);
    setCitationError(null);
    setCitationDetail(null);

    try {
      const detail = await fetchCitationDetail(currentOrgId, citationId);
      setCitationDetail(detail);
    } catch (err) {
      setCitationError(formatChatError(err, '引用鉴权失败'));
    } finally {
      setCitationLoading(false);
    }
  };

  /** 发起流式提问（自动保证已创建 conversation 并以 POST Fetch SSE 传输） */
  const handleSendQuestion = async () => {
    const trimmed = question.trim();
    if (!trimmed || streaming || !currentOrgId) return;

    setStreaming(true);
    setStreamError(null);
    setLiveMessageId(null);
    setLiveUserQuestion(trimmed);
    setLiveAnswerText('');
    setLiveCitations([]);
    setQuestion('');
    setStreamStage('正在准备鉴权会话...');

    let targetConvId = currentConvId;
    try {
      if (!targetConvId) {
        const created = await createConversation(currentOrgId, {
          title: trimmed.slice(0, 32),
          space_ids: activeSpaceSelection,
        });
        targetConvId = created.id;
        setActiveConversationId(created.id);
        void queryClient.invalidateQueries({ queryKey: ['chat', currentOrgId, 'conversations'] });
      }

      const controller = new AbortController();
      abortRef.current = controller;
      setStreamStage('正在建立安全 SSE 连接...');

      await streamChatSSE({
        url: `/api/v1/chat/conversations/${encodeURIComponent(targetConvId)}/messages:stream`,
        payload: {
          query: trimmed,
          space_ids: activeSpaceSelection,
        },
        organizationId: currentOrgId,
        signal: controller.signal,
        onEvent: (evt: ChatSSEEvent) => {
          switch (evt.event) {
            case 'message_start':
              setStreamingMessageId(evt.data.message_id);
              setLiveMessageId(evt.data.message_id);
              setStreamStage('已建立消息，准备检索');
              break;
            case 'retrieval_started':
              setStreamStage(`正在检索 ${evt.data.space_ids.length} 个空间...`);
              break;
            case 'retrieval_completed':
              setStreamStage(`ACL 复核通过 ${evt.data.authorized_count} 条授权证据`);
              break;
            case 'citation':
              setLiveCitations((prev) => [...prev, evt.data]);
              break;
            case 'token':
              setLiveAnswerText((prev) => prev + evt.data.text);
              break;
            case 'message_complete':
              setStreamStage('生成完成');
              break;
            case 'message_error':
              setStreamError(`${evt.data.message ?? '回答流中断'}（${evt.data.code}）`);
              setStreamStage('已中断');
              break;
          }
        },
      });

      // 完成后同步刷新持久化历史消息
      void queryClient.invalidateQueries({
        queryKey: ['chat', currentOrgId, 'conversation', targetConvId],
      });
    } catch (error) {
      if (!(error instanceof DOMException && error.name === 'AbortError')) {
        setStreamError(formatChatError(error, '流式问答连接失败'));
      }
      setStreamStage('已停止');
    } finally {
      setStreaming(false);
      setStreamingMessageId(null);
      abortRef.current = null;
    }
  };

  /** 停止生成并通知后端取消任务 */
  const handleCancelGeneration = async () => {
    abortRef.current?.abort();
    if (currentOrgId && streamingMessageId) {
      await cancelChatMessage(currentOrgId, streamingMessageId).catch(() => undefined);
    }
  };

  /** 渲染单条历史消息（支持段落级 redacted 撤权隐藏 Callout） */
  const renderHistoryMessage = (msg: ChatMessageItem) => {
    if (msg.role === 'user') {
      return (
        <div key={msg.id} className="flex justify-end">
          <div className="max-w-[82%] rounded-xl bg-accent px-4 py-2.5 text-sm text-ink-inverted">
            {msg.content}
          </div>
        </div>
      );
    }

    return (
      <div
        key={msg.id}
        className="space-y-3 rounded-xl border border-line bg-surface-subtle p-4"
        data-testid={`chat-message-${msg.id}`}
      >
        {msg.redacted ? (
          <Alert
            variant="warning"
            title="受限内容已隐藏（当前权限已撤回）"
            icon={<Lock className="h-4 w-4" />}
          >
            该历史回答来源权限已发生变化或无法拆分受限来源，依据安全红线已整段隐藏，仅保留审计证据。
          </Alert>
        ) : msg.segments.length > 0 ? (
          <div className="space-y-2.5">
            {msg.segments.map((seg) =>
              seg.redacted ? (
                <Alert
                  key={seg.id}
                  variant="warning"
                  title="已撤权回答片段已脱敏隐藏"
                  icon={<Lock className="h-4 w-4" />}
                >
                  {seg.redactedReason ?? '您当前对该段落引用来源已无读取权限，系统在重鉴权后已自动隐藏。'}
                </Alert>
              ) : (
                <div key={seg.id} className="prose prose-sm max-w-none text-sm text-ink">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{seg.content}</ReactMarkdown>
                </div>
              ),
            )}
          </div>
        ) : (
          <div className="prose prose-sm max-w-none text-sm text-ink">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.content}</ReactMarkdown>
          </div>
        )}

        {/* 引用卡片列表 */}
        {msg.citations.length > 0 ? (
          <div className="flex flex-wrap gap-2 pt-1">
            {msg.citations.map((cit) => (
              <button
                key={cit.citation_id}
                type="button"
                onClick={() => void handleInspectCitation(cit.citation_id)}
                className="inline-flex items-center gap-1.5 rounded-md border border-line bg-surface-elevated px-2.5 py-1 text-xs text-ink transition-colors hover:border-line-strong hover:bg-surface-muted"
              >
                <BookOpen className="h-3.5 w-3.5 text-info" aria-hidden="true" />
                <span className="max-w-44 truncate font-medium">{cit.document_title}</span>
                {typeof cit.page === 'number' ? (
                  <span className="text-[11px] text-ink-muted">P.{cit.page}</span>
                ) : null}
              </button>
            ))}
          </div>
        ) : null}

        {/* 答案反馈按钮 */}
        {!msg.redacted ? (
          <div className="flex items-center gap-2 border-t border-line/70 pt-2">
            <span className="text-[11px] text-ink-muted">回答是否有帮助：</span>
            <Button
              variant={msg.feedback === 'up' ? 'secondary' : 'ghost'}
              size="sm"
              className="h-7 px-2 text-xs"
              onClick={() => feedbackMutation.mutate({ messageId: msg.id, rating: 'up' })}
            >
              <ThumbsUp className="h-3 w-3" aria-hidden="true" />
              有用
            </Button>
            <Button
              variant={msg.feedback === 'down' ? 'secondary' : 'ghost'}
              size="sm"
              className="h-7 px-2 text-xs"
              onClick={() => feedbackMutation.mutate({ messageId: msg.id, rating: 'down' })}
            >
              <ThumbsDown className="h-3 w-3" aria-hidden="true" />
              无用/缺口
            </Button>
          </div>
        ) : null}
      </div>
    );
  };

  return (
    <div className="space-y-5">
      {/* 页头 */}
      <div className="flex flex-col gap-3 border-b border-line pb-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="space-y-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="outline" className="font-mono text-[10px]">
              PRD-07
            </Badge>
            <h1 className="text-xl font-semibold tracking-tight text-ink">AI 问答工作台</h1>
            <Badge variant="info">POST Fetch SSE</Badge>
            <Badge variant="neutral">每轮实时 ACL 重算</Badge>
          </div>
          <p className="text-sm text-ink-secondary">
            支持多 Knowledge Space 跨空间 RRF 融合、流式 Markdown 回答、点击引用重鉴权定位与历史撤权动态隐藏。
          </p>
        </div>

        <Button
          size="sm"
          variant="outline"
          disabled={!currentOrgId || createConvMutation.isPending}
          onClick={() => createConvMutation.mutate()}
        >
          <MessageSquarePlus className="h-3.5 w-3.5" aria-hidden="true" />
          新建会话
        </Button>
      </div>

      {!currentOrgId ? (
        <Alert variant="warning" title="尚未绑定当前 Organization">
          请先在顶部组织切换器中选择有效组织。
        </Alert>
      ) : (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
          {/* 左侧：会话历史与 Space 复选器 */}
          <Card className="lg:col-span-3">
            <CardHeader className="py-3.5">
              <CardTitle className="flex items-center gap-2">
                <MessageSquareText className="h-4 w-4 text-ink-muted" aria-hidden="true" />
                历史会话与检索范围
              </CardTitle>
              <CardDescription>重开历史会话将实时按当前权限重渲染。</CardDescription>
            </CardHeader>

            <CardContent className="space-y-4">
              {/* 多 Space 复选器 */}
              <div className="space-y-1.5">
                <p className="text-xs font-semibold text-ink">检索 Knowledge Space（可多选）</p>
                {spaces.length === 0 ? (
                  <p className="text-xs text-ink-muted">暂无可用 Space</p>
                ) : (
                  <div className="space-y-1">
                    {spaces.map((sp) => {
                      const checked = activeSpaceSelection.includes(sp.id);
                      return (
                        <label
                          key={sp.id}
                          className="flex cursor-pointer items-center justify-between gap-2 rounded-md border border-line/80 px-2.5 py-1.5 text-xs hover:bg-surface-muted"
                        >
                          <span className="flex items-center gap-2 truncate">
                            <input
                              type="checkbox"
                              checked={checked}
                              onChange={() => toggleSpace(sp.id)}
                              className="h-3.5 w-3.5 rounded border-line accent-accent"
                            />
                            <span className="truncate text-ink">{sp.name}</span>
                          </span>
                          <Badge variant={sp.visibility === 'PUBLIC' ? 'info' : 'warning'} className="text-[10px]">
                            {sp.visibility}
                          </Badge>
                        </label>
                      );
                    })}
                  </div>
                )}
              </div>

              {/* 会话列表 */}
              <div className="space-y-1.5 border-t border-line pt-3">
                <p className="text-xs font-semibold text-ink">最近会话</p>
                {conversationsQuery.isLoading ? (
                  <div className="space-y-1.5">
                    <Skeleton shape="block" className="h-8 w-full" />
                    <Skeleton shape="block" className="h-8 w-full" />
                  </div>
                ) : conversations.length === 0 ? (
                  <p className="text-xs text-ink-muted">暂无历史会话，可直接提问自动创建。</p>
                ) : (
                  <ul className="space-y-1">
                    {conversations.map((conv) => (
                      <li key={conv.id} className="group flex items-center gap-1">
                        <button
                          type="button"
                          onClick={() => setActiveConversationId(conv.id)}
                          className={cn(
                            'min-w-0 flex-1 truncate rounded-md px-2.5 py-1.5 text-left text-xs transition-colors',
                            conv.id === currentConvId
                              ? 'bg-surface-muted font-semibold text-ink'
                              : 'text-ink-secondary hover:bg-surface-muted hover:text-ink',
                          )}
                        >
                          {conv.title}
                        </button>
                        <button
                          type="button"
                          aria-label={`删除会话 ${conv.title}`}
                          title="删除该会话"
                          disabled={deleteConvMutation.isPending}
                          onClick={(event) => {
                            event.stopPropagation();
                            deleteConvMutation.mutate(conv.id);
                          }}
                          className="shrink-0 rounded-md p-1 text-ink-muted opacity-0 transition-opacity hover:bg-danger-subtle hover:text-danger focus-visible:opacity-100 group-hover:opacity-100"
                        >
                          <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </CardContent>
          </Card>

          {/* 右侧：对话与 SSE 流式区 */}
          <Card className="lg:col-span-9 flex flex-col">
            <CardHeader className="flex-row items-center justify-between space-y-0 py-3.5">
              <div>
                <CardTitle>{detailQuery.data?.title ?? '受 ACL 保护的企业问答'}</CardTitle>
                <CardDescription>
                  Authorization Header 鉴权流式传输 · 严禁在 URL Query 传递任何 JWT
                </CardDescription>
              </div>
              <Badge variant={streaming ? 'info' : 'neutral'} data-testid="chat-stream-stage">
                {streamStage}
              </Badge>
            </CardHeader>

            <CardContent className="flex flex-1 flex-col justify-between space-y-4">
              {streamError ? (
                <Alert variant="danger" title="问答流提示">
                  {streamError}
                </Alert>
              ) : null}

              {/* 消息时间线 */}
              <div className="min-h-80 max-h-[32rem] space-y-4 overflow-y-auto pr-1">
                {detailQuery.isLoading ? (
                  <div className="space-y-3">
                    <Skeleton shape="block" className="h-14 w-3/4" />
                    <Skeleton shape="block" className="h-24 w-full" />
                  </div>
                ) : (
                  (detailQuery.data?.messages ?? []).map(renderHistoryMessage)
                )}

                {/* 本轮实时流式预览（仅在后端持久化列表尚未包含本轮消息时展示，杜绝重复 QA） */}
                {!isLiveTurnPersisted && liveUserQuestion ? (
                  <div className="flex justify-end">
                    <div className="max-w-[82%] rounded-xl bg-accent px-4 py-2.5 text-sm text-ink-inverted">
                      {liveUserQuestion}
                    </div>
                  </div>
                ) : null}

                {!isLiveTurnPersisted && (streaming || liveAnswerText || liveCitations.length > 0) ? (
                  <div
                    className="space-y-3 rounded-xl border border-line bg-surface-subtle p-4"
                    data-testid="live-sse-answer"
                  >
                    <div className="prose prose-sm max-w-none text-sm text-ink">
                      {liveAnswerText ? (
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>{liveAnswerText}</ReactMarkdown>
                      ) : (
                        <p className="text-xs text-ink-muted">正在等待授权证据与首个 Token...</p>
                      )}
                    </div>

                    {liveCitations.length > 0 ? (
                      <div className="flex flex-wrap gap-2 pt-1">
                        {liveCitations.map((cit) => (
                          <button
                            key={cit.citation_id}
                            type="button"
                            onClick={() => void handleInspectCitation(cit.citation_id)}
                            className="inline-flex items-center gap-1.5 rounded-md border border-line bg-surface-elevated px-2.5 py-1 text-xs text-ink hover:bg-surface-muted"
                          >
                            <BookOpen className="h-3.5 w-3.5 text-info" aria-hidden="true" />
                            <span>{cit.document_title}</span>
                            {typeof cit.page === 'number' ? (
                              <span className="text-[11px] text-ink-muted">P.{cit.page}</span>
                            ) : null}
                          </button>
                        ))}
                      </div>
                    ) : null}
                  </div>
                ) : null}

                {!detailQuery.isLoading &&
                (detailQuery.data?.messages.length ?? 0) === 0 &&
                !liveUserQuestion ? (
                  <Alert variant="info" icon={<ShieldAlert className="h-4 w-4" />}>
                    请勾选左侧授权 Knowledge Space 并输入问题。无权限文档的存在性、标题与摘要绝不会向未授权用户泄露。
                  </Alert>
                ) : null}
              </div>

              {/* 提问输入框 */}
              <div className="flex items-center gap-2 border-t border-line pt-3">
                <Input
                  aria-label="输入知识库提问内容"
                  placeholder="输入企业知识问题，回车或点击发送..."
                  value={question}
                  onChange={(event) => setQuestion(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' && !event.shiftKey) {
                      event.preventDefault();
                      void handleSendQuestion();
                    }
                  }}
                />
                {streaming ? (
                  <Button variant="destructive" size="md" onClick={() => void handleCancelGeneration()}>
                    <Square className="h-3.5 w-3.5" aria-hidden="true" />
                    停止
                  </Button>
                ) : (
                  <Button
                    size="md"
                    onClick={() => void handleSendQuestion()}
                    disabled={!question.trim()}
                  >
                    <Send className="h-3.5 w-3.5" aria-hidden="true" />
                    发送
                  </Button>
                )}
              </div>
            </CardContent>
          </Card>
        </div>
      )}

      {/* 引用点击实时重鉴权定位弹窗 */}
      <CitationPreviewDialog
        open={citationModalOpen}
        onOpenChange={setCitationModalOpen}
        isLoading={citationLoading}
        errorMessage={citationError}
        citation={citationDetail}
      />
    </div>
  );
}
