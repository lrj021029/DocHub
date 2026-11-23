import { ClipboardList, CheckCheck } from 'lucide-react';
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
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui';

/**
 * PRD 模块 10：Knowledge Gap 知识缺口清单与补充任务管理。
 * 严格遵循安全边界：ACL_DENIED 与 UNAUTHORIZED_ONLY_RESULTS 绝不进入普通知识缺口池。
 */
export function KnowledgeGapPage() {
  const gapStates = [
    'DETECTED',
    'TRIAGED',
    'ASSIGNED',
    'IN_PROGRESS',
    'CONTENT_SUBMITTED',
    'REVIEWING',
    'RESOLVED',
  ];

  return (
    <ModuleWorkspace
      moduleCode="PRD-10"
      title="Knowledge Gap 知识缺口闭环"
      description="聚合无召回、低相关性与授权证据不足的问题，流转补充任务并在新文档发布后执行回归验收。"
      apiContract="GET /api/v1/knowledge-gaps · POST /api/v1/knowledge-gaps/{id}/tasks · PATCH /api/v1/knowledge-gap-tasks/{id}"
      governanceBadges={['ACL 拒绝不入池', '回归验证后方可 RESOLVED']}
      metrics={[
        { label: '待分诊（DETECTED）', value: '--', hint: '仅包含合法知识缺口事件' },
        { label: '处理中任务', value: '--', hint: '绑定负责人、优先级、截止时间与目标 Space' },
        { label: '待回归验证', value: '--', hint: '补充文档发布后重跑 Gap 回归问题' },
        { label: '已闭环（RESOLVED）', value: '--', hint: '必须关联回归验证达标证据' },
      ]}
      headerActions={
        <Button size="sm" disabled>
          <ClipboardList className="h-3.5 w-3.5" aria-hidden="true" />
          新建补充任务
        </Button>
      }
    >
      <div className="space-y-4">
        <Alert variant="warning" title="缺口准入隔离规则（PRD §7.1）">
          ACL 拒绝、仅召回未授权文档、越权尝试进入安全审计；LLM / MinerU 基础设施故障进入运维监控，均严禁混入普通
          Knowledge Gap 池。
        </Alert>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <CheckCheck className="h-4 w-4 text-ink-muted" aria-hidden="true" />
              缺口生命周期与任务追踪
            </CardTitle>
            <CardDescription className="flex flex-wrap items-center gap-1.5 pt-1">
              {gapStates.map((state) => (
                <Badge key={state} variant="outline" className="font-mono text-[10px]">
                  {state}
                </Badge>
              ))}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>脱敏缺口主题</TableHead>
                  <TableHead>归属 Space</TableHead>
                  <TableHead>状态</TableHead>
                  <TableHead>负责人 / 截止时间</TableHead>
                  <TableHead>回归验收</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                <TableRow>
                  <TableCell colSpan={5} className="py-8 text-center text-xs text-ink-muted">
                    等待 Slice 7 Knowledge Gap 与任务流转接口接入后呈现真实记录。
                  </TableCell>
                </TableRow>
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      </div>
    </ModuleWorkspace>
  );
}
