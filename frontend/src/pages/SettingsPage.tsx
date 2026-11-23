import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Cpu, LockKeyhole, Save, ShieldAlert } from 'lucide-react';
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
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui';
import {
  fetchProviderConfig,
  formatGovError,
  updateProviderConfig,
} from '@/features/governance/api';
import { useAuth } from '@/lib/auth-store';

/**
 * PRD 模块 12：系统 Provider 与数据外发策略配置中心。
 * 遵循 ADR-001 & ADR-003 铁律：
 * - 展示并可配置 LLM（OpenAI-compatible，默认 https://100milionseek.top/v1）、Qwen / 本地 BGE 双选与 MinerU / Local OCR 开关；
 * - 绝不在前端保存、输入框渲染或日志输出任何明文 API Key / Secret！
 */
export function SettingsPage() {
  const queryClient = useQueryClient();
  const { currentOrgId } = useAuth();
  const [embeddingPrimary, setEmbeddingPrimary] = useState<'QWEN_API' | 'LOCAL_BGE'>('QWEN_API');
  const [localBgeEnabled, setLocalBgeEnabled] = useState(false);
  const [localOcrEnabled, setLocalOcrEnabled] = useState(false);
  const [llmBaseUrl, setLlmBaseUrl] = useState('https://100milionseek.top/v1');
  const [llmModel, setLlmModel] = useState('');
  const [feedback, setFeedback] = useState<{ variant: 'success' | 'danger'; message: string } | null>(
    null,
  );

  const configQuery = useQuery({
    queryKey: ['providers', currentOrgId, 'config'],
    queryFn: () => fetchProviderConfig(currentOrgId!),
    enabled: Boolean(currentOrgId),
    retry: false,
  });

  useEffect(() => {
    const cfg = configQuery.data;
    if (!cfg) return;
    setEmbeddingPrimary(cfg.embedding.primaryProvider === 'LOCAL_BGE' ? 'LOCAL_BGE' : 'QWEN_API');
    setLocalBgeEnabled(cfg.embedding.localBgeEnabled);
    setLocalOcrEnabled(cfg.parser.localOcrEnabled);
    setLlmBaseUrl(cfg.llm.baseUrl);
    setLlmModel(cfg.llm.model);
  }, [configQuery.data]);

  const saveMutation = useMutation({
    mutationFn: () =>
      updateProviderConfig(currentOrgId!, {
        embedding_primary_provider: embeddingPrimary,
        local_bge_enabled: localBgeEnabled,
        local_ocr_enabled: localOcrEnabled,
        llm_base_url: llmBaseUrl.trim(),
        llm_model: llmModel.trim() || undefined,
      }),
    onSuccess: () => {
      setFeedback({
        variant: 'success',
        message: 'Provider 路由与本地安全开关已更新（API Key 始终由后端 Secret 托管）。',
      });
      void queryClient.invalidateQueries({ queryKey: ['providers', currentOrgId, 'config'] });
    },
    onError: (err) => {
      setFeedback({ variant: 'danger', message: formatGovError(err, '更新系统配置失败') });
    },
  });

  const cfg = configQuery.data;

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-3 border-b border-line pb-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="space-y-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="outline" className="font-mono text-[10px]">
              PRD-12
            </Badge>
            <h1 className="text-xl font-semibold tracking-tight text-ink">系统与模型配置中心</h1>
            <Badge variant="danger">零明文 API Key</Badge>
            <Badge variant="info">Qwen + Local BGE 双选</Badge>
          </div>
          <p className="text-sm text-ink-secondary">
            统一管理 LLM Base URL、Embedding/Reranker 双 Provider 路由、MinerU 官方 API 与本地 OCR 开关及 L0–L3 外发矩阵。
          </p>
        </div>
      </div>

      <Alert variant="danger" title="密钥零暴露安全红线（ADR-001 & TECHNICAL_SPEC §3.3）" icon={<LockKeyhole className="h-4 w-4" />}>
        前端绝不存储、回显或提交任何 MinerU API Key、LLM API Key、Qwen Key 或 Reranker Secret。所有密钥均在服务端 Secret 环境变量中托管，前端仅展示脱敏的「已托管」状态。
      </Alert>

      {feedback ? <Alert variant={feedback.variant}>{feedback.message}</Alert> : null}

      {configQuery.isLoading ? (
        <Skeleton shape="block" className="h-48 w-full" />
      ) : (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          {/* Provider 双选路由配置 */}
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Cpu className="h-4 w-4 text-ink-muted" aria-hidden="true" />
                模型与解析 Provider 路由（ADR-003）
              </CardTitle>
              <CardDescription>
                L0/L1 优先使用官方云 API；L2/L3 敏感文档强制路由至本地 BGE / Local OCR 或 Fail-Closed。
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-3.5">
              <div className="space-y-1">
                <label htmlFor="cfg-llm-url" className="text-xs font-medium text-ink">
                  OpenAI-Compatible LLM Base URL
                </label>
                <Input
                  id="cfg-llm-url"
                  value={llmBaseUrl}
                  onChange={(e) => setLlmBaseUrl(e.target.value)}
                />
              </div>

              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <div className="space-y-1">
                  <label htmlFor="cfg-llm-model" className="text-xs font-medium text-ink">
                    对话生成模型名称
                  </label>
                  <Input
                    id="cfg-llm-model"
                    value={llmModel}
                    onChange={(e) => setLlmModel(e.target.value)}
                    placeholder="configured-on-backend"
                  />
                </div>
                <div className="space-y-1">
                  <label htmlFor="cfg-emb-primary" className="text-xs font-medium text-ink">
                    Embedding / Reranker 主路由
                  </label>
                  <select
                    id="cfg-emb-primary"
                    value={embeddingPrimary}
                    onChange={(e) =>
                      setEmbeddingPrimary(e.target.value === 'LOCAL_BGE' ? 'LOCAL_BGE' : 'QWEN_API')
                    }
                    className="flex h-9 w-full rounded-lg border border-line bg-surface-elevated px-3 text-sm text-ink"
                  >
                    <option value="QWEN_API">Qwen 官方模型 API（默认优先）</option>
                    <option value="LOCAL_BGE">本地 BGE 服务（LOCAL_BGE_*）</option>
                  </select>
                </div>
              </div>

              <div className="space-y-2 rounded-lg border border-line bg-surface-subtle p-3 text-xs">
                <label className="flex cursor-pointer items-center justify-between gap-2">
                  <span className="font-medium text-ink">启用本地 BGE Embedding / Reranker（供 L2/L3 使用）</span>
                  <input
                    type="checkbox"
                    checked={localBgeEnabled}
                    onChange={(e) => setLocalBgeEnabled(e.target.checked)}
                    className="h-4 w-4 rounded border-line accent-accent"
                  />
                </label>
                <label className="flex cursor-pointer items-center justify-between gap-2">
                  <span className="font-medium text-ink">启用本地 OCR 解析器（LOCAL_OCR_ENABLED）</span>
                  <input
                    type="checkbox"
                    checked={localOcrEnabled}
                    onChange={(e) => setLocalOcrEnabled(e.target.checked)}
                    className="h-4 w-4 rounded border-line accent-accent"
                  />
                </label>
              </div>

              <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
                <div className="flex flex-wrap gap-1.5">
                  <Badge variant="success">
                    LLM Secret: {cfg?.llm.hasApiKeyConfigured !== false ? '后端已托管' : '未配置'}
                  </Badge>
                  <Badge variant="success">
                    MinerU Secret: {cfg?.parser.hasMineruKeyConfigured !== false ? '后端已托管' : '未配置'}
                  </Badge>
                </div>
                <Button
                  size="sm"
                  loading={saveMutation.isPending}
                  disabled={!currentOrgId}
                  onClick={() => saveMutation.mutate()}
                >
                  <Save className="h-3.5 w-3.5" aria-hidden="true" />
                  保存路由策略
                </Button>
              </div>
            </CardContent>
          </Card>

          {/* L0-L3 数据外发门控矩阵 */}
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <ShieldAlert className="h-4 w-4 text-warning" aria-hidden="true" />
                L0–L3 数据外发安全策略矩阵
              </CardTitle>
              <CardDescription>
                Organization 策略只允许在系统安全基线上收紧，绝不允许放宽 L2/L3 外发限制。
              </CardDescription>
            </CardHeader>
            <CardContent>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>敏感级别</TableHead>
                    <TableHead>外部解析</TableHead>
                    <TableHead>外部模型</TableHead>
                    <TableHead>策略说明</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {(cfg?.egressMatrix ?? [
                    { level: 'L0', label: 'L0 Public', allowExternalParse: true, allowExternalLlm: true, note: '允许外部服务' },
                    { level: 'L1', label: 'L1 Internal', allowExternalParse: true, allowExternalLlm: true, note: '组织授权后允许' },
                    { level: 'L2', label: 'L2 Confidential', allowExternalParse: false, allowExternalLlm: false, note: '默认本地处理，无本地 OCR 时 BLOCKED_POLICY' },
                    { level: 'L3', label: 'L3 Restricted', allowExternalParse: false, allowExternalLlm: false, note: '严禁任何外部发送' },
                  ]).map((row) => (
                    <TableRow key={row.level}>
                      <TableCell>
                        <Badge variant={row.level === 'L2' || row.level === 'L3' ? 'danger' : 'outline'}>
                          {row.label}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-xs">
                        {row.allowExternalParse ? '允许' : '禁止'}
                      </TableCell>
                      <TableCell className="text-xs">
                        {row.allowExternalLlm ? '允许' : '禁止'}
                      </TableCell>
                      <TableCell className="text-xs text-ink-secondary">{row.note}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  );
}
