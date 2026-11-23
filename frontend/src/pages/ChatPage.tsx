import { useRef, useState } from 'react';
import { BookOpen, Send, ShieldAlert, Square } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { ModuleWorkspace } from '@/components/common/ModuleWorkspace';
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
} from '@/components/ui';
import { streamChatSSE, type ChatSSEEvent, type CitationData } from '@/lib/sse-client';

/**
 * PRD 模块 7：AI 问答工作台（多 Space 选择、历史会话、POST Fetch SSE 流式输出、Markdown 与引用溯源）。
 * 安全红线：
 * - 严禁在 URL Query 拼接任何 JWT，统一由 streamChatSSE 通过 Authorization Bearer Header 发送；
 * - 引用卡片打开与历史会话重开均须向后端重新鉴权。
 */
export function ChatPage() {
  const [question, setQuestion] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [streamStage, setStreamStage] = useState<string>('待提问');
  const [answerMarkdown, setAnswerMarkdown] = useState<string>('');
  const [citations, setCitations] = useState<CitationData[]>([]);
  const [streamError, setStreamError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const handleAsk = async () => {
    const trimmed = question.trim();
    if (!trimmed || streaming) return;

    setStreaming(true);
    setStreamError(null);
    setAnswerMarkdown('');
    setCitations([]);
    setStreamStage('正在建立 SSE 安全连接...');

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      await streamChatSSE({
        url: '/api/v1/chat/conversations/active/messages:stream',
        payload: {
          query: trimmed,
          space_ids: [],
        },
        signal: controller.signal,
        onEvent: (evt: ChatSSEEvent) => {
          switch (evt.event) {
            case 'message_start':
              setStreamStage(`已创建消息 ${evt.data.message_id}`);
              break;
            case 'retrieval_started':
              setStreamStage(`正在多空间检索（${evt.data.space_ids.length} 个 Space）...`);
              break;
            case 'retrieval_completed':
              setStreamStage(`权威 ACL 复核通过 ${evt.data.authorized_count} 条证据片段`);
              break;
            case 'citation':
              setCitations((prev) => [...prev, evt.data]);
              break;
            case 'token':
              setAnswerMarkdown((prev) => prev + evt.data.text);
              break;
            case 'message_complete':
              setStreamStage('回答生成完成');
              break;
            case 'message_error':
              setStreamError(`${evt.data.message ?? '问答流返回错误'}（${evt.data.code}）`);
              setStreamStage('已中断');
              break;
          }
        },
      });
    } catch (error) {
      if (!(error instanceof DOMException && error.name === 'AbortError')) {
        setStreamError('无法连接流式问答接口（POST /api/v1/chat/conversations/{id}/messages:stream）。');
      }
      setStreamStage('已停止');
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  };

  const handleStop = () => {
    abortRef.current?.abort();
  };

  return (
    <ModuleWorkspace
      moduleCode="PRD-07"
      title="AI 问答工作台"
      description="支持当前 Organization 内多 Knowledge Space 联合检索、POST Fetch SSE 流式输出、Markdown 渲染与可重鉴权引用卡片。"
      apiContract="POST /api/v1/chat/conversations/{id}/messages:stream · GET /api/v1/citations/{citation_id}"
      governanceBadges={['Fetch POST SSE', '无 URL JWT', '每轮重算 ACL']}
    >
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        {/* 左侧问答主区 */}
        <Card className="lg:col-span-2">
          <CardHeader className="flex-row items-center justify-between space-y-0">
            <div>
              <CardTitle>受 ACL 保护的流式问答流</CardTitle>
              <CardDescription>支持 7 类标准 SSE 事件：start / retrieval / citation / token / complete / error</CardDescription>
            </div>
            <Badge variant={streaming ? 'info' : 'neutral'}>{streamStage}</Badge>
          </CardHeader>

          <CardContent className="space-y-4">
            {streamError ? (
              <Alert variant="danger" title="流式响应异常">
                {streamError}
              </Alert>
            ) : null}

            {/* 回答内容渲染区（基于 react-markdown + remark-gfm） */}
            <div className="min-h-56 rounded-lg border border-line bg-surface-subtle p-4 text-sm leading-relaxed text-ink">
              {answerMarkdown ? (
                <div className="prose prose-sm max-w-none">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{answerMarkdown}</ReactMarkdown>
                </div>
              ) : (
                <p className="text-xs text-ink-muted">
                  输入业务问题并点击发送，前端将通过 Authorization Header 发起 POST SSE
                  流式请求。未授权文档的存在性、标题与摘要绝不会出现在响应流中。
                </p>
              )}
            </div>

            {/* 提问输入栏 */}
            <div className="flex items-center gap-2">
              <Input
                aria-label="输入知识库提问内容"
                placeholder="例如：差旅住宿报销标准是什么？"
                value={question}
                onChange={(event) => setQuestion(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' && !event.shiftKey) {
                    event.preventDefault();
                    void handleAsk();
                  }
                }}
              />
              {streaming ? (
                <Button variant="destructive" size="md" onClick={handleStop}>
                  <Square className="h-3.5 w-3.5" aria-hidden="true" />
                  停止
                </Button>
              ) : (
                <Button size="md" onClick={() => void handleAsk()} disabled={!question.trim()}>
                  <Send className="h-3.5 w-3.5" aria-hidden="true" />
                  发送
                </Button>
              )}
            </div>
          </CardContent>
        </Card>

        {/* 右侧引用溯源区 */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <BookOpen className="h-4 w-4 text-ink-muted" aria-hidden="true" />
              授权引用溯源（{citations.length}）
            </CardTitle>
            <CardDescription>点击引用查看原页高亮时，将调用后端接口重新校验当前权限。</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {citations.length === 0 ? (
              <div className="rounded-lg border border-dashed border-line p-4 text-center text-xs text-ink-muted">
                当前会话暂无已返回的授权引用卡片。
              </div>
            ) : (
              citations.map((item) => (
                <div
                  key={item.citation_id}
                  className="rounded-lg border border-line bg-surface-subtle p-3 text-xs space-y-1"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-medium text-ink truncate">{item.document_title}</span>
                    {item.page !== undefined ? (
                      <Badge variant="outline">第 {item.page} 页</Badge>
                    ) : null}
                  </div>
                  {item.quote ? (
                    <p className="line-clamp-3 text-ink-secondary">{item.quote}</p>
                  ) : null}
                </div>
              ))
            )}

            <Alert variant="warning" icon={<ShieldAlert className="h-4 w-4" />}>
              历史对话重开时将逐段复核当前权限；已撤权来源的正文片段会自动隐藏或脱敏。
            </Alert>
          </CardContent>
        </Card>
      </div>
    </ModuleWorkspace>
  );
}
