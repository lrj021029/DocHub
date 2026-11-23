import { ShieldAlert, FileCheck2 } from 'lucide-react';
import { ModuleWorkspace } from '@/components/common/ModuleWorkspace';
import {
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
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from '@/components/ui';

/**
 * PRD 模块 13：业务审计与安全审计中心。
 * 严格分离普通业务审计与高敏安全审计（ACL Deny、跨租户越权尝试、Break-glass 与外发拦截）。
 */
export function AuditPage() {
  return (
    <ModuleWorkspace
      moduleCode="PRD-13"
      title="业务与安全审计中心"
      description="支持问答溯源审计、ACL 变更 Diff、FAQ 审批流记录以及仅限安全审计角色查看的 SecurityAuditEvent。"
      apiContract="GET /api/v1/audit/business · GET /api/v1/audit/security（Cursor 分页）"
      governanceBadges={['业务/安全审计分离', '审计读取受控']}
    >
      <Tabs defaultValue="business">
        <TabsList aria-label="审计日志分类">
          <TabsTrigger value="business">业务审计（AuditEvent）</TabsTrigger>
          <TabsTrigger value="security">安全审计（SecurityAuditEvent）</TabsTrigger>
        </TabsList>

        <TabsContent value="business">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <FileCheck2 className="h-4 w-4 text-ink-muted" aria-hidden="true" />
                业务操作与问答来源审计
              </CardTitle>
              <CardDescription>
                追踪文档导入、版本发布、FAQ 审批与问答进入模型的授权 Chunk/Citation 记录。
              </CardDescription>
            </CardHeader>
            <CardContent>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>时间</TableHead>
                    <TableHead>事件类型</TableHead>
                    <TableHead>操作主体</TableHead>
                    <TableHead>资源与版本</TableHead>
                    <TableHead>链路 ID</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  <TableRow>
                    <TableCell colSpan={5} className="py-8 text-center text-xs text-ink-muted">
                      等待审计接口接入后以 Cursor 分页展示当前 Organization 业务审计流。
                    </TableCell>
                  </TableRow>
                </TableBody>
              </Table>
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="security">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center justify-between">
                <span className="inline-flex items-center gap-2">
                  <ShieldAlert className="h-4 w-4 text-danger" aria-hidden="true" />
                  高敏安全审计事件
                </span>
                <Badge variant="danger">仅 Security Auditor 可见</Badge>
              </CardTitle>
              <CardDescription>
                记录 ACL Deny、跨租户 IDOR 拦截、Break-glass 限时访问及 L2/L3 外发策略拒绝事件。
              </CardDescription>
            </CardHeader>
            <CardContent>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>时间</TableHead>
                    <TableHead>安全事件</TableHead>
                    <TableHead>触发用户</TableHead>
                    <TableHead>ACL Revision / 策略版本</TableHead>
                    <TableHead>阻断原因</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  <TableRow>
                    <TableCell colSpan={5} className="py-8 text-center text-xs text-ink-muted">
                      需具备安全审计权限并在 Slice 5/8 接入后端后加载真实 SecurityAuditEvent。
                    </TableCell>
                  </TableRow>
                </TableBody>
              </Table>
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </ModuleWorkspace>
  );
}
