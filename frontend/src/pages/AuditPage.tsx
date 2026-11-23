import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { ChevronRight, FileCheck2, Inbox, RefreshCw, ShieldAlert } from 'lucide-react';
import {
  Alert,
  Badge,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  Skeleton,
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
import { fetchAuditEvents, formatGovError } from '@/features/governance/api';
import { useAuth } from '@/lib/auth-store';

/**
 * PRD 模块 13（Slice 8 真实对接）：业务审计与安全审计分离中心。
 * 严格分离 GET /api/v1/audit/events 与 GET /api/v1/audit/security-events，采用 Cursor 分页。
 */
export function AuditPage() {
  const { currentOrgId, hasPermission } = useAuth();
  const [activeTab, setActiveTab] = useState<'business' | 'security'>('business');
  const [businessCursor, setBusinessCursor] = useState<string | null>(null);
  const [securityCursor, setSecurityCursor] = useState<string | null>(null);

  const canReadBusinessAudit = hasPermission('audit:read');
  const canReadSecurityAudit = hasPermission('security_audit:read');

  const businessQuery = useQuery({
    queryKey: ['audit', currentOrgId, 'business', businessCursor],
    queryFn: () => fetchAuditEvents(currentOrgId!, 'business', businessCursor),
    enabled: Boolean(currentOrgId && activeTab === 'business' && canReadBusinessAudit),
  });

  const securityQuery = useQuery({
    queryKey: ['audit', currentOrgId, 'security', securityCursor],
    queryFn: () => fetchAuditEvents(currentOrgId!, 'security', securityCursor),
    enabled: Boolean(currentOrgId && activeTab === 'security' && canReadSecurityAudit),
  });

  const renderTable = (
    kind: 'business' | 'security',
    query: typeof businessQuery,
    onNextCursor: (cursor: string | null) => void,
    onResetCursor: () => void,
  ) => {
    if (query.isLoading) {
      return (
        <div className="space-y-2.5">
          <Skeleton shape="block" className="h-12 w-full" />
          <Skeleton shape="block" className="h-12 w-full" />
        </div>
      );
    }
    if (query.isError) {
      return (
        <Alert variant="danger" title="加载审计日志失败">
          {formatGovError(query.error, '无权或无法读取审计流')}
        </Alert>
      );
    }
    const items = query.data?.items ?? [];
    if (items.length === 0) {
      return (
        <div className="flex flex-col items-center justify-center rounded-lg border border-dashed border-line py-10 text-center">
          <Inbox className="h-5 w-5 text-ink-muted" aria-hidden="true" />
          <p className="mt-2 text-xs font-medium text-ink">当前暂无审计记录</p>
        </div>
      );
    }

    return (
      <div className="space-y-3">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>时间</TableHead>
              <TableHead>事件类型</TableHead>
              <TableHead>操作主体</TableHead>
              <TableHead>受影响资源</TableHead>
              <TableHead>{kind === 'security' ? '安全原因 / 严重度' : '链路追踪 ID'}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {items.map((item) => (
              <TableRow key={item.id}>
                <TableCell className="font-mono text-xs text-ink-muted">
                  {item.createdAt ? item.createdAt.slice(0, 19).replace('T', ' ') : '—'}
                </TableCell>
                <TableCell>
                  <Badge variant={kind === 'security' ? 'danger' : 'outline'} className="font-mono text-[10px]">
                    {item.eventType}
                  </Badge>
                </TableCell>
                <TableCell className="text-xs font-medium text-ink">{item.actorEmail}</TableCell>
                <TableCell className="font-mono text-xs text-ink-secondary">
                  {item.resourceSummary}
                </TableCell>
                <TableCell className="text-xs text-ink-secondary">
                  {kind === 'security'
                    ? `${item.severity ?? 'HIGH'} · ${item.reason ?? '策略拦截'}`
                    : item.requestId ?? '—'}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>

        <div className="flex items-center justify-end gap-2">
          <Button variant="outline" size="sm" onClick={onResetCursor}>
            回到首页
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={!query.data?.nextCursor}
            onClick={() => onNextCursor(query.data?.nextCursor ?? null)}
          >
            下一页（Cursor）
            <ChevronRight className="h-3.5 w-3.5" aria-hidden="true" />
          </Button>
        </div>
      </div>
    );
  };

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-3 border-b border-line pb-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="space-y-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="outline" className="font-mono text-[10px]">
              PRD-13
            </Badge>
            <h1 className="text-xl font-semibold tracking-tight text-ink">业务与安全审计中心</h1>
            <Badge variant="neutral">Cursor 分页</Badge>
            <Badge variant="danger">安全日志读权限受控</Badge>
          </div>
          <p className="text-sm text-ink-secondary">
            物理分离普通业务操作审计（AuditEvent）与高敏安全审计（SecurityAuditEvent：ACL Deny、重放攻击、外发拦截与 Break-glass）。
          </p>
        </div>

        <Button
          variant="outline"
          size="sm"
          onClick={() =>
            void (activeTab === 'business' ? businessQuery.refetch() : securityQuery.refetch())
          }
        >
          <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
          刷新
        </Button>
      </div>

      <Tabs
        value={activeTab}
        onValueChange={(val) => setActiveTab(val as 'business' | 'security')}
      >
        <TabsList aria-label="审计类型切换">
          <TabsTrigger value="business" className="gap-1.5">
            <FileCheck2 className="h-3.5 w-3.5" aria-hidden="true" />
            业务审计（AuditEvent）
          </TabsTrigger>
          <TabsTrigger value="security" className="gap-1.5">
            <ShieldAlert className="h-3.5 w-3.5" aria-hidden="true" />
            安全审计（SecurityAuditEvent）
          </TabsTrigger>
        </TabsList>

        <TabsContent value="business">
          <Card>
            <CardHeader>
              <CardTitle>业务操作与问答来源审计</CardTitle>
              <CardDescription>GET /api/v1/audit/events</CardDescription>
            </CardHeader>
            <CardContent>
              {!canReadBusinessAudit ? (
                <Alert variant="warning" title="当前角色无权查看业务审计日志">
                  业务操作审计（audit:read）仅对知识库管理员、系统管理员与安全审计员开放；后端接口同样强制校验该权限。
                </Alert>
              ) : (
                renderTable(
                  'business',
                  businessQuery,
                  (c) => setBusinessCursor(c),
                  () => setBusinessCursor(null),
                )
              )}
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="security">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center justify-between">
                <span>高敏安全审计事件</span>
                <Badge variant="danger">仅 Security Auditor 可见</Badge>
              </CardTitle>
              <CardDescription>
                GET /api/v1/audit/security-events · 涵盖 ACL Deny、Refresh 重放整族吊销、外发策略拒绝与 Break-glass。
              </CardDescription>
            </CardHeader>
            <CardContent>
              {!canReadSecurityAudit ? (
                <Alert variant="warning" title="高敏安全审计仅对安全审计员开放">
                  依据职责分离安全红线，安全审计事件（security_audit:read）仅授权给安全审计员（如 auditor@acme.local），普通管理员不可查阅，后端同样强制拦截。
                </Alert>
              ) : (
                renderTable(
                  'security',
                  securityQuery,
                  (c) => setSecurityCursor(c),
                  () => setSecurityCursor(null),
                )
              )}
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  );
}
