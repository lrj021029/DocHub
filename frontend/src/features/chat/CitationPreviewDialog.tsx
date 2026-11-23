import { BookOpen, Lock, MapPin, ShieldAlert } from 'lucide-react';
import {
  Alert,
  Badge,
  Button,
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Skeleton,
} from '@/components/ui';
import type { CitationDetail } from '@/features/chat/api';

export interface CitationPreviewDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  isLoading: boolean;
  errorMessage: string | null;
  citation: CitationDetail | null;
}

/** 将数组或对象形式的 Bounding Box 转换为百分比高亮坐标 */
function resolveBboxStyle(bbox: CitationDetail['bbox']): {
  left: string;
  top: string;
  width: string;
  height: string;
} | null {
  if (!bbox) return null;
  let x = 0;
  let y = 0;
  let w = 0;
  let h = 0;

  if (Array.isArray(bbox) && bbox.length === 4) {
    [x, y, w, h] = bbox;
  } else if (!Array.isArray(bbox)) {
    x = bbox.x;
    y = bbox.y;
    w = bbox.width;
    h = bbox.height;
  } else {
    return null;
  }

  // 若坐标处于 0-1 归一化区间则直接换算百分比，否则按标准 1000 归一化坐标系换算
  const scale = x <= 1 && y <= 1 && w <= 1 && h <= 1 ? 100 : 0.1;
  return {
    left: `${Math.max(0, Math.min(95, x * scale))}%`,
    top: `${Math.max(0, Math.min(95, y * scale))}%`,
    width: `${Math.max(5, Math.min(100, w * scale))}%`,
    height: `${Math.max(4, Math.min(100, h * scale))}%`,
  };
}

/**
 * 引用溯源重鉴权与 PDF 页面定位高亮弹窗。
 * 严格遵循 PRD §8.2 & TECHNICAL_SPEC §9.6：
 * - 点击引用时重新请求后端权限校验；
 * - 失权时绝不展示受限文档标题、摘要或坐标，仅展示受限提示；
 * - 具备准确页面渲染图与坐标时叠加高亮框，否则仅展示页码与章节，绝不伪造高亮。
 */
export function CitationPreviewDialog({
  open,
  onOpenChange,
  isLoading,
  errorMessage,
  citation,
}: CitationPreviewDialogProps) {
  const bboxStyle = citation?.authorized ? resolveBboxStyle(citation.bbox ?? null) : null;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="w-[min(94vw,42rem)]">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <BookOpen className="h-4 w-4 text-ink-muted" aria-hidden="true" />
            引用溯源定位（实时权限复核）
          </DialogTitle>
          <DialogDescription>
            每次打开引用卡片均由服务端依据 PostgreSQL 当前 ACL Revision 重新鉴权。
          </DialogDescription>
        </DialogHeader>

        <div className="mt-4 space-y-3">
          {isLoading ? (
            <div className="space-y-2.5" data-testid="citation-recheck-loading">
              <Skeleton shape="line" className="w-1/2" />
              <Skeleton shape="block" className="h-32 w-full" />
            </div>
          ) : errorMessage ? (
            <Alert variant="danger" title="引用鉴权请求失败">
              {errorMessage}
            </Alert>
          ) : !citation ? null : !citation.authorized ? (
            <Alert
              variant="warning"
              title="引用访问受限（ACL 实时复核未通过）"
              icon={<Lock className="h-4 w-4" />}
            >
              {citation.deniedReason ??
                '当前账号已不再具备该来源文档的访问权限，系统已隐藏受限正文与定位详情。'}
            </Alert>
          ) : (
            <>
              <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-line bg-surface-subtle px-3.5 py-2.5">
                <div className="min-w-0">
                  <p className="truncate text-sm font-semibold text-ink">
                    {citation.documentTitle}
                  </p>
                  <p className="text-xs text-ink-muted">
                    {citation.headingPath ?? '正文片段'}
                    {citation.pageNumber ? ` · 第 ${citation.pageNumber} 页` : ''}
                  </p>
                </div>
                <div className="flex items-center gap-1.5">
                  {citation.pageNumber ? (
                    <Badge variant="info" className="gap-1">
                      <MapPin className="h-3 w-3" aria-hidden="true" />
                      P.{citation.pageNumber}
                    </Badge>
                  ) : null}
                  {citation.aclRevision !== null && citation.aclRevision !== undefined ? (
                    <Badge variant="outline" className="font-mono text-[10px]">
                      ACL rev #{citation.aclRevision}
                    </Badge>
                  ) : null}
                </div>
              </div>

              {/* 原页渲染图 + Bounding Box 高亮层 */}
              {citation.pageImageUrl ? (
                <div className="relative overflow-hidden rounded-lg border border-line bg-surface-subtle">
                  <img
                    src={citation.pageImageUrl}
                    alt={`${citation.documentTitle} 第 ${citation.pageNumber ?? '?'} 页渲染图`}
                    className="max-h-72 w-full object-contain"
                  />
                  {bboxStyle ? (
                    <div
                      aria-label="引用定位高亮区域"
                      data-testid="citation-bbox-highlight"
                      style={bboxStyle}
                      className="pointer-events-none absolute rounded border-2 border-warning bg-warning/20"
                    />
                  ) : null}
                </div>
              ) : bboxStyle ? (
                <div className="relative h-28 rounded-lg border border-line bg-surface-subtle p-3">
                  <span className="text-[11px] text-ink-muted">
                    坐标定位区域（第 {citation.pageNumber ?? '-'} 页）
                  </span>
                  <div
                    data-testid="citation-bbox-highlight"
                    style={bboxStyle}
                    className="pointer-events-none absolute rounded border-2 border-warning bg-warning/20"
                  />
                </div>
              ) : (
                <Alert variant="info" icon={<ShieldAlert className="h-4 w-4" />}>
                  当前片段来自结构化文本块（{citation.pageNumber ? `第 ${citation.pageNumber} 页` : '无页码坐标'}
                  ），无独立页面坐标时绝不伪造高亮框。
                </Alert>
              )}

              {/* 引用原文片段 */}
              <div className="rounded-lg border border-line bg-surface-elevated p-3 text-xs leading-relaxed text-ink-secondary">
                <p className="mb-1 text-[11px] font-medium text-ink-muted">授权证据片段</p>
                <p className="whitespace-pre-wrap">{citation.quoteText || '（无摘要文本）'}</p>
              </div>
            </>
          )}
        </div>

        <DialogFooter>
          <DialogClose render={<Button variant="outline" size="sm">关闭</Button>} />
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
