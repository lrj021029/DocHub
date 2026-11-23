import { useMemo, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Activity,
  AlertTriangle,
  Building2,
  CheckCircle2,
  LogIn,
  LogOut,
  Menu,
  RefreshCw,
  Shield,
  UserCircle2,
  X,
} from 'lucide-react';
import { Link, NavLink, Outlet, useNavigate } from 'react-router-dom';
import {
  Alert,
  Badge,
  Button,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui';
import {
  LOGIN_NAV_ITEM,
  NAV_GROUP_LABELS,
  WORKSPACE_NAV_ITEMS,
  type NavGroupKey,
} from '@/config/navigation';
import { fetchBackendReadiness } from '@/lib/api-client';
import { useAuth } from '@/lib/auth-store';
import { cn } from '@/lib/cn';
import type { OrganizationSummary } from '@/types/domain';

/** 当组织清单为空时的安全占位项 */
const FALLBACK_ORGS: OrganizationSummary[] = [
  {
    id: 'org-unassigned',
    name: '暂无已绑定组织',
    slug: 'unassigned',
    status: 'ACTIVE',
    roleNames: [],
  },
];

const GROUP_ORDER: NavGroupKey[] = [
  'workspace',
  'governance',
  'knowledge',
  'intelligence',
  'operations',
];

/**
 * DocHub 企业级主应用外壳（AppShell）。
 * 包含：
 * - 左侧分组导航侧边栏（覆盖 PRD 全部 14 个模块路由入口）
 * - 顶部当前 Organization 切换器（真实调用 POST /api/v1/organizations/{org_id}/switch 并刷新全部 Query 缓存）
 * - 后端 /api/v1/readyz 实时健康状态指示器
 * - 用户身份菜单（真实展示邮箱、SystemRole 与 OrganizationRole 徽章，登出调用 POST /api/v1/auth/logout）
 */
export function AppShell() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const {
    user,
    organizations,
    currentOrgId,
    isAuthenticated,
    switchOrganization,
    logout,
  } = useAuth();
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const [isSwitchingOrg, setIsSwitchingOrg] = useState(false);
  const [isLoggingOut, setIsLoggingOut] = useState(false);
  const [switchError, setSwitchError] = useState<string | null>(null);

  // 定时轮询后端 /api/v1/readyz 就绪探针，实时展示服务健康状态
  const readinessQuery = useQuery({
    queryKey: ['system', 'readyz'],
    queryFn: () => fetchBackendReadiness(),
    refetchInterval: 30_000,
    retry: false,
    staleTime: 10_000,
  });

  const availableOrgs = organizations.length > 0 ? organizations : FALLBACK_ORGS;
  const activeOrgId = currentOrgId ?? availableOrgs[0]?.id ?? '';

  const groupedNav = useMemo(() => {
    return GROUP_ORDER.map((groupKey) => ({
      key: groupKey,
      label: NAV_GROUP_LABELS[groupKey],
      items: WORKSPACE_NAV_ITEMS.filter((item) => item.group === groupKey),
    }));
  }, []);

  const readinessState = readinessQuery.data?.status ?? 'unavailable';
  const readinessBadge = useMemo(() => {
    if (readinessQuery.isLoading) {
      return { variant: 'neutral' as const, label: '探测中', icon: Activity };
    }
    if (readinessState === 'ok' || readinessState === 'ready') {
      return { variant: 'success' as const, label: '后端就绪', icon: CheckCircle2 };
    }
    if (readinessState === 'degraded') {
      return { variant: 'warning' as const, label: '服务降级', icon: AlertTriangle };
    }
    return { variant: 'danger' as const, label: '后端未连接', icon: AlertTriangle };
  }, [readinessQuery.isLoading, readinessState]);

  const ReadinessIcon = readinessBadge.icon;

  /** 执行真实的组织切换：调用 /organizations/{org_id}/switch 并失效全部 TanStack Query 缓存 */
  const handleOrganizationChange = async (nextOrgId: string) => {
    if (!nextOrgId || nextOrgId === activeOrgId || nextOrgId === 'org-unassigned') {
      return;
    }
    setSwitchError(null);
    setIsSwitchingOrg(true);
    try {
      const ok = await switchOrganization(nextOrgId);
      if (!ok) {
        setSwitchError('切换组织失败：您可能不再是目标组织的有效成员，或会话已失效。');
        return;
      }
      // 组织上下文变更后立即刷新所有业务缓存，杜绝跨租户缓存串读
      await queryClient.invalidateQueries();
    } catch {
      setSwitchError('切换组织请求异常，请稍后重试。');
    } finally {
      setIsSwitchingOrg(false);
    }
  };

  /** 执行安全登出：撤销服务端 Refresh 会话、清空内存状态与查询缓存 */
  const handleLogout = async () => {
    setIsLoggingOut(true);
    try {
      await logout();
      queryClient.clear();
      navigate('/login', { replace: true });
    } finally {
      setIsLoggingOut(false);
    }
  };

  /** 侧边栏导航内容（桌面端常驻 + 移动端抽屉复用） */
  const renderSidebarNav = () => (
    <div className="flex h-full flex-col">
      {/* 品牌标识 */}
      <div className="flex h-14 items-center justify-between border-b border-line px-4">
        <Link
          to="/"
          className="flex items-center gap-2.5 rounded-md text-ink focus-visible:outline-2 focus-visible:outline-ring"
        >
          <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-accent text-ink-inverted shadow-xs">
            <Shield className="h-4 w-4" aria-hidden="true" />
          </div>
          <div className="leading-tight">
            <span className="block text-sm font-semibold tracking-tight">DocHub</span>
            <span className="block text-[10px] font-medium text-ink-muted">
              Enterprise Knowledge OS
            </span>
          </div>
        </Link>
        <button
          type="button"
          aria-label="关闭导航菜单"
          className="rounded-md p-1 text-ink-muted hover:bg-surface-muted hover:text-ink lg:hidden"
          onClick={() => setMobileMenuOpen(false)}
        >
          <X className="h-4 w-4" aria-hidden="true" />
        </button>
      </div>

      {/* 分组导航列表 */}
      <nav
        aria-label="DocHub 主导航"
        className="flex-1 space-y-5 overflow-y-auto px-3 py-4"
      >
        {groupedNav.map((group) => (
          <div key={group.key} className="space-y-1">
            <p className="px-2.5 text-[11px] font-semibold tracking-wide text-ink-muted uppercase">
              {group.label}
            </p>
            <ul className="space-y-0.5">
              {group.items.map((item) => {
                const IconComp = item.icon;
                return (
                  <li key={item.id}>
                    <NavLink
                      to={item.path}
                      end={item.path === '/'}
                      onClick={() => setMobileMenuOpen(false)}
                      className={({ isActive }) =>
                        cn(
                          'flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-[13px] font-medium transition-colors',
                          'focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-ring',
                          isActive
                            ? 'bg-accent text-ink-inverted shadow-xs'
                            : 'text-ink-secondary hover:bg-surface-muted hover:text-ink',
                        )
                      }
                    >
                      <IconComp className="h-4 w-4 shrink-0" aria-hidden="true" />
                      <span className="truncate">{item.label}</span>
                    </NavLink>
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
      </nav>

      {/* 底部会话入口（涵盖 PRD 模块 1：登录与会话） */}
      <div className="border-t border-line p-3">
        <NavLink
          to={LOGIN_NAV_ITEM.path}
          onClick={() => setMobileMenuOpen(false)}
          className={({ isActive }) =>
            cn(
              'flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-[13px] font-medium transition-colors',
              isActive
                ? 'bg-accent text-ink-inverted'
                : 'text-ink-secondary hover:bg-surface-muted hover:text-ink',
            )
          }
        >
          <LogIn className="h-4 w-4 shrink-0" aria-hidden="true" />
          <span>{LOGIN_NAV_ITEM.label}</span>
        </NavLink>
      </div>
    </div>
  );

  return (
    <div className="flex min-h-screen bg-surface text-ink">
      {/* 桌面端左侧企业级固定侧边栏 */}
      <aside className="hidden w-64 shrink-0 border-r border-line bg-surface-elevated lg:block">
        <div className="sticky top-0 h-screen">{renderSidebarNav()}</div>
      </aside>

      {/* 移动端抽屉侧边栏 */}
      {mobileMenuOpen ? (
        <div className="fixed inset-0 z-50 flex lg:hidden">
          <div
            className="fixed inset-0 bg-ink/35 backdrop-blur-[1px]"
            onClick={() => setMobileMenuOpen(false)}
          />
          <aside className="relative z-10 w-68 max-w-[82vw] border-r border-line bg-surface-elevated shadow-md">
            {renderSidebarNav()}
          </aside>
        </div>
      ) : null}

      {/* 右侧主区域（顶栏 + 路由内容） */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex h-14 items-center justify-between gap-3 border-b border-line bg-surface-elevated/95 px-4 backdrop-blur-xs lg:px-6">
          {/* 左侧：移动端菜单按钮 + 当前 Organization 切换器 */}
          <div className="flex min-w-0 items-center gap-3">
            <button
              type="button"
              aria-label="打开导航菜单"
              className="inline-flex h-8 w-8 items-center justify-center rounded-md border border-line text-ink-secondary hover:bg-surface-muted lg:hidden"
              onClick={() => setMobileMenuOpen(true)}
            >
              <Menu className="h-4 w-4" aria-hidden="true" />
            </button>

            <div className="flex items-center gap-2">
              <Building2 className="hidden h-4 w-4 text-ink-muted sm:block" aria-hidden="true" />
              <span className="hidden text-xs font-medium text-ink-muted md:inline">当前组织</span>
              <div className="w-48 sm:w-56">
                <Select
                  value={activeOrgId}
                  disabled={isSwitchingOrg || organizations.length === 0}
                  onValueChange={(value) => {
                    if (typeof value === 'string') {
                      void handleOrganizationChange(value);
                    }
                  }}
                >
                  <SelectTrigger aria-label="切换当前 Organization" className="h-8 text-xs">
                    <SelectValue placeholder="选择当前 Organization" />
                  </SelectTrigger>
                  <SelectContent>
                    {availableOrgs.map((org) => (
                      <SelectItem key={org.id} value={org.id}>
                        {org.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </div>
          </div>

          {/* 右侧：/api/v1/readyz 实时健康状态指示器 + 用户菜单与角色徽章 */}
          <div className="flex items-center gap-2.5">
            <div
              className="flex items-center gap-1.5"
              title="后端就绪探针 GET /api/v1/readyz"
              data-testid="backend-readiness-indicator"
            >
              <Badge variant={readinessBadge.variant} className="gap-1.5 py-1">
                <ReadinessIcon className="h-3 w-3" aria-hidden="true" />
                <span>{readinessBadge.label}</span>
              </Badge>
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7"
                aria-label="刷新后端健康状态"
                onClick={() => void readinessQuery.refetch()}
              >
                <RefreshCw
                  className={cn('h-3.5 w-3.5', readinessQuery.isFetching && 'animate-spin')}
                  aria-hidden="true"
                />
              </Button>
            </div>

            <div className="h-4 w-px bg-line" aria-hidden="true" />

            {/* 用户身份与角色菜单区 */}
            <div className="flex items-center gap-2" data-testid="user-menu">
              <div className="hidden text-right md:block">
                <div className="flex items-center justify-end gap-1.5">
                  <p className="text-xs font-medium leading-tight text-ink">
                    {user?.email ?? '未登录访客'}
                  </p>
                  {user?.systemRoles.map((role) => (
                    <Badge key={`sys-${role}`} variant="warning" className="py-0 text-[10px]">
                      {role}
                    </Badge>
                  ))}
                  {user?.organizationRoles.slice(0, 2).map((role) => (
                    <Badge key={`org-${role}`} variant="info" className="py-0 text-[10px]">
                      {role}
                    </Badge>
                  ))}
                </div>
                <p className="text-[10px] leading-tight text-ink-muted">
                  {user?.displayName ?? '内存会话未激活'}
                </p>
              </div>
              <UserCircle2 className="h-5 w-5 text-ink-secondary" aria-hidden="true" />
              {isAuthenticated ? (
                <Button
                  variant="outline"
                  size="sm"
                  loading={isLoggingOut}
                  onClick={() => void handleLogout()}
                >
                  <LogOut className="h-3.5 w-3.5" aria-hidden="true" />
                  退出
                </Button>
              ) : (
                <Button variant="outline" size="sm" onClick={() => navigate('/login')}>
                  登录
                </Button>
              )}
            </div>
          </div>
        </header>

        {/* 组织切换失败提示横幅 */}
        {switchError ? (
          <div className="mx-auto w-full max-w-7xl px-4 pt-4 lg:px-8">
            <Alert variant="danger" title="组织切换受阻">
              {switchError}
            </Alert>
          </div>
        ) : null}

        {/* 主内容视图区 */}
        <main className="mx-auto w-full max-w-7xl flex-1 px-4 py-6 lg:px-8">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
