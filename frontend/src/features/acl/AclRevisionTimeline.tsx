import { History, MinusCircle, PlusCircle } from 'lucide-react';
import {
  Alert,
  Badge,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  Skeleton,
} from '@/components/ui';
import { formatAclError, type AclRevisionItem } from '@/features/acl/api';

export interface AclRevisionTimelineProps {
  revisions: AclRevisionItem[];
  isLoading: boolean;
  isError: boolean;
  error: unknown;
}

/**
 * 不可变 ACLRevision 历史时间线与增删 Diff 视图。
 * 展示每次变更的修订号、操作人、时间、审计理由以及新增/移除的授权规则对比。
 */
export function AclRevisionTimeline({
  revisions,
  isLoading,
  isError,
  error,
}: AclRevisionTimelineProps) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <History className="h-4 w-4 text-ink-muted" aria-hidden="true" />
          ACLRevision 审计时间线与变更 Diff（{revisions.length}）
        </CardTitle>
        <CardDescription>
          每次 ACL 变更在 PostgreSQL 事务中递增 revision、写入不可变快照并通过 Outbox 同步索引投影。
        </CardDescription>
      </CardHeader>

      <CardContent>
        {isLoading ? (
          <div className="space-y-3">
            <Skeleton shape="block" className="h-20 w-full" />
            <Skeleton shape="block" className="h-20 w-full" />
          </div>
        ) : isError ? (
          <Alert variant="danger" title="加载 ACL 修订历史失败">
            {formatAclError(error, '无法获取修订时间线')}
          </Alert>
        ) : revisions.length === 0 ? (
          <p className="rounded-lg border border-dashed border-line py-8 text-center text-xs text-ink-muted">
            该文档尚无 ACL 修订历史记录（当前仍处于初始默认拒绝态）。
          </p>
        ) : (
          <ol className="space-y-3">
            {revisions.map((rev) => (
              <li
                key={rev.id}
                className="rounded-lg border border-line bg-surface-subtle p-3.5 text-xs space-y-2"
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <Badge variant="outline" className="font-mono">
                      Revision #{rev.revision}
                    </Badge>
                    <span className="font-medium text-ink">
                      {rev.changedByEmail ?? rev.changedBy}
                    </span>
                  </div>
                  <span className="font-mono text-[11px] text-ink-muted">
                    {rev.createdAt ? rev.createdAt.slice(0, 19).replace('T', ' ') : '—'}
                  </span>
                </div>

                <p className="text-ink-secondary">
                  <span className="font-medium text-ink">变更理由：</span>
                  {rev.reason}
                </p>

                {/* 增删 Diff 明细 */}
                <div className="flex flex-wrap gap-1.5 pt-0.5">
                  {rev.addedGrants.map((item, idx) => (
                    <Badge key={`add-${idx}`} variant="success" className="gap-1 font-mono text-[10px]">
                      <PlusCircle className="h-3 w-3" aria-hidden="true" />
                      {item}
                    </Badge>
                  ))}
                  {rev.removedGrants.map((item, idx) => (
                    <Badge key={`rem-${idx}`} variant="danger" className="gap-1 font-mono text-[10px]">
                      <MinusCircle className="h-3 w-3" aria-hidden="true" />
                      {item}
                    </Badge>
                  ))}
                  {rev.addedGrants.length === 0 && rev.removedGrants.length === 0 ? (
                    <span className="text-[11px] text-ink-muted">
                      规则快照总数：{rev.snapshotCount ?? 0}（无增删差异明细）
                    </span>
                  ) : null}
                </div>
              </li>
            ))}
          </ol>
        )}
      </CardContent>
    </Card>
  );
}
