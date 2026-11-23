import { useRef, useState, type DragEvent, type InputHTMLAttributes } from 'react';
import { useMutation } from '@tanstack/react-query';
import { FileStack, FolderUp, ShieldAlert, Upload, X } from 'lucide-react';
import {
  Alert,
  Badge,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui';
import {
  createImportBatch,
  formatDocumentError,
  uploadSingleDocument,
} from '@/features/documents/api';
import type { KnowledgeSpaceItem } from '@/features/spaces/api';
import type { SensitivityLevel } from '@/types/domain';

/** 扩展标准 file input 属性以支持目录选择（webkitdirectory） */
type DirectoryInputProps = InputHTMLAttributes<HTMLInputElement> & {
  webkitdirectory?: string;
  directory?: string;
};

export interface DocumentImportPanelProps {
  orgId: string;
  spaces: KnowledgeSpaceItem[];
  onImportSuccess: () => void;
}

const ACCEPTED_EXTENSIONS = '.pdf,.md,.markdown,.docx,.txt,.xlsx,.xls,.csv,.json';

/**
 * 多源文档导入面板：
 * 支持单文件上传、多文件批量导入及文件夹拖拽/目录选择，支持指定目标 Space 与 L0-L3 敏感等级。
 */
export function DocumentImportPanel({
  orgId,
  spaces,
  onImportSuccess,
}: DocumentImportPanelProps) {
  const [targetSpaceId, setTargetSpaceId] = useState<string>(spaces[0]?.id ?? '');
  const [sensitivity, setSensitivity] = useState<SensitivityLevel>('L1');
  const [queuedFiles, setQueuedFiles] = useState<File[]>([]);
  const [isDragging, setIsDragging] = useState(false);
  const [feedback, setFeedback] = useState<{ variant: 'success' | 'danger'; message: string } | null>(
    null,
  );

  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const folderInputRef = useRef<HTMLInputElement | null>(null);

  const effectiveSpaceId = targetSpaceId || spaces[0]?.id || '';

  const appendFiles = (list: FileList | File[]) => {
    const next = Array.from(list).filter((f) => f.size >= 0);
    if (next.length === 0) return;
    setQueuedFiles((prev) => [...prev, ...next]);
    setFeedback(null);
  };

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setIsDragging(false);
    if (event.dataTransfer.files?.length) {
      appendFiles(event.dataTransfer.files);
    }
  };

  const uploadMutation = useMutation({
    mutationFn: async () => {
      if (!effectiveSpaceId) {
        throw new Error('请先选择目标 Knowledge Space');
      }
      if (queuedFiles.length === 0) {
        throw new Error('请先选择或拖拽至少一个文档文件');
      }
      if (queuedFiles.length === 1) {
        const single = queuedFiles[0]!;
        await uploadSingleDocument(orgId, effectiveSpaceId, single, sensitivity);
        return `已提交单文件「${single.name}」至 Quarantine 待授权队列。`;
      }
      const batch = await createImportBatch(orgId, effectiveSpaceId, queuedFiles, sensitivity);
      return `已创建批量导入任务，共接收 ${batch.acceptedCount} 个文件（全部默认进入 Quarantine）。`;
    },
    onSuccess: (msg) => {
      setFeedback({ variant: 'success', message: msg });
      setQueuedFiles([]);
      onImportSuccess();
    },
    onError: (err) => {
      setFeedback({ variant: 'danger', message: formatDocumentError(err, '文档导入失败') });
    },
  });

  const folderExtraProps: DirectoryInputProps = {
    webkitdirectory: '',
    directory: '',
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center justify-between gap-2">
          <span className="inline-flex items-center gap-2">
            <Upload className="h-4 w-4 text-ink-muted" aria-hidden="true" />
            多源文档导入（单文件 / 批量 / 文件夹拖拽）
          </span>
          <Badge variant="outline">新文档默认 Quarantine</Badge>
        </CardTitle>
        <CardDescription>
          支持 PDF、Markdown、DOCX、TXT、Excel、CSV、JSON。对象键由后端安全生成，上传完成后自动校验 SHA-256 与 MIME。
        </CardDescription>
      </CardHeader>

      <CardContent className="space-y-4">
        {feedback ? <Alert variant={feedback.variant}>{feedback.message}</Alert> : null}

        {/* 目标 Space 与敏感等级选择 */}
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          <div className="space-y-1">
            <label htmlFor="import-space-select" className="text-xs font-medium text-ink">
              目标 Knowledge Space
            </label>
            <select
              id="import-space-select"
              value={effectiveSpaceId}
              onChange={(event) => setTargetSpaceId(event.target.value)}
              className="flex h-9 w-full rounded-lg border border-line bg-surface-elevated px-3 text-sm text-ink shadow-xs"
            >
              {spaces.length === 0 ? (
                <option value="">暂无可用 Space，请先创建</option>
              ) : (
                spaces.map((space) => (
                  <option key={space.id} value={space.id}>
                    {space.name}（{space.visibility}）
                  </option>
                ))
              )}
            </select>
          </div>

          <div className="space-y-1">
            <label htmlFor="import-sensitivity-select" className="text-xs font-medium text-ink">
              敏感等级（L0-L3 外发策略门控）
            </label>
            <select
              id="import-sensitivity-select"
              value={sensitivity}
              onChange={(event) => setSensitivity(event.target.value as SensitivityLevel)}
              className="flex h-9 w-full rounded-lg border border-line bg-surface-elevated px-3 text-sm text-ink shadow-xs"
            >
              <option value="L0">L0 Public（公开，允许外部解析/模型）</option>
              <option value="L1">L1 Internal（内部，需组织授权后允许外部）</option>
              <option value="L2">L2 Confidential（机密，禁止默认外发 MinerU）</option>
              <option value="L3">L3 Restricted（受限，严格禁止任何外部发送）</option>
            </select>
          </div>
        </div>

        {(sensitivity === 'L2' || sensitivity === 'L3') ? (
          <Alert variant="warning" icon={<ShieldAlert className="h-4 w-4" />}>
            已选择 <strong>{sensitivity}</strong> 敏感等级：扫描 PDF 将严格执行 Fail-Closed 外发拦截；
            若未启用合规本地 OCR，解析任务将进入 <span className="font-mono">BLOCKED_POLICY</span>，绝不自动外发至外部 MinerU 云服务。
          </Alert>
        ) : null}

        {/* 拖拽上传区域 */}
        <div
          onDragOver={(event) => {
            event.preventDefault();
            setIsDragging(true);
          }}
          onDragLeave={() => setIsDragging(false)}
          onDrop={handleDrop}
          className={`flex flex-col items-center justify-center rounded-xl border-2 border-dashed p-6 text-center transition-colors ${
            isDragging
              ? 'border-accent bg-surface-muted'
              : 'border-line bg-surface-subtle hover:border-line-strong'
          }`}
        >
          <p className="text-sm font-medium text-ink">拖拽单个文件、多个文件或整个文件夹到此处</p>
          <p className="mt-1 text-xs text-ink-muted">
            单文件走 `/uploads` 会话，多文件/文件夹走 `/import-batches` 幂等批次导入
          </p>

          <div className="mt-3 flex flex-wrap items-center justify-center gap-2">
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept={ACCEPTED_EXTENSIONS}
              className="hidden"
              onChange={(event) => {
                if (event.target.files) appendFiles(event.target.files);
                event.target.value = '';
              }}
            />
            <input
              ref={folderInputRef}
              type="file"
              multiple
              className="hidden"
              {...folderExtraProps}
              onChange={(event) => {
                if (event.target.files) appendFiles(event.target.files);
                event.target.value = '';
              }}
            />

            <Button
              variant="outline"
              size="sm"
              onClick={() => fileInputRef.current?.click()}
            >
              <FileStack className="h-3.5 w-3.5" aria-hidden="true" />
              选择文件（单选/多选）
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => folderInputRef.current?.click()}
            >
              <FolderUp className="h-3.5 w-3.5" aria-hidden="true" />
              选择文件夹
            </Button>
          </div>
        </div>

        {/* 待提交文件队列 */}
        {queuedFiles.length > 0 ? (
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <p className="text-xs font-medium text-ink">待导入文件（{queuedFiles.length}）</p>
              <div className="flex items-center gap-2">
                <Button variant="ghost" size="sm" onClick={() => setQueuedFiles([])}>
                  清空队列
                </Button>
                <Button
                  size="sm"
                  loading={uploadMutation.isPending}
                  onClick={() => uploadMutation.mutate()}
                >
                  开始导入（{queuedFiles.length === 1 ? '单文件' : '批量'}）
                </Button>
              </div>
            </div>

            <ul className="max-h-36 divide-y divide-line overflow-y-auto rounded-lg border border-line bg-surface-elevated">
              {queuedFiles.map((file, index) => (
                <li
                  key={`${file.name}-${file.size}-${index}`}
                  className="flex items-center justify-between gap-2 px-3 py-1.5 text-xs"
                >
                  <span className="truncate font-medium text-ink">
                    {file.webkitRelativePath || file.name}
                  </span>
                  <div className="flex items-center gap-2 shrink-0">
                    <span className="font-mono text-[11px] text-ink-muted">
                      {(file.size / 1024).toFixed(1)} KB
                    </span>
                    <button
                      type="button"
                      aria-label={`移除文件 ${file.name}`}
                      className="text-ink-muted hover:text-danger"
                      onClick={() =>
                        setQueuedFiles((prev) => prev.filter((_, idx) => idx !== index))
                      }
                    >
                      <X className="h-3.5 w-3.5" aria-hidden="true" />
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}
