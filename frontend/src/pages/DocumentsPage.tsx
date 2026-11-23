import { Upload, FileStack, ShieldAlert } from 'lucide-react';
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
 * PRD 模块 5：知识维护中心（文档台账、上传、版本、分阶段解析进度与 Chunk 预览）。
 * 严格分离 Import / Parse / Chunk / Embedding / Index 五个子状态机，严禁伪造进度百分比。
 */
export function DocumentsPage() {
  const supportedFormats = ['PDF (MinerU)', 'Markdown', 'DOCX', 'TXT', 'XLSX/XLS', 'CSV', 'JSON'];

  return (
    <ModuleWorkspace
      moduleCode="PRD-05"
      title="文档维护中心"
      description="支持单文件、批量与文件夹拖拽导入；提供不可变 DocumentVersion 管理、分阶段任务状态追踪与 Parent-Child Chunk 预览。"
      apiContract="POST /api/v1/spaces/{space_id}/documents/uploads · GET /api/v1/documents"
      governanceBadges={['新文档默认 Quarantine', 'L2/L3 扫描件 Fail-Closed']}
      metrics={[
        { label: '文档逻辑对象', value: '--', hint: '新导入文档默认隔离，未授权不可检索' },
        { label: 'MinerU 解析任务', value: '--', hint: '复杂 PDF / 扫描件 / 表格 / 公式' },
        { label: '轻量解析任务', value: '--', hint: 'MD / DOCX / TXT / Excel / CSV / JSON' },
        { label: '有效 Parent/Child Chunk', value: '--', hint: '继承所属文档 ACL 与页码坐标' },
      ]}
      headerActions={
        <>
          <Button variant="outline" size="sm" disabled>
            <FileStack className="h-3.5 w-3.5" aria-hidden="true" />
            批量导入
          </Button>
          <Button size="sm" disabled>
            <Upload className="h-3.5 w-3.5" aria-hidden="true" />
            上传文档
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <Alert variant="warning" title="L2/L3 敏感文档外发拦截保护（ADR-003）">
          当文档标记为 L2 Confidential 或 L3 Restricted 且需要扫描 PDF OCR 时，若本地 OCR
          未启用，解析任务将强制进入 BLOCKED_POLICY 状态，严禁自动降级外发至外部 MinerU 云服务。
        </Alert>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center justify-between gap-2">
              <span>文档台账与五阶段流水线状态</span>
              <div className="flex flex-wrap gap-1.5">
                {supportedFormats.map((fmt) => (
                  <Badge key={fmt} variant="neutral">
                    {fmt}
                  </Badge>
                ))}
              </div>
            </CardTitle>
            <CardDescription>
              每个文档版本独立展示 ImportJob → ParseJob → ChunkingJob → EmbeddingJob → IndexJob 真实状态。
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>文档标题 / 版本</TableHead>
                  <TableHead>敏感级别</TableHead>
                  <TableHead>Import</TableHead>
                  <TableHead>Parse</TableHead>
                  <TableHead>Chunk / Index</TableHead>
                  <TableHead>ACL 发布状态</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                <TableRow>
                  <TableCell colSpan={6} className="py-8 text-center text-xs text-ink-muted">
                    <span className="inline-flex items-center gap-1.5">
                      <ShieldAlert className="h-3.5 w-3.5" aria-hidden="true" />
                      等待 Slice 2/3 文档上传与解析管道接入后展示真实台账与 Chunk 预览抽屉。
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
