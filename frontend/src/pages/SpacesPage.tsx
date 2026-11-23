import { FolderPlus, Lock } from 'lucide-react';
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
 * PRD 模块 4：Knowledge Space 管理与成员入口授权。
 * Space 入口授权与 Document ACL 为两层独立校验；支持每 Space 独立 Embedding Profile 与回答策略。
 */
export function SpacesPage() {
  return (
    <ModuleWorkspace
      moduleCode="PRD-04"
      title="Knowledge Space 管理"
      description="管理当前 Organization 下的知识空间、公开/私有入口策略、私有 Space 成员清单、Embedding Profile 及回答策略。"
      apiContract="GET/POST/PATCH /api/v1/organizations/{org_id}/spaces · /spaces/{space_id}/members"
      governanceBadges={['两层独立鉴权', 'Space 级 Embedding Profile']}
      metrics={[
        { label: '活跃空间数', value: '--', hint: 'Global 作用域仅限于当前 Space 入口内' },
        { label: '私有入口空间', value: '--', hint: '需显式 SpaceMembership 授权' },
        { label: '严格证据模式', value: '--', hint: 'STRICT_EVIDENCE 默认策略' },
        { label: '默认 ACL 模板', value: '--', hint: '高敏感文档仍需管理员确认' },
      ]}
      headerActions={
        <Button size="sm" disabled>
          <FolderPlus className="h-3.5 w-3.5" aria-hidden="true" />
          新建 Knowledge Space
        </Button>
      }
    >
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Lock className="h-4 w-4 text-ink-muted" aria-hidden="true" />
            知识空间台账与策略概览
          </CardTitle>
          <CardDescription>
            支持多 Space 并行检索与跨空间排名 RRF 融合；绝不直接比较异构 Embedding 原始分数。
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Space 名称</TableHead>
                <TableHead>入口可见性</TableHead>
                <TableHead>Embedding Profile</TableHead>
                <TableHead>回答策略</TableHead>
                <TableHead>策略版本</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              <TableRow>
                <TableCell colSpan={5} className="py-8 text-center text-xs text-ink-muted">
                  等待 Slice 2 Knowledge Space API 返回当前组织下的受授权空间列表。
                </TableCell>
              </TableRow>
            </TableBody>
          </Table>
          <div className="flex flex-wrap gap-2">
            <Badge variant="info">STRICT_EVIDENCE</Badge>
            <Badge variant="outline">GROUNDED_WITH_GENERAL_CONTEXT</Badge>
          </div>
        </CardContent>
      </Card>
    </ModuleWorkspace>
  );
}
