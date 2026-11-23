import { BarChart3 } from 'lucide-react';
import { ModuleWorkspace } from '@/components/common/ModuleWorkspace';
import {
  Alert,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui';

/**
 * PRD 模块 11：数据运营看板。
 * 严格按当前 Organization 和授权范围过滤，绝不通过热门排行或侧信道暴露受限文档标题。
 */
export function AnalyticsPage() {
  const metricGroups = [
    {
      title: '访问与问答活跃度',
      items: ['PV / UV 与活跃会话数', '首 Token 延迟（TTFT）与完整响应延迟', 'Token 消耗与模型成本明细'],
    },
    {
      title: '知识生产与解析质量',
      items: ['文档数 / DocumentVersion / 有效 Chunk 数', '导入成功率、MinerU 解析失败率与 OCR 阻断率', '索引构建耗时与代次分布'],
    },
    {
      title: 'FAQ 沉淀与知识缺口闭环',
      items: ['FAQ 命中率、直接回答率与用户有用率', 'STALE / EXPIRED 来源健康占比', 'Knowledge Gap 解决率与平均处理时长'],
    },
  ];

  return (
    <ModuleWorkspace
      moduleCode="PRD-11"
      title="数据运营分析看板"
      description="按当前 Organization 授权范围统计知识资产规模、检索与模型耗时、FAQ 命中及 Knowledge Gap 闭环效率。"
      apiContract="GET /api/v1/analytics/overview · /analytics/faq · /analytics/knowledge-gaps"
      governanceBadges={['受限文档侧信道防护', '禁止伪造覆盖率']}
      metrics={[
        { label: '近 7 日 PV / UV', value: '--', hint: '按当前 Organization 隔离统计' },
        { label: 'FAQ 命中率', value: '--', hint: '仅统计通过 ACL 复核的有效命中' },
        { label: '首 Token 延迟 P95', value: '--', hint: '真实延迟待 Benchmark 与流量观测汇总' },
        { label: 'Gap 闭环解决率', value: '--', hint: '基于通过回归验收的 RESOLVED 任务' },
      ]}
    >
      <div className="space-y-4">
        <Alert variant="info" title="隐私与防侧信道原则（PRD §9 & §10.1）">
          受限文档绝不会出现在普通运营看板的热门文档榜或高频词云中；权限拒绝事件详情仅在经授权的安全审计视图展示。
        </Alert>

        <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
          {metricGroups.map((group) => (
            <Card key={group.title}>
              <CardHeader>
                <CardTitle className="flex items-center gap-2">
                  <BarChart3 className="h-4 w-4 text-ink-muted" aria-hidden="true" />
                  {group.title}
                </CardTitle>
                <CardDescription>Slice 8 接入真实聚合指标与 Grafana 观测面板</CardDescription>
              </CardHeader>
              <CardContent>
                <ul className="space-y-2 text-xs text-ink-secondary">
                  {group.items.map((item) => (
                    <li key={item} className="flex items-start gap-2">
                      <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-accent" />
                      <span>{item}</span>
                    </li>
                  ))}
                </ul>
              </CardContent>
            </Card>
          ))}
        </div>
      </div>
    </ModuleWorkspace>
  );
}
