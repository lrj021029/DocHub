import { useState } from 'react';
import { zodResolver } from '@hookform/resolvers/zod';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Edit3, FolderKanban, FolderPlus, Inbox, RefreshCw } from 'lucide-react';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
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
  createKnowledgeSpace,
  fetchKnowledgeSpaces,
  formatSpaceError,
  updateKnowledgeSpace,
  type KnowledgeSpaceItem,
  type SpaceAnswerPolicy,
  type SpaceVisibility,
} from '@/features/spaces/api';
import { SpaceMembersPanel } from '@/features/spaces/SpaceMembersPanel';
import { useAuth } from '@/lib/auth-store';

/** 新建/编辑 Knowledge Space 表单校验 */
const spaceFormSchema = z.object({
  name: z.string().trim().min(2, 'Space 名称不少于 2 个字符').max(80, 'Space 名称不超过 80 个字符'),
  description: z.string().max(240, '描述不超过 240 个字符').optional(),
  visibility: z.enum(['PUBLIC', 'PRIVATE']),
  answer_policy: z.enum(['STRICT_EVIDENCE', 'GROUNDED_WITH_GENERAL_CONTEXT']),
  default_embedding_profile_id: z.string().optional(),
});

type SpaceFormValues = z.infer<typeof spaceFormSchema>;

/**
 * PRD 模块 4（Slice 2 真实对接）：Knowledge Space 管理与成员入口授权。
 * 对接 GET/POST/PATCH /api/v1/organizations/{org_id}/spaces 及 /api/v1/spaces/{space_id}/members。
 */
export function SpacesPage() {
  const queryClient = useQueryClient();
  const { currentOrgId, hasPermission } = useAuth();
  const [selectedSpaceId, setSelectedSpaceId] = useState<string | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editingSpace, setEditingSpace] = useState<KnowledgeSpaceItem | null>(null);
  const [feedback, setFeedback] = useState<{ variant: 'success' | 'danger'; message: string } | null>(
    null,
  );

  // 前端仅用于体验层按钮显隐；后端对所有 Space 写操作强制执行 require_permission 校验
  const canManageSpaces =
    hasPermission('space:manage') ||
    hasPermission('space:write') ||
    hasPermission('org:manage') ||
    hasPermission('organization:manage');

  const spacesQuery = useQuery({
    queryKey: ['spaces', currentOrgId],
    queryFn: () => fetchKnowledgeSpaces(currentOrgId!),
    enabled: Boolean(currentOrgId),
  });

  const spaces = spacesQuery.data ?? [];
  const activeSpace =
    spaces.find((s) => s.id === selectedSpaceId) ?? spaces[0] ?? null;

  const form = useForm<SpaceFormValues>({
    resolver: zodResolver(spaceFormSchema),
    defaultValues: {
      name: '',
      description: '',
      visibility: 'PRIVATE',
      answer_policy: 'STRICT_EVIDENCE',
      default_embedding_profile_id: '',
    },
  });

  const saveMutation = useMutation({
    mutationFn: (values: SpaceFormValues) => {
      if (!currentOrgId) throw new Error('尚未绑定当前 Organization');
      const payload = {
        name: values.name,
        description: values.description || '',
        visibility: values.visibility as SpaceVisibility,
        answer_policy: values.answer_policy as SpaceAnswerPolicy,
        default_embedding_profile_id: values.default_embedding_profile_id?.trim() || null,
      };
      if (editingSpace) {
        return updateKnowledgeSpace(currentOrgId, editingSpace.id, payload);
      }
      return createKnowledgeSpace(currentOrgId, payload);
    },
    onSuccess: (saved) => {
      setFeedback({
        variant: 'success',
        message: editingSpace
          ? `Knowledge Space「${saved.name}」配置已更新。`
          : `Knowledge Space「${saved.name}」已创建。`,
      });
      setSelectedSpaceId(saved.id);
      setDialogOpen(false);
      setEditingSpace(null);
      void queryClient.invalidateQueries({ queryKey: ['spaces', currentOrgId] });
    },
    onError: (err) => {
      setFeedback({ variant: 'danger', message: formatSpaceError(err, '保存 Knowledge Space 失败') });
    },
  });

  const openCreateDialog = () => {
    setEditingSpace(null);
    form.reset({
      name: '',
      description: '',
      visibility: 'PRIVATE',
      answer_policy: 'STRICT_EVIDENCE',
      default_embedding_profile_id: '',
    });
    setDialogOpen(true);
  };

  const openEditDialog = (space: KnowledgeSpaceItem) => {
    setEditingSpace(space);
    form.reset({
      name: space.name,
      description: space.description,
      visibility: space.visibility,
      answer_policy: space.answerPolicy,
      default_embedding_profile_id: space.defaultEmbeddingProfileId ?? '',
    });
    setDialogOpen(true);
  };

  return (
    <div className="space-y-5">
      {/* 页面头部 */}
      <div className="flex flex-col gap-3 border-b border-line pb-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="space-y-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="outline" className="font-mono text-[10px]">
              PRD-04
            </Badge>
            <h1 className="text-xl font-semibold tracking-tight text-ink">
              Knowledge Space 管理
            </h1>
            <Badge variant="neutral">Space 入口 + Document ACL 双层鉴权</Badge>
          </div>
          <p className="text-sm text-ink-secondary">
            治理当前 Organization 下的知识空间、公开/私有入口成员授权、Embedding Profile 及 RAG 回答策略。
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => void spacesQuery.refetch()}
            disabled={spacesQuery.isFetching}
          >
            <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
            刷新
          </Button>
          {canManageSpaces ? (
            <Button size="sm" onClick={openCreateDialog} disabled={!currentOrgId}>
              <FolderPlus className="h-3.5 w-3.5" aria-hidden="true" />
              新建 Knowledge Space
            </Button>
          ) : null}
        </div>
      </div>

      {feedback ? <Alert variant={feedback.variant}>{feedback.message}</Alert> : null}

      {!currentOrgId ? (
        <Alert variant="warning" title="尚未绑定当前 Organization">
          请先在顶部组织切换器选择有效组织后再管理 Knowledge Space。
        </Alert>
      ) : (
        <div className="space-y-5">
          {/* Space 台账列表 */}
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <FolderKanban className="h-4 w-4 text-ink-muted" aria-hidden="true" />
                当前组织知识空间列表
              </CardTitle>
              <CardDescription>
                点击任意空间行可在下方查看或配置其入口成员权限（PUBLIC 仅限本组织成员，非跨组织公开）。
              </CardDescription>
            </CardHeader>
            <CardContent>
              {spacesQuery.isLoading ? (
                <div className="space-y-2.5" data-testid="spaces-loading">
                  <Skeleton shape="block" className="h-12 w-full" />
                  <Skeleton shape="block" className="h-12 w-full" />
                </div>
              ) : spacesQuery.isError ? (
                <Alert variant="danger" title="加载 Knowledge Space 失败">
                  {formatSpaceError(spacesQuery.error, '无法加载空间列表')}
                </Alert>
              ) : spaces.length === 0 ? (
                <div className="flex flex-col items-center justify-center rounded-lg border border-dashed border-line py-10 text-center">
                  <Inbox className="h-5 w-5 text-ink-muted" aria-hidden="true" />
                  <p className="mt-2 text-sm font-medium text-ink">当前组织尚未创建 Knowledge Space</p>
                  <p className="mt-1 text-xs text-ink-muted">
                    点击右上角「新建 Knowledge Space」创建首个知识空间。
                  </p>
                </div>
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Space 名称</TableHead>
                      <TableHead>可见性</TableHead>
                      <TableHead>回答策略</TableHead>
                      <TableHead>Embedding Profile</TableHead>
                      <TableHead>状态 / 版本</TableHead>
                      {canManageSpaces ? <TableHead className="text-right">操作</TableHead> : null}
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {spaces.map((space) => {
                      const isSelected = activeSpace?.id === space.id;
                      return (
                        <TableRow
                          key={space.id}
                          data-selected={isSelected}
                          className="cursor-pointer"
                          onClick={() => setSelectedSpaceId(space.id)}
                        >
                          <TableCell>
                            <p className="font-medium text-ink">{space.name}</p>
                            {space.description ? (
                              <p className="text-xs text-ink-muted">{space.description}</p>
                            ) : null}
                          </TableCell>
                          <TableCell>
                            <Badge variant={space.visibility === 'PUBLIC' ? 'info' : 'warning'}>
                              {space.visibility}
                            </Badge>
                          </TableCell>
                          <TableCell>
                            <Badge variant="outline" className="font-mono text-[10px]">
                              {space.answerPolicy}
                            </Badge>
                          </TableCell>
                          <TableCell className="text-xs text-ink-secondary">
                            {space.embeddingProfileName}
                          </TableCell>
                          <TableCell>
                            <div className="flex items-center gap-1.5">
                              <Badge variant={space.status === 'ACTIVE' ? 'success' : 'neutral'}>
                                {space.status}
                              </Badge>
                              <span className="font-mono text-[11px] text-ink-muted">
                                v{space.configVersion}
                              </span>
                            </div>
                          </TableCell>
                          {canManageSpaces ? (
                            <TableCell className="text-right">
                              <Button
                                variant="outline"
                                size="sm"
                                onClick={(event) => {
                                  event.stopPropagation();
                                  openEditDialog(space);
                                }}
                              >
                                <Edit3 className="h-3.5 w-3.5" aria-hidden="true" />
                                编辑
                              </Button>
                            </TableCell>
                          ) : null}
                        </TableRow>
                      );
                    })}
                  </TableBody>
                </Table>
              )}
            </CardContent>
          </Card>

          {/* 选中 Space 的入口成员管理面板 */}
          {activeSpace ? (
            <SpaceMembersPanel
              orgId={currentOrgId}
              space={activeSpace}
              canManage={canManageSpaces}
            />
          ) : null}
        </div>
      )}

      {/* 新建 / 编辑 Space 弹窗 */}
      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {editingSpace ? `编辑 Knowledge Space：${editingSpace.name}` : '新建 Knowledge Space'}
            </DialogTitle>
            <DialogDescription>
              配置空间入口可见性、回答模式与向量 Profile。PUBLIC 仅对本组织有效成员开放，严禁跨组织访问。
            </DialogDescription>
          </DialogHeader>

          <form
            className="mt-4 space-y-3"
            onSubmit={form.handleSubmit((vals) => saveMutation.mutate(vals))}
            noValidate
          >
            <div className="space-y-1">
              <label htmlFor="space-name" className="text-xs font-medium text-ink">
                Space 名称
              </label>
              <Input
                id="space-name"
                placeholder="例如：财务与差旅制度库"
                invalid={Boolean(form.formState.errors.name)}
                {...form.register('name')}
              />
              {form.formState.errors.name ? (
                <p className="text-xs text-danger">{form.formState.errors.name.message}</p>
              ) : null}
            </div>

            <div className="space-y-1">
              <label htmlFor="space-desc" className="text-xs font-medium text-ink">
                空间说明（可选）
              </label>
              <Input
                id="space-desc"
                placeholder="存放公司财务报销规范与审批流文档"
                {...form.register('description')}
              />
            </div>

            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <div className="space-y-1">
                <label htmlFor="space-visibility" className="text-xs font-medium text-ink">
                  入口可见性
                </label>
                <select
                  id="space-visibility"
                  className="flex h-9 w-full rounded-lg border border-line bg-surface-elevated px-3 text-sm text-ink shadow-xs"
                  {...form.register('visibility')}
                >
                  <option value="PRIVATE">PRIVATE（仅显式成员可进入）</option>
                  <option value="PUBLIC">PUBLIC（本组织有效成员公开）</option>
                </select>
              </div>

              <div className="space-y-1">
                <label htmlFor="space-policy" className="text-xs font-medium text-ink">
                  回答策略
                </label>
                <select
                  id="space-policy"
                  className="flex h-9 w-full rounded-lg border border-line bg-surface-elevated px-3 text-sm text-ink shadow-xs"
                  {...form.register('answer_policy')}
                >
                  <option value="STRICT_EVIDENCE">STRICT_EVIDENCE（严格基于授权证据）</option>
                  <option value="GROUNDED_WITH_GENERAL_CONTEXT">
                    GROUNDED_WITH_GENERAL_CONTEXT（允许通用知识补充并标注）
                  </option>
                </select>
              </div>
            </div>

            <div className="space-y-1">
              <label htmlFor="space-profile" className="text-xs font-medium text-ink">
                绑定 Embedding Profile ID（可选，留空采用组织默认）
              </label>
              <Input
                id="space-profile"
                placeholder="例如：qwen-text-embedding-v3"
                {...form.register('default_embedding_profile_id')}
              />
            </div>

            <DialogFooter>
              <DialogClose render={<Button variant="outline" size="sm">取消</Button>} />
              <Button type="submit" size="sm" loading={saveMutation.isPending}>
                {editingSpace ? '保存修改' : '确认创建'}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  );
}
