import { Gauge, ShieldCheck } from 'lucide-react';
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
 * PRD 模块 14：RAG / Parser Benchmark 评测台。
 * 坚守硬发布门槛：ACL Violation = 0、未授权内容进入 Reranker/LLM = 0、跨 Organization 泄露 = 0。
 */
export function BenchmarkPage() {
  const hardGates = [
    { name: 'ACL Violation', target: '严格等于 0', status: '待真实执行' },
    { name: '未授权 Chunk 进入 Reranker / LLM', target: '严格等于 0', status: '待真实执行' },
    { name: '跨 Organization 数据泄露', target: '严格等于 0', status: '待真实执行' },
    { name: 'Recall@K / MRR / Citation Accuracy', target: '按真实测试集评测', status: '待真实执行' },
    { name: 'MinerU 复杂 PDF / 表格 / 公式回归', target: '无敏感基准 PDF 实测', status: '待真实执行' },
  ];

  return (
    <ModuleWorkspace
      moduleCode="PRD-14"
      title="RAG 与解析 Benchmark"
      description="跟踪 L0–L3 测试分层、检索/引用质量指标、MinerU 解析回归与 ACL 对抗安全门槛，严禁虚构评测分数。"
      apiContract="GET/POST /api/v1/benchmarks/runs（真实测试数据回填）"
      governanceBadges={['ACL Violation = 0 硬门槛', '禁止虚构基准数据']}
    >
      <div className="space-y-4">
        <Alert variant="info" title="安全硬门槛不可折抵（TECHNICAL_SPEC §17.2）">
          任何 ACL 越权、跨组织泄露或未授权正文进入模型上下文的失败均直接阻断发布，绝不可由高准确率或低延迟指标抵消。
        </Alert>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Gauge className="h-4 w-4 text-ink-muted" aria-hidden="true" />
              评测门槛与回归清单
            </CardTitle>
            <CardDescription>
              所有结果将在 L2/L3 端到端与安全对抗测试执行后由后端持久化写入 BenchmarkRun。
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>评测维度</TableHead>
                  <TableHead>验收门槛</TableHead>
                  <TableHead>当前状态</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {hardGates.map((gate) => (
                  <TableRow key={gate.name}>
                    <TableCell className="font-medium">{gate.name}</TableCell>
                    <TableCell className="text-xs text-ink-secondary">{gate.target}</TableCell>
                    <TableCell>
                      <Badge variant="outline" className="gap-1">
                        <ShieldCheck className="h-3 w-3" aria-hidden="true" />
                        {gate.status}
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
