import { useState } from 'react';
import { History, Plus, ShieldCheck } from 'lucide-react';
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
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
  Input,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui';

/**
 * PRD 模块 6：文档权限 ACL 配置（Global / Department / Role / User 四维 Allow OR）。
 * 遵循 ADR-001 裁决：写操作统一收敛至 POST /api/v1/documents/{document_id}/acl/revisions，
 * 必须携带 expected_revision、reason 与 Idempotency-Key。
 */
export function AclPage() {
  const [reasonDraft, setReasonDraft] = useState('');

  const dimensions = [
    { code: 'GLOBAL', name: '空间全局（Global）', desc: '仅覆盖当前 Knowledge Space 入口内的有效用户，非平台公开' },
    { code: 'DEPARTMENT', name: '部门维度（Department）', desc: '支持 include_descendants 与 DYNAMIC / SNAPSHOT 继承模式' },
    { code: 'ROLE', name: '组织角色（OrganizationRole）', desc: '仅允许引用当前 Organization 内的业务角色' },
    { code: 'USER', name: '个人授权（User）', desc: '支持永久或限时授权（valid_until 到期即时拒绝）' },
  ];

  return (
    <ModuleWorkspace
      moduleCode="PRD-06"
      title="文档权限 ACL 配置"
      description="默认拒绝，四维 Allow OR 授权判定。每次变更生成不可变 ACLRevision 快照与审计 Diff，经 Outbox 同步索引投影。"
      apiContract="GET /api/v1/documents/{id}/acl · POST /api/v1/documents/{id}/acl/revisions"
      governanceBadges={['默认拒绝', '四维 Allow OR', '单一 Revision 写入口']}
      headerActions={
        <Dialog>
          <DialogTrigger
            render={
              <Button size="sm">
                <Plus className="h-3.5 w-3.5" aria-hidden="true" />
                变更 ACL Revision 说明
              </Button>
            }
          />
          <DialogContent>
            <DialogHeader>
              <DialogTitle>ACL Revision 变更契约预览</DialogTitle>
              <DialogDescription>
                依据 ADR-001，所有 ACL 写操作必须通过单一 revision 端点提交，携带乐观锁版本号与变更理由。
              </DialogDescription>
            </DialogHeader>
            <div className="mt-4 space-y-3">
              <div className="space-y-1">
                <label htmlFor="acl-reason-preview" className="text-xs font-medium text-ink">
                  变更理由（必填审计字段）
                </label>
                <Input
                  id="acl-reason-preview"
                  value={reasonDraft}
                  onChange={(event) => setReasonDraft(event.target.value)}
                  placeholder="例如：根据 HR 部门调整，收回离职兼职成员临时授权"
                />
              </div>
              <p className="font-mono text-[11px] text-ink-muted">
                POST /api/v1/documents/&#123;document_id&#125;/acl/revisions · Header: Idempotency-Key
              </p>
            </div>
            <DialogFooter>
              <DialogClose render={<Button variant="outline" size="sm">关闭</Button>} />
              <Button size="sm" disabled>
                待绑定具体文档后提交
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      }
    >
      <div className="space-y-4">
        <Alert variant="info" title="权威鉴权保障（PostgreSQL Final Check）">
          即使 Qdrant Payload 异步同步存在延迟窗口，所有检索与问答请求在进入 Reranker 与 LLM
          前都会依据 PostgreSQL 当前 ACL Revision 强制批量复核，彻底杜绝撤权窗口越权。
        </Alert>

        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          {dimensions.map((dim) => (
            <Card key={dim.code}>
              <CardHeader className="py-3">
                <CardTitle className="flex items-center justify-between">
                  <span>{dim.name}</span>
                  <Badge variant="outline" className="font-mono">
                    {dim.code}
                  </Badge>
                </CardTitle>
              </CardHeader>
              <CardContent className="py-3">
                <CardDescription>{dim.desc}</CardDescription>
              </CardContent>
            </Card>
          ))}
        </div>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <History className="h-4 w-4 text-ink-muted" aria-hidden="true" />
              ACL Revision 审计历史
            </CardTitle>
            <CardDescription>记录每次授权快照、操作人、时间、变更理由与 Diff。</CardDescription>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Revision</TableHead>
                  <TableHead>操作人</TableHead>
                  <TableHead>变更理由</TableHead>
                  <TableHead>授权主体摘要</TableHead>
                  <TableHead>变更时间</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                <TableRow>
                  <TableCell colSpan={5} className="py-8 text-center text-xs text-ink-muted">
                    <span className="inline-flex items-center gap-1.5">
                      <ShieldCheck className="h-3.5 w-3.5" aria-hidden="true" />
                      请选择具体文档后加载 GET /api/v1/documents/&#123;document_id&#125;/acl/revisions 历史记录。
                    </span>
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
