import { Cpu, ShieldAlert } from 'lucide-react';
import { ModuleWorkspace } from '@/components/common/ModuleWorkspace';
import {
  Alert,
  Badge,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui';

/**
 * PRD 模块 12：模型、MinerU API、Embedding、Reranker 与外发策略系统配置。
 * 安全铁律：所有 API Key 仅托管于后端环境变量 / Secret 存储，前端仅展示脱敏后的 Provider 路由状态。
 */
export function SettingsPage() {
  const providerRows = [
    {
      capability: 'LLM 对话 / 改写 / FAQ 生成',
      provider: 'OpenAICompatibleLLMProvider',
      routing: '后端可配置 Base URL / Model（Secret 不下发前端）',
      policy: '受 EgressPolicyEngine 与敏感等级约束',
    },
    {
      capability: 'Embedding & Reranker（双选架构）',
      provider: 'Qwen 官方模型 API / 本地 BGE（LOCAL_BGE_*）',
      routing: 'L0/L1 经授权走 Qwen；L2/L3 优先本地 BGE 或安全拦截',
      policy: '遵循 ADR-003 双 Provider 路由',
    },
    {
      capability: '复杂 PDF / 扫描件解析',
      provider: 'MinerUOfficialAdapter / LocalOCRParserAdapter',
      routing: 'L0/L1 复杂 PDF 走 MinerU 官方 API；L2/L3 扫描件默认 Fail-Closed',
      policy: '无本地 OCR 时进入 BLOCKED_POLICY',
    },
  ];

  return (
    <ModuleWorkspace
      moduleCode="PRD-12"
      title="模型与系统配置中心"
      description="统一查看与治理 LLM、Embedding、Reranker 双 Provider 路由、MinerU 解析适配器及 L0-L3 数据外发策略。"
      apiContract="GET/PATCH /api/v1/providers · /api/v1/egress-policies（Secret 仅驻留后端）"
      governanceBadges={['零前端 Secret', 'Qwen + 本地 BGE 双选', 'L0-L3 外发门控']}
    >
      <div className="space-y-4">
        <Alert variant="danger" title="密钥零暴露安全边界（ADR-001 & TECHNICAL_SPEC §3.3）">
          前端严禁保存、回显或透传任何 MinerU API Key、LLM API Key、Embedding Key 或 Reranker
          Secret；组织策略仅允许在系统最高安全限制基础上进一步收紧。
        </Alert>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Cpu className="h-4 w-4 text-ink-muted" aria-hidden="true" />
              Provider 能力路由矩阵（ADR-003）
            </CardTitle>
            <CardDescription>
              所有模型与解析外发调用前均强制经过 EgressPolicyEngine.check() 并落盘审计元数据。
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>能力域</TableHead>
                  <TableHead>适配器 / Provider</TableHead>
                  <TableHead>路由规则</TableHead>
                  <TableHead>安全门控</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {providerRows.map((row) => (
                  <TableRow key={row.capability}>
                    <TableCell className="font-medium">{row.capability}</TableCell>
                    <TableCell className="font-mono text-xs">{row.provider}</TableCell>
                    <TableCell className="text-xs text-ink-secondary">{row.routing}</TableCell>
                    <TableCell>
                      <Badge variant="outline" className="gap-1">
                        <ShieldAlert className="h-3 w-3" aria-hidden="true" />
                        {row.policy}
                      </Badge>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      </div>
    </ModuleWorkspace>
  );
}
