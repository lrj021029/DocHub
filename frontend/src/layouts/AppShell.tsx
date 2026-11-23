import { useMemo, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Activity,
  AlertTriangle,
  Building2,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  KeyRound,
  LogIn,
  LogOut,
  Menu,
  RefreshCw,
  Shield,
  UserCircle2,
  X,
} from 'lucide-react';
import { Link, NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom';
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
  DialogTrigger,
  Input,
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
import { changeOwnPassword, formatIamError } from '@/features/iam/api';
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
 * DocHub × Notion 风格企业级主应用外壳（AppShell）。
 * 视觉特点：
 * - 左侧极浅暖灰（#F7F7F5）侧边栏 + 可折叠式页面分组 + 柔和 #EFEFED 悬停态
 * - 顶部极简面包屑与 Organization 工作区切换器 + 极细 1px 暖灰分割线（#E9E9E7）
 * - 纯白主画布（#FFFFFF）+ 宽敞呼吸感留白 + /api/v1/readyz 实时健康状态胶囊
 */
export function AppShell() {
  const navigate = useNavigate();
  const location = useLocation();
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
  const [collapsedGroups, setCollapsedGroups] = useState<Record<string, boolean>>({});
  const [changePwdOpen, setChangePwdOpen] = useState(false);
  const [currentPwd, setCurrentPwd] = useState('');
  const [newPwd, setNewPwd] = useState('');
  const [confirmNewPwd, setConfirmNewPwd] = useState('');
  const [isChangingPwd, setIsChangingPwd] = useState(false);
  const [pwdFeedback, setPwdFeedback] = useState<{
    variant: 'success' | 'danger';
    message: string;
  } | null>(null);

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
  const activeOrg = availableOrgs.find((o) => o.id === activeOrgId);

  const groupedNav = useMemo(() => {
    return GROUP_ORDER.map((groupKey) => ({
      key: groupKey,
      label: NAV_GROUP_LABELS[groupKey],
      items: WORKSPACE_NAV_ITEMS.filter((item) => item.group === groupKey),
    }));
  }, []);

  /** 计算顶部面包屑当前页面标题 */
  const currentNavItem = useMemo(() => {
    return (
      WORKSPACE_NAV_ITEMS.find((item) =>
        item.path === '/' ? location.pathname === '/' : location.pathname.startsWith(item.path),
      ) ?? WORKSPACE_NAV_ITEMS[0]
    );
  }, [location.pathname]);

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

  /** 切换左侧分组折叠状态 */
  const toggleGroup = (groupKey: NavGroupKey) => {
    setCollapsedGroups((prev) => ({ ...prev, [groupKey]: !prev[groupKey] }));
  };

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
      await queryClient.invalidateQueries();
    } catch {
      setSwitchError('切换组织请求异常，请稍后重试。');
    } finally {
      setIsSwitchingOrg(false);
    }
  };

  /** 执行当前用户自助改密，同时吊销其他设备活跃会话 */
  const handleChangeOwnPassword = async (e: React.FormEvent) => {
    e.preventDefault();
    setPwdFeedback(null);
    if (newPwd.length < 8 || !/[A-Za-z]/.test(newPwd) || !/[0-9]/.test(newPwd)) {
      setPwdFeedback({
        variant: 'danger',
        message: '新密码至少 8 位，且必须同时包含英文字母和数字。',
      });
      return;
    }
    if (newPwd !== confirmNewPwd) {
      setPwdFeedback({ variant: 'danger', message: '两次输入的新密码不一致。' });
      return;
    }
    setIsChangingPwd(true);
    try {
      const res = await changeOwnPassword({
        current_password: currentPwd,
        new_password: newPwd,
      });
      setPwdFeedback({
        variant: 'success',
        message: `密码修改成功，已同步吊销 ${res.revoked_other_sessions} 个其他设备会话。`,
      });
      setCurrentPwd('');
      setNewPwd('');
      setConfirmNewPwd('');
    } catch (err) {
      setPwdFeedback({ variant: 'danger', message: formatIamError(err, '修改密码失败') });
    } finally {
      setIsChangingPwd(false);
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

  /** Notion 风格极浅暖灰侧边栏（#F7F7F5） */
  const renderSidebarNav = () => (
    <div className="flex h-full flex-col bg-surface-sidebar">
      {/* 品牌与工作区头 */}
      <div className="flex h-12 items-center justify-between border-b border-line px-3.5">
        <Link
          to="/"
          className="flex items-center gap-2 rounded-md px-1 py-0.5 text-ink transition-colors hover:bg-surface-muted focus-visible:outline-2 focus-visible:outline-ring"
        >
          <div className="flex h-6 w-6 items-center justify-center rounded-md bg-accent text-ink-inverted">
            <Shield className="h-3.5 w-3.5" aria-hidden="true" />
          </div>
          <div className="leading-none">
            <span className="text-[13px] font-semibold tracking-tight">DocHub</span>
            <span className="ml-1.5 rounded bg-surface-muted px-1 py-0.5 text-[10px] font-medium text-ink-muted">
              Workspace
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

      {/* Notion 式可折叠分组导航 */}
      <nav
        aria-label="DocHub 主导航"
        className="flex-1 space-y-4 overflow-y-auto px-2.5 py-3.5"
      >
        {groupedNav.map((group) => {
          const isCollapsed = Boolean(collapsedGroups[group.key]);
          return (
            <div key={group.key} className="space-y-0.5">
              <button
                type="button"
                aria-expanded={!isCollapsed}
                onClick={() => toggleGroup(group.key)}
                className="flex w-full items-center gap-1 rounded px-2 py-1 text-[11px] font-semibold text-ink-muted transition-colors hover:bg-surface-muted hover:text-ink"
              >
                {isCollapsed ? (
                  <ChevronRight className="h-3 w-3 shrink-0 opacity-70" aria-hidden="true" />
                ) : (
                  <ChevronDown className="h-3 w-3 shrink-0 opacity-70" aria-hidden="true" />
                )}
                <span className="truncate">{group.label}</span>
              </button>

              {!isCollapsed ? (
                <ul className="space-y-0.5 pt-0.5">
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
                              'flex items-center gap-2 rounded-md px-2.5 py-1.5 text-[13px] transition-colors',
                              'focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-ring',
                              isActive
                                ? 'bg-surface-muted font-semibold text-ink'
                                : 'font-normal text-ink-secondary hover:bg-surface-muted hover:text-ink',
                            )
                          }
                        >
                          <IconComp className="h-4 w-4 shrink-0 opacity-80" aria-hidden="true" />
                          <span className="truncate">{item.label}</span>
                        </NavLink>
                      </li>
                    );
                  })}
                </ul>
              ) : null}
            </div>
          );
        })}
      </nav>

      {/* 底部会话入口（涵盖 PRD 模块 1：登录与会话） */}
      <div className="border-t border-line p-2.5">
        <NavLink
          to={LOGIN_NAV_ITEM.path}
          onClick={() => setMobileMenuOpen(false)}
          className={({ isActive }) =>
            cn(
              'flex items-center gap-2 rounded-md px-2.5 py-1.5 text-[13px] transition-colors',
              isActive
                ? 'bg-surface-muted font-semibold text-ink'
                : 'text-ink-secondary hover:bg-surface-muted hover:text-ink',
            )
          }
        >
          <LogIn className="h-4 w-4 shrink-0 opacity-80" aria-hidden="true" />
          <span>{LOGIN_NAV_ITEM.label}</span>
        </NavLink>
      </div>
    </div>
  );

  return (
    <div className="flex min-h-screen bg-surface text-ink">
      {/* 桌面端 Notion 风格极浅暖灰侧边栏 */}
      <aside className="hidden w-60 shrink-0 border-r border-line bg-surface-sidebar lg:block">
        <div className="sticky top-0 h-screen">{renderSidebarNav()}</div>
      </aside>

      {/* 移动端抽屉侧边栏 */}
      {mobileMenuOpen ? (
        <div className="fixed inset-0 z-50 flex lg:hidden">
          <div
            className="fixed inset-0 bg-ink/25 backdrop-blur-[1px]"
            onClick={() => setMobileMenuOpen(false)}
          />
          <aside className="relative z-10 w-64 max-w-[82vw] border-r border-line bg-surface-sidebar shadow-md">
            {renderSidebarNav()}
          </aside>
        </div>
      ) : null}

      {/* 右侧纯白主画布 */}
      <div className="flex min-w-0 flex-1 flex-col bg-surface">
        <header className="sticky top-0 z-30 flex h-12 items-center justify-between gap-3 border-b border-line bg-surface/95 px-4 backdrop-blur-xs lg:px-8">
          {/* 左侧：移动端菜单按钮 + Organization 工作区切换器 + 面包屑 */}
          <div className="flex min-w-0 items-center gap-2.5">
            <button
              type="button"
              aria-label="打开导航菜单"
              className="inline-flex h-7 w-7 items-center justify-center rounded-md text-ink-secondary hover:bg-surface-muted lg:hidden"
              onClick={() => setMobileMenuOpen(true)}
            >
              <Menu className="h-4 w-4" aria-hidden="true" />
            </button>

            <div className="flex items-center gap-1.5">
              <Building2 className="hidden h-3.5 w-3.5 text-ink-muted sm:block" aria-hidden="true" />
              <div className="w-44 sm:w-52">
                <Select
                  value={activeOrgId}
                  disabled={isSwitchingOrg || organizations.length === 0}
                  onValueChange={(value) => {
                    if (typeof value === 'string') {
                      void handleOrganizationChange(value);
                    }
                  }}
                >
                  <SelectTrigger
                    aria-label="切换当前 Organization"
                    className="h-7 border-transparent bg-transparent px-2 text-xs font-medium shadow-none hover:border-line hover:bg-surface-muted"
                  >
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

            {currentNavItem ? (
              <div className="hidden items-center gap-1.5 text-xs text-ink-muted xl:flex">
                <span>/</span>
                <span className="truncate font-medium text-ink">
                  {activeOrg?.slug ? `${currentNavItem.label}` : currentNavItem.label}
                </span>
              </div>
            ) : null}
          </div>

          {/* 右侧：/api/v1/readyz 实时健康状态胶囊 + 用户菜单与角色徽章 */}
          <div className="flex items-center gap-2">
            <div
              className="flex items-center gap-1"
              title="后端就绪探针 GET /api/v1/readyz"
              data-testid="backend-readiness-indicator"
            >
              <Badge variant={readinessBadge.variant} className="gap-1.5 py-0.5">
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

            <div className="h-3.5 w-px bg-line" aria-hidden="true" />

            {/* 用户身份与角色菜单区 */}
            <div className="flex items-center gap-2" data-testid="user-menu">
              <div className="hidden text-right md:block">
                <div className="flex items-center justify-end gap-1">
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
              <UserCircle2 className="h-4 w-4 text-ink-secondary" aria-hidden="true" />
              {isAuthenticated ? (
                <>
                  <Dialog
                    open={changePwdOpen}
                    onOpenChange={(open) => {
                      setChangePwdOpen(open);
                      if (!open) {
                        setPwdFeedback(null);
                        setCurrentPwd('');
                        setNewPwd('');
                        setConfirmNewPwd('');
                      }
                    }}
                  >
                    <DialogTrigger
                      render={
                        <Button
                          variant="ghost"
                          size="sm"
                          className="h-7 px-2 text-xs"
                          title="修改当前账号密码"
                        >
                          <KeyRound className="h-3.5 w-3.5" aria-hidden="true" />
                          <span className="hidden sm:inline">改密</span>
                        </Button>
                      }
                    />
                    <DialogContent>
                      <DialogHeader>
                        <DialogTitle>修改登录密码</DialogTitle>
                        <DialogDescription>
                          更新 {user?.email ?? '当前账号'} 的密码后，系统将自动吊销其他设备的活跃会话以保障账号安全。
                        </DialogDescription>
                      </DialogHeader>
                      <form onSubmit={(ev) => void handleChangeOwnPassword(ev)} className="space-y-3.5">
                        {pwdFeedback ? (
                          <Alert
                            variant={pwdFeedback.variant}
                            title={pwdFeedback.variant === 'success' ? '修改成功' : '修改失败'}
                          >
                            {pwdFeedback.message}
                          </Alert>
                        ) : null}
                        <div className="space-y-1">
                          <label htmlFor="self-cur-pwd" className="block text-xs font-medium text-ink">
                            当前密码
                          </label>
                          <Input
                            id="self-cur-pwd"
                            type="password"
                            autoComplete="current-password"
                            required
                            value={currentPwd}
                            onChange={(ev) => setCurrentPwd(ev.target.value)}
                          />
                        </div>
                        <div className="space-y-1">
                          <label htmlFor="self-new-pwd" className="block text-xs font-medium text-ink">
                            新密码
                          </label>
                          <Input
                            id="self-new-pwd"
                            type="password"
                            autoComplete="new-password"
                            placeholder="至少 8 位，需包含字母和数字"
                            required
                            value={newPwd}
                            onChange={(ev) => setNewPwd(ev.target.value)}
                          />
                        </div>
                        <div className="space-y-1">
                          <label htmlFor="self-confirm-pwd" className="block text-xs font-medium text-ink">
                            确认新密码
                          </label>
                          <Input
                            id="self-confirm-pwd"
                            type="password"
                            autoComplete="new-password"
                            required
                            value={confirmNewPwd}
                            onChange={(ev) => setConfirmNewPwd(ev.target.value)}
                          />
                        </div>
                        <DialogFooter>
                          <DialogClose render={<Button variant="outline" type="button">关闭</Button>} />
                          <Button type="submit" loading={isChangingPwd}>
                            保存新密码
                          </Button>
                        </DialogFooter>
                      </form>
                    </DialogContent>
                  </Dialog>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-7 px-2 text-xs"
                    loading={isLoggingOut}
                    onClick={() => void handleLogout()}
                  >
                    <LogOut className="h-3.5 w-3.5" aria-hidden="true" />
                    退出
                  </Button>
                </>
              ) : (
                <Button
                  variant="outline"
                  size="sm"
                  className="h-7 px-2.5 text-xs"
                  onClick={() => navigate('/login')}
                >
                  登录
                </Button>
              )}
            </div>
          </div>
        </header>

        {/* 组织切换失败提示横幅 */}
        {switchError ? (
          <div className="mx-auto w-full max-w-6xl px-4 pt-4 lg:px-10">
            <Alert variant="danger" title="组织切换受阻">
              {switchError}
            </Alert>
          </div>
        ) : null}

        {/* 主内容视图区（Notion 宽敞阅读与操作留白） */}
        <main className="mx-auto w-full max-w-6xl flex-1 px-4 py-8 lg:px-10">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
