import { DatabaseZap, Plus } from 'lucide-react';
import { ModuleWorkspace } from '@/components/common/ModuleWorkspace';
import {
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
 * PRD 模块 9：已发布 FAQ 管理与缓存状态。
 * 遵循 ADR-001 裁决：严格分离 Lifecycle、Publication（UNPUBLISHED / PUBLISHED / DISABLED）与 Source Health 三轴。
 */
export function FaqPublishedPage() {
  return (
    <ModuleWorkspace
      moduleCode="PRD-09"
      title="已发布 FAQ 与缓存治理"
      description="管理已发布的不可变 FAQVersion、段落级来源引用、独立 FAQ ACL 与 Redis 高速匹配缓存版本映射。"
      apiContract="GET/POST /api/v1/faq · POST /api/v1/faq/{id}/publish · POST /api/v1/faq/{id}/disable"
      governanceBadges={['三轴状态解耦', '缓存命中二次鉴权']}
      metrics={[
        { label: 'PUBLISHED FAQ', value: '--', hint: '进入受 ACL 保护的高速匹配路径' },
        { label: 'STALE / EXPIRED 来源', value: '--', hint: '不覆盖 PUBLISHED 事实，独立标记健康度' },
        { label: 'Redis 缓存代次', value: '--', hint: 'Outbox 事件驱动失效 + 周期对账' },
        { label: '手工维护（MANUAL）', value: '--', hint: '须填写事实依据、有效期与维护人' },
      ]}
      headerActions={
        <Button size="sm" disabled>
          <Plus className="h-3.5 w-3.5" aria-hidden="true" />
          创建手工 FAQ
        </Button>
      }
    >
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center justify-between gap-2">
            <span className="inline-flex items-center gap-2">
              <DatabaseZap className="h-4 w-4 text-ink-muted" aria-hidden="true" />
              FAQ 三轴状态台账
            </span>
            <div className="flex flex-wrap gap-1.5">
              <Badge variant="neutral">UNPUBLISHED</Badge>
              <Badge variant="success">PUBLISHED</Badge>
              <Badge variant="warning">DISABLED</Badge>
              <Badge variant="danger">SOURCE: STALE</Badge>
            </div>
          </CardTitle>
          <CardDescription>
            Cache Key 绑定 organization_id / space_id / faq_id / faq_version / faq_acl_revision / source_version_hash。
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>FAQ 问题 / 版本</TableHead>
                <TableHead>发布状态（Publication）</TableHead>
                <TableHead>来源健康（Source Health）</TableHead>
                <TableHead>ACL Revision</TableHead>
                <TableHead>缓存状态</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              <TableRow>
                <TableCell colSpan={5} className="py-8 text-center text-xs text-ink-muted">
                  等待 Slice 6/7 已发布 FAQ 与缓存对账接口就绪。
                </TableCell>
              </TableRow>
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </ModuleWorkspace>
  );
}
