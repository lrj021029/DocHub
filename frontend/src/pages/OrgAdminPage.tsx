import { Building2, Users, ShieldAlert } from 'lucide-react';
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
 * PRD 模块 3：Organization / Department / User / Role 管理。
 * 强调 SystemRole 与 OrganizationRole 分离、部门 Closure Tree 与多部门兼职有效期治理。
 */
export function OrgAdminPage() {
  return (
    <ModuleWorkspace
      moduleCode="PRD-03"
      title="组织、部门与角色管理"
      description="治理多 Organization 成员、部门层级树、多部门兼职有效期以及 SystemRole 与 OrganizationRole 的权限边界。"
      apiContract="GET/POST/PATCH /api/v1/organizations/{org_id}/members · /departments"
      governanceBadges={['SystemRole ≠ 知识读权', '多部门并集授权']}
      metrics={[
        { label: '组织成员', value: '--', hint: '按当前 Organization 严格隔离' },
        { label: '部门树节点', value: '--', hint: 'PostgreSQL Closure Table 维护' },
        { label: '组织业务角色', value: '--', hint: '仅组织角色可作为 Document ACL 主体' },
        { label: '限时成员关系', value: '--', hint: '到期后实时失效，拒绝授权' },
      ]}
      headerActions={
        <>
          <Button variant="outline" size="sm" disabled>
            <Building2 className="h-3.5 w-3.5" aria-hidden="true" />
            新建部门
          </Button>
          <Button size="sm" disabled>
            <Users className="h-3.5 w-3.5" aria-hidden="true" />
            邀请成员
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <Alert variant="warning" title="权限分离硬约束（PRD §3.3）">
          SystemAdmin 仅管理基础设施与平台配置，绝不自动获得任何 Document 读取权限；紧急访问必须经独立限时
          Break-glass 流程并完整审计。
        </Alert>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <ShieldAlert className="h-4 w-4 text-ink-muted" aria-hidden="true" />
              成员与角色模型契约表
            </CardTitle>
            <CardDescription>
              Slice 1 接入后端后将渲染真实组织成员、主/兼职部门及角色有效期列表。
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>成员邮箱</TableHead>
                  <TableHead>主部门 / 兼职部门</TableHead>
                  <TableHead>OrganizationRole</TableHead>
                  <TableHead>有效期（valid_until）</TableHead>
                  <TableHead>状态</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                <TableRow>
                  <TableCell colSpan={5} className="py-8 text-center text-xs text-ink-muted">
                    等待 Slice 1 组织成员接口数据（当前仅展示表结构契约，不填充虚假成员记录）。
                  </TableCell>
                </TableRow>
              </TableBody>
            </Table>
          </CardContent>
        </Card>

        <div className="flex flex-wrap gap-2 text-xs text-ink-muted">
          <Badge variant="outline">DepartmentClosure 支持动态/快照继承</Badge>
          <Badge variant="outline">RLS 事务级 SET LOCAL organization_id</Badge>
        </div>
      </div>
    </ModuleWorkspace>
  );
}
