import { useMemo, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  ArrowUpDown,
  ChevronLeft,
  ChevronRight,
  FileText,
  Inbox,
  RefreshCw,
  Search,
  ShieldAlert,
  ShieldCheck,
} from 'lucide-react';
import { Link } from 'react-router-dom';
import {
  Alert,
  Badge,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  Input,
  Skeleton,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui';
import {
  fetchDocuments,
  formatDocumentError,
  type DocumentLedgerItem,
} from '@/features/documents/api';
import { DocumentImportPanel } from '@/features/documents/DocumentImportPanel';
import { ProcessingStatusDrawer } from '@/features/documents/ProcessingStatusDrawer';
import { fetchKnowledgeSpaces } from '@/features/spaces/api';
import { useAuth } from '@/lib/auth-store';

const PAGE_SIZE = 10;

/**
 * PRD 模块 5（Slice 2 & 3 真实对接）：文档维护与处理中心。
 * 包含多源导入（单文件/批量/文件夹拖拽）、带筛选排序分页的文档台账、
 * 醒目的 QUARANTINED / L2 / L3 标识、真实五阶段处理进度抽屉及 Parent-Child 切片预览。
 */
export function DocumentsPage() {
  const queryClient = useQueryClient();
  const { currentOrgId, hasPermission } = useAuth();

  const [spaceFilter, setSpaceFilter] = useState<string>('ALL');
  const [statusFilter, setStatusFilter] = useState<string>('ALL');
  const [sensitivityFilter, setSensitivityFilter] = useState<string>('ALL');
  const [searchKeyword, setSearchKeyword] = useState<string>('');
  const [sortBy, setSortBy] = useState<'created_at' | 'title' | 'sensitivity_level'>('created_at');
  const [sortOrder, setSortOrder] = useState<'asc' | 'desc'>('desc');
  const [page, setPage] = useState<number>(1);
  const [selectedDocId, setSelectedDocId] = useState<string | null>(null);

  // 前端仅用于体验层按钮显隐；后端对所有上传、重处理与发布请求强制执行二次鉴权
  const canManageDocuments =
    hasPermission('document:create') ||
    hasPermission('document:write') ||
    hasPermission('document:manage') ||
    hasPermission('org:manage');

  const spacesQuery = useQuery({
    queryKey: ['spaces', currentOrgId],
    queryFn: () => fetchKnowledgeSpaces(currentOrgId!),
    enabled: Boolean(currentOrgId),
  });

  const documentsQuery = useQuery({
    queryKey: [
      'documents',
      currentOrgId,
      spaceFilter,
      statusFilter,
      sensitivityFilter,
      searchKeyword,
      sortBy,
      sortOrder,
      page,
    ],
    queryFn: () =>
      fetchDocuments(currentOrgId!, {
        spaceId: spaceFilter,
        status: statusFilter,
        sensitivityLevel: sensitivityFilter,
        search: searchKeyword,
        sortBy,
        sortOrder,
        offset: (page - 1) * PAGE_SIZE,
        limit: PAGE_SIZE,
      }),
    enabled: Boolean(currentOrgId),
  });

  const spaces = spacesQuery.data ?? [];
  const spaceNameMap = useMemo(
    () => new Map(spaces.map((s) => [s.id, s.name])),
    [spaces],
  );

  const items = documentsQuery.data?.items ?? [];
  const total = documentsQuery.data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const selectedDoc: DocumentLedgerItem | null =
    items.find((d) => d.id === selectedDocId) ?? items[0] ?? null;

  const toggleSort = (field: 'created_at' | 'title' | 'sensitivity_level') => {
    if (sortBy === field) {
      setSortOrder((prev) => (prev === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortBy(field);
      setSortOrder('asc');
    }
    setPage(1);
  };

  const refreshAll = () => {
    void queryClient.invalidateQueries({ queryKey: ['documents', currentOrgId] });
  };

  return (
    <div className="space-y-5">
      {/* 页头 */}
      <div className="flex flex-col gap-3 border-b border-line pb-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="space-y-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="outline" className="font-mono text-[10px]">
              PRD-05
            </Badge>
            <h1 className="text-xl font-semibold tracking-tight text-ink">文档维护与处理中心</h1>
            <Badge variant="warning">新文档默认 QUARANTINED</Badge>
            <Badge variant="danger">L2/L3 禁止默认外发 MinerU</Badge>
          </div>
          <p className="text-sm text-ink-secondary">
            统一管理多源导入、不可变文档版本、五阶段真实处理进度（Import / Parse / Chunk / Embedding / Index）与切片发布。
          </p>
        </div>

        <Button
          variant="outline"
          size="sm"
          onClick={() => void documentsQuery.refetch()}
          disabled={documentsQuery.isFetching}
        >
          <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
          刷新台账
        </Button>
      </div>

      {!currentOrgId ? (
        <Alert variant="warning" title="尚未绑定当前 Organization">
          请先选择当前组织后再访问文档台账与导入功能。
        </Alert>
      ) : (
        <div className="space-y-5">
          {/* 多源导入面板 */}
          {canManageDocuments ? (
            <DocumentImportPanel
              orgId={currentOrgId}
              spaces={spaces}
              onImportSuccess={refreshAll}
            />
          ) : null}

          {/* 文档台账表格 */}
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <FileText className="h-4 w-4 text-ink-muted" aria-hidden="true" />
                文档台账（共 {total} 条）
              </CardTitle>
              <CardDescription>
                点击任意文档行可在下方展开五阶段真实处理状态、MinerU 任务 ID、Block / Chunk 预览与发布入口。
              </CardDescription>
            </CardHeader>

            <CardContent className="space-y-4">
              {/* 筛选栏 */}
              <div className="grid grid-cols-1 gap-2.5 md:grid-cols-4">
                <div className="relative">
                  <Search
                    className="pointer-events-none absolute top-2.5 left-3 h-4 w-4 text-ink-muted"
                    aria-hidden="true"
                  />
                  <Input
                    aria-label="搜索文档标题"
                    placeholder="搜索文档标题..."
                    value={searchKeyword}
                    onChange={(event) => {
                      setSearchKeyword(event.target.value);
                      setPage(1);
                    }}
                    className="pl-9"
                  />
                </div>

                <select
                  aria-label="按 Space 筛选"
                  value={spaceFilter}
                  onChange={(event) => {
                    setSpaceFilter(event.target.value);
                    setPage(1);
                  }}
                  className="flex h-9 w-full rounded-lg border border-line bg-surface-elevated px-3 text-sm text-ink shadow-xs"
                >
                  <option value="ALL">全部 Knowledge Space</option>
                  {spaces.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name}
                    </option>
                  ))}
                </select>

                <select
                  aria-label="按文档状态筛选"
                  value={statusFilter}
                  onChange={(event) => {
                    setStatusFilter(event.target.value);
                    setPage(1);
                  }}
                  className="flex h-9 w-full rounded-lg border border-line bg-surface-elevated px-3 text-sm text-ink shadow-xs"
                >
                  <option value="ALL">全部状态</option>
                  <option value="QUARANTINED">QUARANTINED（待授权隔离）</option>
                  <option value="ACTIVE">ACTIVE（已激活可检索）</option>
                  <option value="ARCHIVED">ARCHIVED（已归档）</option>
                </select>

                <select
                  aria-label="按敏感等级筛选"
                  value={sensitivityFilter}
                  onChange={(event) => {
                    setSensitivityFilter(event.target.value);
                    setPage(1);
                  }}
                  className="flex h-9 w-full rounded-lg border border-line bg-surface-elevated px-3 text-sm text-ink shadow-xs"
                >
                  <option value="ALL">全部敏感等级（L0-L3）</option>
                  <option value="L0">L0 Public</option>
                  <option value="L1">L1 Internal</option>
                  <option value="L2">L2 Confidential</option>
                  <option value="L3">L3 Restricted</option>
                </select>
              </div>

              {/* 表格主体 */}
              {documentsQuery.isLoading ? (
                <div className="space-y-2.5" data-testid="documents-loading">
                  <Skeleton shape="block" className="h-12 w-full" />
                  <Skeleton shape="block" className="h-12 w-full" />
                </div>
              ) : documentsQuery.isError ? (
                <Alert variant="danger" title="加载文档台账失败">
                  {formatDocumentError(documentsQuery.error, '无法读取文档列表')}
                </Alert>
              ) : items.length === 0 ? (
                <div className="flex flex-col items-center justify-center rounded-lg border border-dashed border-line py-10 text-center">
                  <Inbox className="h-5 w-5 text-ink-muted" aria-hidden="true" />
                  <p className="mt-2 text-sm font-medium text-ink">当前筛选条件下暂无文档</p>
                  <p className="mt-1 text-xs text-ink-muted">
                    可在上方多源导入面板上传文档，或调整 Space / 状态 / 敏感等级筛选条件。
                  </p>
                </div>
              ) : (
                <>
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>
                          <button
                            type="button"
                            className="inline-flex items-center gap-1 hover:text-ink"
                            onClick={() => toggleSort('title')}
                          >
                            文档标题 / 版本
                            <ArrowUpDown className="h-3 w-3" aria-hidden="true" />
                          </button>
                        </TableHead>
                        <TableHead>归属 Space</TableHead>
                        <TableHead>
                          <button
                            type="button"
                            className="inline-flex items-center gap-1 hover:text-ink"
                            onClick={() => toggleSort('sensitivity_level')}
                          >
                            敏感等级
                            <ArrowUpDown className="h-3 w-3" aria-hidden="true" />
                          </button>
                        </TableHead>
                        <TableHead>文档状态</TableHead>
                        <TableHead>解析状态</TableHead>
                        <TableHead className="text-right">快捷操作</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {items.map((doc) => {
                        const isHighSens =
                          doc.sensitivityLevel === 'L2' || doc.sensitivityLevel === 'L3';
                        const isSelected = selectedDoc?.id === doc.id;
                        return (
                          <TableRow
                            key={doc.id}
                            data-selected={isSelected}
                            className="cursor-pointer"
                            onClick={() => setSelectedDocId(doc.id)}
                          >
                            <TableCell>
                              <p className="font-medium text-ink">{doc.title}</p>
                              <p className="font-mono text-[11px] text-ink-muted">
                                v{doc.currentVersionNo} · {doc.sourceType}
                              </p>
                            </TableCell>
                            <TableCell className="text-xs text-ink-secondary">
                              {doc.spaceName ?? spaceNameMap.get(doc.spaceId) ?? doc.spaceId}
                            </TableCell>
                            <TableCell>
                              <div className="flex flex-wrap items-center gap-1">
                                <Badge variant={isHighSens ? 'danger' : 'outline'}>
                                  {doc.sensitivityLevel}
                                </Badge>
                                {isHighSens ? (
                                  <span className="inline-flex items-center gap-0.5 text-[10px] text-danger">
                                    <ShieldAlert className="h-3 w-3" aria-hidden="true" />
                                    禁外发 MinerU
                                  </span>
                                ) : null}
                              </div>
                            </TableCell>
                            <TableCell>
                              <Badge
                                variant={
                                  doc.status === 'ACTIVE'
                                    ? 'success'
                                    : doc.status === 'QUARANTINED'
                                      ? 'warning'
                                      : 'neutral'
                                }
                              >
                                {doc.status === 'QUARANTINED'
                                  ? 'QUARANTINED（未授权不可检索）'
                                  : doc.status}
                              </Badge>
                            </TableCell>
                            <TableCell>
                              {doc.latestParseStatus ? (
                                <Badge
                                  variant={
                                    doc.latestParseStatus === 'BLOCKED_POLICY' ||
                                    doc.latestParseStatus === 'FAILED'
                                      ? 'danger'
                                      : doc.latestParseStatus === 'SUCCEEDED'
                                        ? 'success'
                                        : 'info'
                                  }
                                  className="font-mono text-[10px]"
                                >
                                  {doc.latestParseStatus}
                                </Badge>
                              ) : (
                                <span className="text-xs text-ink-muted">点击查看</span>
                              )}
                            </TableCell>
                            <TableCell className="text-right">
                              <Link
                                to={`/acl?documentId=${encodeURIComponent(doc.id)}`}
                                onClick={(event) => event.stopPropagation()}
                              >
                                <Button variant="outline" size="sm">
                                  <ShieldCheck className="h-3.5 w-3.5" aria-hidden="true" />
                                  ACL
                                </Button>
                              </Link>
                            </TableCell>
                          </TableRow>
                        );
                      })}
                    </TableBody>
                  </Table>

                  {/* 分页控制 */}
                  <div className="flex items-center justify-between pt-2 text-xs text-ink-muted">
                    <span>
                      第 {page} / {totalPages} 页（共 {total} 条文档）
                    </span>
                    <div className="flex items-center gap-1.5">
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={page <= 1}
                        onClick={() => setPage((p) => Math.max(1, p - 1))}
                      >
                        <ChevronLeft className="h-3.5 w-3.5" aria-hidden="true" />
                        上一页
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={page >= totalPages}
                        onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                      >
                        下一页
                        <ChevronRight className="h-3.5 w-3.5" aria-hidden="true" />
                      </Button>
                    </div>
                  </div>
                </>
              )}
            </CardContent>
          </Card>

          {/* 选中文档的五阶段真实处理进度与切片预览 */}
          {selectedDoc ? (
            <ProcessingStatusDrawer
              key={selectedDoc.id}
              orgId={currentOrgId}
              document={selectedDoc}
              canManage={canManageDocuments}
              onStatusMutated={refreshAll}
            />
          ) : null}
        </div>
      )}
    </div>
  );
}
