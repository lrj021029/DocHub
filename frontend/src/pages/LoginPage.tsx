import { useState } from 'react';
import { zodResolver } from '@hookform/resolvers/zod';
import { Building2, LockKeyhole, ShieldCheck, UserPlus } from 'lucide-react';
import { useForm } from 'react-hook-form';
import { useLocation, useNavigate } from 'react-router-dom';
import { z } from 'zod';
import {
  Alert,
  Badge,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
  Input,
} from '@/components/ui';
import { ApiError, apiClient } from '@/lib/api-client';
import {
  fetchCurrentUserProfile,
  normalizeAuthUser,
  normalizeOrganizationList,
  readCsrfCookie,
  useAuth,
  type AuthEndpointPayload,
} from '@/lib/auth-store';
import { cn } from '@/lib/cn';

/** 登录表单 Zod 校验规则 */
const loginSchema = z.object({
  email: z.email('请输入有效的企业邮箱地址'),
  password: z.string().min(8, '密码长度不少于 8 位'),
});

type LoginFormValues = z.infer<typeof loginSchema>;

/** 自助注册表单 Zod 校验规则 */
const registerSchema = z
  .object({
    email: z.email('请输入有效的企业邮箱地址'),
    display_name: z
      .string()
      .trim()
      .min(2, '显示名称不少于 2 个字符')
      .max(64, '显示名称不超过 64 个字符'),
    password: z
      .string()
      .min(8, '密码至少 8 位')
      .max(128, '密码不超过 128 位')
      .refine(
        (val) => /[A-Za-z]/.test(val) && /[0-9]/.test(val),
        '密码必须同时包含至少一个英文字母和一位数字',
      ),
    confirm_password: z.string().min(1, '请再次确认登录密码'),
    organization_slug: z
      .string()
      .trim()
      .max(64, '组织标识不超过 64 个字符')
      .optional(),
  })
  .refine((data) => data.password === data.confirm_password, {
    message: '两次输入的密码不一致',
    path: ['confirm_password'],
  });

type RegisterFormValues = z.infer<typeof registerSchema>;

/**
 * PRD 模块 1：登录、自助注册与当前 Organization 会话绑定页（Notion 极简风格）。
 * 安全约束：
 * - 真实对接 POST /api/v1/auth/login 与 POST /api/v1/auth/register；
 * - Access Token 仅保存在内存（auth-store），绝不写入 localStorage/sessionStorage；
 * - Refresh Token 由后端写入 HttpOnly Cookie，同时保存 csrf_token（兼容 dochub_csrf Cookie）；
 * - 自助注册仅授予目标组织最小基础 EMPLOYEE 权限，严禁提权或泄露受限机密文档。
 */
export function LoginPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const { setSession, isAuthenticated, user, currentOrgId } = useAuth();
  const [authMode, setAuthMode] = useState<'login' | 'register'>('login');
  const [submitError, setSubmitError] = useState<string | null>(null);

  const redirectTarget =
    (location.state as { from?: string } | null)?.from &&
    (location.state as { from?: string }).from !== '/login'
      ? (location.state as { from?: string }).from!
      : '/';

  const loginForm = useForm<LoginFormValues>({
    resolver: zodResolver(loginSchema),
    defaultValues: {
      email: '',
      password: '',
    },
  });

  const registerForm = useForm<RegisterFormValues>({
    resolver: zodResolver(registerSchema),
    defaultValues: {
      email: '',
      display_name: '',
      password: '',
      confirm_password: '',
      organization_slug: 'acme-corp',
    },
  });

  /** 统一写入内存认证态并补齐 /api/v1/auth/me 全景权限 */
  const applyAuthSessionAndNavigate = async (
    data: AuthEndpointPayload,
    fallbackEmail: string,
  ): Promise<boolean> => {
    const accessToken = data.access_token ?? data.accessToken;
    if (!accessToken) {
      setSubmitError('认证响应缺少 access_token，请联系管理员检查服务端配置。');
      return false;
    }

    const organizations = normalizeOrganizationList(data.organizations);
    const activeOrgId =
      data.active_organization_id ??
      data.current_organization_id ??
      data.currentOrgId ??
      organizations[0]?.id ??
      null;

    const normalizedUser = normalizeAuthUser(data.user, data) ?? {
      id: 'pending-profile',
      email: fallbackEmail,
      displayName: fallbackEmail.split('@')[0] ?? fallbackEmail,
      systemRoles: [],
      organizationRoles: [],
      permissions: [],
    };

    const resolvedCsrf = data.csrf_token ?? data.csrfToken ?? readCsrfCookie();

    setSession({
      accessToken,
      csrfToken: resolvedCsrf,
      user: normalizedUser,
      currentOrgId: activeOrgId,
      organizations,
      hasBootstrapped: true,
    });

    if (normalizedUser.permissions.length === 0) {
      await fetchCurrentUserProfile();
    }

    navigate(redirectTarget, { replace: true });
    return true;
  };

  const onLoginSubmit = async (values: LoginFormValues) => {
    setSubmitError(null);
    try {
      const data = await apiClient.post<AuthEndpointPayload>('/api/v1/auth/login', values, {
        skipAuthRefresh: true,
      });
      await applyAuthSessionAndNavigate(data, values.email);
    } catch (error) {
      if (error instanceof ApiError) {
        if (error.status === 401) {
          setSubmitError('邮箱或密码不正确，或账号已被停用。');
        } else {
          setSubmitError(`${error.message}（错误码：${error.code}）`);
        }
      } else {
        setSubmitError('无法连接认证服务（POST /api/v1/auth/login），请确认后端 API 已启动。');
      }
    }
  };

  const onRegisterSubmit = async (values: RegisterFormValues) => {
    setSubmitError(null);
    try {
      const data = await apiClient.post<AuthEndpointPayload>(
        '/api/v1/auth/register',
        {
          email: values.email,
          display_name: values.display_name,
          password: values.password,
          organization_slug: values.organization_slug?.trim() || 'acme-corp',
        },
        { skipAuthRefresh: true },
      );
      await applyAuthSessionAndNavigate(data, values.email);
    } catch (error) {
      if (error instanceof ApiError) {
        if (error.status === 409) {
          setSubmitError('该邮箱已注册，请直接切换至「账号登录」或更换其他邮箱。');
        } else {
          setSubmitError(`${error.message}（错误码：${error.code}）`);
        }
      } else {
        setSubmitError('无法连接注册服务（POST /api/v1/auth/register），请确认后端 API 已启动。');
      }
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-surface px-4 py-10">
      <div className="w-full max-w-md">
        <div className="mb-6 space-y-2 text-center">
          <div className="mx-auto flex h-11 w-11 items-center justify-center rounded-xl bg-accent text-ink-inverted shadow-sm">
            <ShieldCheck className="h-5 w-5" aria-hidden="true" />
          </div>
          <h1 className="text-xl font-semibold tracking-tight text-ink">
            {authMode === 'login' ? '登录 DocHub 知识平台' : '注册 DocHub 企业账号'}
          </h1>
          <p className="text-xs text-ink-muted">
            单请求绑定单一 Organization · Access Token 仅存内存 · Refresh Token 托管于 HttpOnly Cookie
          </p>
        </div>

        <Card>
          <CardHeader className="space-y-3">
            <div className="flex items-center justify-between">
              <CardTitle>企业账号认证</CardTitle>
              <Badge variant="outline" className="font-mono text-[10px]">
                PRD-01
              </Badge>
            </div>

            {/* Notion 风格极简模式切换胶囊 */}
            <div
              role="tablist"
              aria-label="认证方式切换"
              className="grid grid-cols-2 rounded-lg border border-line bg-surface-subtle p-1"
            >
              <button
                type="button"
                role="tab"
                aria-selected={authMode === 'login'}
                onClick={() => {
                  setAuthMode('login');
                  setSubmitError(null);
                }}
                className={cn(
                  'rounded-md px-3 py-1.5 text-xs font-medium transition-colors',
                  authMode === 'login'
                    ? 'bg-surface text-ink shadow-xs'
                    : 'text-ink-muted hover:text-ink',
                )}
              >
                账号登录
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={authMode === 'register'}
                onClick={() => {
                  setAuthMode('register');
                  setSubmitError(null);
                }}
                className={cn(
                  'rounded-md px-3 py-1.5 text-xs font-medium transition-colors',
                  authMode === 'register'
                    ? 'bg-surface text-ink shadow-xs'
                    : 'text-ink-muted hover:text-ink',
                )}
              >
                注册新账号
              </button>
            </div>

            <CardDescription>
              {authMode === 'login'
                ? '请输入企业邮箱与密码。登录态刷新与登出撤销均通过后端权威会话表与 CSRF 校验。'
                : '自助注册默认加入指定组织并仅授予基础 EMPLOYEE 权限，不包含受限机密文档访问权。'}
            </CardDescription>
          </CardHeader>

          <CardContent className="space-y-4">
            {isAuthenticated ? (
              <Alert variant="success" title="当前内存会话已激活">
                当前用户：{user?.email ?? '已认证'} · 当前组织：{currentOrgId ?? '待切换'}
              </Alert>
            ) : null}

            {submitError ? (
              <Alert
                variant="danger"
                title={authMode === 'login' ? '登录未完成' : '注册未完成'}
              >
                {submitError}
              </Alert>
            ) : null}

            {authMode === 'login' ? (
              <form
                onSubmit={loginForm.handleSubmit(onLoginSubmit)}
                className="space-y-4"
                noValidate
              >
                <div className="space-y-1.5">
                  <label htmlFor="login-email" className="block text-xs font-medium text-ink">
                    企业邮箱
                  </label>
                  <Input
                    id="login-email"
                    type="email"
                    autoComplete="email"
                    placeholder="admin@acme.local"
                    invalid={Boolean(loginForm.formState.errors.email)}
                    {...loginForm.register('email')}
                  />
                  {loginForm.formState.errors.email ? (
                    <p className="text-xs text-danger">
                      {loginForm.formState.errors.email.message}
                    </p>
                  ) : null}
                </div>

                <div className="space-y-1.5">
                  <label htmlFor="login-password" className="block text-xs font-medium text-ink">
                    登录密码
                  </label>
                  <Input
                    id="login-password"
                    type="password"
                    autoComplete="current-password"
                    placeholder="请输入密码（不少于 8 位）"
                    invalid={Boolean(loginForm.formState.errors.password)}
                    {...loginForm.register('password')}
                  />
                  {loginForm.formState.errors.password ? (
                    <p className="text-xs text-danger">
                      {loginForm.formState.errors.password.message}
                    </p>
                  ) : null}
                </div>

                <Button
                  type="submit"
                  className="w-full"
                  loading={loginForm.formState.isSubmitting}
                >
                  <LockKeyhole className="h-4 w-4" aria-hidden="true" />
                  安全登录
                </Button>
              </form>
            ) : (
              <form
                onSubmit={registerForm.handleSubmit(onRegisterSubmit)}
                className="space-y-3.5"
                noValidate
              >
                <div className="space-y-1">
                  <label htmlFor="reg-email" className="block text-xs font-medium text-ink">
                    企业邮箱
                  </label>
                  <Input
                    id="reg-email"
                    type="email"
                    autoComplete="email"
                    placeholder="alice@acme.local"
                    invalid={Boolean(registerForm.formState.errors.email)}
                    {...registerForm.register('email')}
                  />
                  {registerForm.formState.errors.email ? (
                    <p className="text-xs text-danger">
                      {registerForm.formState.errors.email.message}
                    </p>
                  ) : null}
                </div>

                <div className="space-y-1">
                  <label htmlFor="reg-display-name" className="block text-xs font-medium text-ink">
                    显示姓名
                  </label>
                  <Input
                    id="reg-display-name"
                    type="text"
                    autoComplete="name"
                    placeholder="例如：林晓晴"
                    invalid={Boolean(registerForm.formState.errors.display_name)}
                    {...registerForm.register('display_name')}
                  />
                  {registerForm.formState.errors.display_name ? (
                    <p className="text-xs text-danger">
                      {registerForm.formState.errors.display_name.message}
                    </p>
                  ) : null}
                </div>

                <div className="space-y-1">
                  <label htmlFor="reg-password" className="block text-xs font-medium text-ink">
                    设置登录密码
                  </label>
                  <Input
                    id="reg-password"
                    type="password"
                    autoComplete="new-password"
                    placeholder="至少 8 位，需同时包含英文字母和数字"
                    invalid={Boolean(registerForm.formState.errors.password)}
                    {...registerForm.register('password')}
                  />
                  {registerForm.formState.errors.password ? (
                    <p className="text-xs text-danger">
                      {registerForm.formState.errors.password.message}
                    </p>
                  ) : null}
                </div>

                <div className="space-y-1">
                  <label
                    htmlFor="reg-confirm-password"
                    className="block text-xs font-medium text-ink"
                  >
                    确认登录密码
                  </label>
                  <Input
                    id="reg-confirm-password"
                    type="password"
                    autoComplete="new-password"
                    placeholder="请再次输入登录密码"
                    invalid={Boolean(registerForm.formState.errors.confirm_password)}
                    {...registerForm.register('confirm_password')}
                  />
                  {registerForm.formState.errors.confirm_password ? (
                    <p className="text-xs text-danger">
                      {registerForm.formState.errors.confirm_password.message}
                    </p>
                  ) : null}
                </div>

                <div className="space-y-1">
                  <label htmlFor="reg-org-slug" className="block text-xs font-medium text-ink">
                    加入企业组织标识（Slug）
                  </label>
                  <Input
                    id="reg-org-slug"
                    type="text"
                    placeholder="acme-corp"
                    invalid={Boolean(registerForm.formState.errors.organization_slug)}
                    {...registerForm.register('organization_slug')}
                  />
                  <p className="text-[11px] text-ink-muted">
                    默认加入主演示组织 <code>acme-corp</code>，注册后仅获得公开知识库问答权限。
                  </p>
                </div>

                <Button
                  type="submit"
                  className="w-full"
                  loading={registerForm.formState.isSubmitting}
                >
                  <UserPlus className="h-4 w-4" aria-hidden="true" />
                  创建账号并登录
                </Button>
              </form>
            )}
          </CardContent>

          <CardFooter className="flex-col items-start gap-1.5 text-[11px] text-ink-muted">
            <div className="flex items-center gap-1.5">
              <Building2 className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
              <span>登录后可在顶部组织切换器切换所属 Organization，严禁默认跨组织检索。</span>
            </div>
          </CardFooter>
        </Card>
      </div>
    </div>
  );
}
