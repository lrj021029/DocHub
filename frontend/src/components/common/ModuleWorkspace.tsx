import { useState, type ReactNode } from 'react';
import { Inbox, RefreshCw } from 'lucide-react';
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
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from '@/components/ui';
import { useAuth } from '@/lib/auth-store';

/** 页面受控视图状态：正常内容 / 加载态 / 空态 / 错误态 */
export type WorkspaceViewState = 'ready' | 'loading' | 'empty' | 'error';

export interface WorkspaceMetric {
  label: string;
  value: string;
  hint: string;
}

export interface ModuleWorkspaceProps {
  /** 模块编号（对应 PRD 8.1 序号） */
  moduleCode: string;
  /** 页面主标题 */
  title: string;
  /** 页面职责副标题 */
  description: string;
  /** 对应后端 API 端点契约提示 */
  apiContract: string;
  /** 顶部安全/治理标签 */
  governanceBadges?: string[];
  /** 关键指标卡预览（阶段 0 明确标注待接入真实数据，严禁伪造业务数字） */
  metrics?: WorkspaceMetric[];
  /** 空态说明文案 */
  emptyTitle?: string;
  emptyDescription?: string;
  /** 错误态说明文案 */
  errorDescription?: string;
  /** 右侧主操作按钮区 */
  headerActions?: ReactNode;
  /** 正常态主体内容 */
  children: ReactNode;
}

/**
 * 统一企业级页面工作台模板。
 * 依据 PRD §8.2，为所有 14 个模块提供一致的页头规范、组织上下文标注，
 * 以及可交互验证的 Loading / Empty / Error 标准状态视图。
 */
export function ModuleWorkspace({
  moduleCode,
  title,
  description,
  apiContract,
  governanceBadges = [],
  metrics = [],
  emptyTitle = '当前组织下暂无业务数据',
  emptyDescription = '待对应 Vertical Slice 后端接口就绪并完成数据初始化后，此处将展示受权限保护的真实数据。',
  errorDescription = '请求后端接口失败或当前用户缺少对应功能权限（后端强制校验）。请检查网络或重试。',
  headerActions,
  children,
}: ModuleWorkspaceProps) {
  const { currentOrgId } = useAuth();
  const [viewState, setViewState] = useState<WorkspaceViewState>('ready');

  return (
    <div className="space-y-5">
      {/* 页头信息与操作区 */}
      <div className="flex flex-col gap-3 border-b border-line pb-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="space-y-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="outline" className="font-mono text-[10px]">
              {moduleCode}
            </Badge>
            <h1 className="text-xl font-semibold tracking-tight text-ink">{title}</h1>
            {governanceBadges.map((tag) => (
              <Badge key={tag} variant="neutral">
                {tag}
              </Badge>
            ))}
          </div>
          <p className="max-w-3xl text-sm text-ink-secondary">{description}</p>
          <p className="font-mono text-[11px] text-ink-muted">
            API 契约：{apiContract} · 当前组织：{currentOrgId ?? '未绑定组织（请先登录或切换）'}
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">{headerActions}</div>
      </div>

      {/* 关键指标概览槽位 */}
      {metrics.length > 0 ? (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {metrics.map((item) => (
            <Card key={item.label}>
              <CardContent className="py-3.5">
                <p className="text-xs font-medium text-ink-muted">{item.label}</p>
                <p className="mt-1.5 text-lg font-semibold tracking-tight text-ink tabular-nums">
                  {item.value}
                </p>
                <p className="mt-1 text-[11px] text-ink-muted">{item.hint}</p>
              </CardContent>
            </Card>
          ))}
        </div>
      ) : null}

      {/* 状态规范切换器：便于在 Phase 0 直接检验 Loading / Empty / Error 标准体验 */}
      <Tabs
        value={viewState}
        onValueChange={(next) => setViewState(next as WorkspaceViewState)}
      >
        <div className="flex flex-wrap items-center justify-between gap-2">
          <TabsList aria-label="页面状态视图切换">
            <TabsTrigger value="ready">工作台视图</TabsTrigger>
            <TabsTrigger value="loading">Loading 态</TabsTrigger>
            <TabsTrigger value="empty">Empty 态</TabsTrigger>
            <TabsTrigger value="error">Error 态</TabsTrigger>
          </TabsList>
          <span className="text-xs text-ink-muted">
            COSS UI 规范：显式 Loading / Empty / Error 状态与全键盘可操作
          </span>
        </div>

        <TabsContent value="ready">{children}</TabsContent>

        <TabsContent value="loading">
          <Card>
            <CardHeader>
              <CardTitle>正在加载受权限保护的数据...</CardTitle>
              <CardDescription>等待后端权威鉴权与数据库查询响应，不使用虚构进度百分比</CardDescription>
            </CardHeader>
            <CardContent className="space-y-3">
              <Skeleton shape="line" className="w-1/3" />
              <Skeleton shape="block" className="h-24 w-full" />
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                <Skeleton shape="block" className="h-16" />
                <Skeleton shape="block" className="h-16" />
                <Skeleton shape="block" className="h-16" />
              </div>
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="empty">
          <Card>
            <CardContent className="flex flex-col items-center justify-center py-12 text-center">
              <div className="flex h-11 w-11 items-center justify-center rounded-full border border-line bg-surface-muted text-ink-muted">
                <Inbox className="h-5 w-5" aria-hidden="true" />
              </div>
              <h2 className="mt-3 text-sm font-semibold text-ink">{emptyTitle}</h2>
              <p className="mt-1.5 max-w-md text-xs leading-relaxed text-ink-muted">
                {emptyDescription}
              </p>
              <Button
                variant="outline"
                size="sm"
                className="mt-4"
                onClick={() => setViewState('ready')}
              >
                返回工作台视图
              </Button>
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="error">
          <Alert variant="danger" title="数据加载异常（Fail-Closed 保护）">
            <p>{errorDescription}</p>
            <div className="mt-3 flex items-center gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => setViewState('ready')}
              >
                <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
                重新尝试
              </Button>
            </div>
          </Alert>
        </TabsContent>
      </Tabs>
    </div>
  );
}
