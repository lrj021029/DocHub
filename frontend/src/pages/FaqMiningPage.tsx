import { Play, UserCheck } from 'lucide-react';
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
 * PRD 模块 8：FAQ 挖掘与审核中心。
 * 覆盖实时增量匹配、定时 HDBSCAN 批量重聚类、手动触发以及强制人工审核（创建人不可自审）。
 */
export function FaqMiningPage() {
  return (
    <ModuleWorkspace
      moduleCode="PRD-08"
      title="FAQ 挖掘与审核中心"
      description="从脱敏后的历史问答事件中通过 HDBSCAN 发现高频簇，基于有权知识来源生成结构化候选答案并执行严格人工审批。"
      apiContract="POST /api/v1/faq-mining/runs · GET /api/v1/faq-candidates · POST /api/v1/faq/{id}/reviews"
      governanceBadges={['禁止自动发布', '创建者 ≠ 审核者', '权限域隔离']}
      metrics={[
        { label: '待审 FAQ 候选', value: '--', hint: '需审核人复核来源引用与 ACL 范围' },
        { label: '最近 HDBSCAN 重聚类', value: '--', hint: '定时批量重聚类 + 实时增量匹配分离' },
        { label: '反刷量过滤事件', value: '--', hint: '按独立用户、会话与部门覆盖度评估' },
        { label: '来源最严继承', value: '强制', hint: '多来源 FAQ 默认继承最严格来源 ACL' },
      ]}
      headerActions={
        <Button size="sm" disabled>
          <Play className="h-3.5 w-3.5" aria-hidden="true" />
          手动触发挖掘
        </Button>
      }
    >
      <div className="space-y-4">
        <Alert variant="info" title="人工审核红线（PRD §6.6 & §10.1）">
          不允许任何 FAQ 自动发布。候选答案必须关联授权来源引用，创建者不能审核自己的 FAQ 候选；
          跨权限域的问题文本绝不合并为可见候选答案。
        </Alert>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <UserCheck className="h-4 w-4 text-ink-muted" aria-hidden="true" />
              FAQ 候选审核队列
            </CardTitle>
            <CardDescription>展示脱敏问题簇指标、置信信号、来源引用数与审批状态。</CardDescription>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>代表问题（脱敏）</TableHead>
                  <TableHead>提问频次 / 独立用户</TableHead>
                  <TableHead>生命周期</TableHead>
                  <TableHead>来源引用数</TableHead>
                  <TableHead>创建人 / 审核槽位</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                <TableRow>
                  <TableCell colSpan={5} className="py-8 text-center text-xs text-ink-muted">
                    等待 Slice 6 FAQ Mining 服务接入后加载真实候选队列。
                  </TableCell>
                </TableRow>
              </TableBody>
            </Table>
            <div className="mt-3 flex flex-wrap gap-2">
              <Badge variant="outline">CANDIDATE</Badge>
              <Badge variant="warning">PENDING_REVIEW</Badge>
              <Badge variant="danger">REJECTED</Badge>
              <Badge variant="neutral">ARCHIVED</Badge>
            </div>
          </CardContent>
        </Card>
      </div>
    </ModuleWorkspace>
  );
}
