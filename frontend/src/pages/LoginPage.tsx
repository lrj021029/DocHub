import { useState } from 'react';
import { zodResolver } from '@hookform/resolvers/zod';
import { Building2, LockKeyhole, ShieldCheck } from 'lucide-react';
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

/** 登录表单 Zod 校验规则 */
const loginSchema = z.object({
  email: z.email('请输入有效的企业邮箱地址'),
  password: z.string().min(8, '密码长度不少于 8 位'),
});

type LoginFormValues = z.infer<typeof loginSchema>;

/**
 * PRD 模块 1：登录与当前 Organization 会话绑定页。
 * 安全约束：
 * - 真实对接 POST /api/v1/auth/login；
 * - 登录成功后 Access Token 仅保存在内存（auth-store），绝不写入 localStorage/sessionStorage；
 * - Refresh Token 由后端写入 HttpOnly Cookie，同时保存 csrf_token（兼容 dochub_csrf Cookie）。
 */
export function LoginPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const { setSession, isAuthenticated, user, currentOrgId } = useAuth();
  const [submitError, setSubmitError] = useState<string | null>(null);

  const redirectTarget =
    (location.state as { from?: string } | null)?.from &&
    (location.state as { from?: string }).from !== '/login'
      ? (location.state as { from?: string }).from!
      : '/';

  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<LoginFormValues>({
    resolver: zodResolver(loginSchema),
    defaultValues: {
      email: '',
      password: '',
    },
  });

  const onSubmit = async (values: LoginFormValues) => {
    setSubmitError(null);
    try {
      const data = await apiClient.post<AuthEndpointPayload>('/api/v1/auth/login', values, {
        skipAuthRefresh: true,
      });

      const accessToken = data.access_token ?? data.accessToken;
      if (!accessToken) {
        setSubmitError('认证响应缺少 access_token，请联系管理员检查服务端配置。');
        return;
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
        email: values.email,
        displayName: values.email.split('@')[0] ?? values.email,
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

      // 若登录响应未附带完整权限清单，自动拉取一次 /api/v1/auth/me 补齐角色与权限
      if (normalizedUser.permissions.length === 0) {
        await fetchCurrentUserProfile();
      }

      navigate(redirectTarget, { replace: true });
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

  return (
    <div className="flex min-h-screen items-center justify-center bg-surface px-4 py-10">
      <div className="w-full max-w-md">
        <div className="mb-6 space-y-2 text-center">
          <div className="mx-auto flex h-11 w-11 items-center justify-center rounded-xl bg-accent text-ink-inverted shadow-sm">
            <ShieldCheck className="h-5 w-5" aria-hidden="true" />
          </div>
          <h1 className="text-xl font-semibold tracking-tight text-ink">登录 DocHub 知识平台</h1>
          <p className="text-xs text-ink-muted">
            单请求绑定单一 Organization · Access Token 仅存内存 · Refresh Token 托管于 HttpOnly Cookie
          </p>
        </div>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center justify-between">
              <span>企业账号认证</span>
              <Badge variant="outline" className="font-mono text-[10px]">
                PRD-01
              </Badge>
            </CardTitle>
            <CardDescription>
              请输入企业邮箱与密码。登录态刷新与登出撤销均通过后端权威会话表与 CSRF 校验。
            </CardDescription>
          </CardHeader>

          <CardContent className="space-y-4">
            {isAuthenticated ? (
              <Alert variant="success" title="当前内存会话已激活">
                当前用户：{user?.email ?? '已认证'} · 当前组织：{currentOrgId ?? '待切换'}
              </Alert>
            ) : null}

            {submitError ? (
              <Alert variant="danger" title="登录未完成">
                {submitError}
              </Alert>
            ) : null}

            <form onSubmit={handleSubmit(onSubmit)} className="space-y-4" noValidate>
              <div className="space-y-1.5">
                <label htmlFor="login-email" className="block text-xs font-medium text-ink">
                  企业邮箱
                </label>
                <Input
                  id="login-email"
                  type="email"
                  autoComplete="email"
                  placeholder="admin@acme.local"
                  invalid={Boolean(errors.email)}
                  {...register('email')}
                />
                {errors.email ? (
                  <p className="text-xs text-danger">{errors.email.message}</p>
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
                  invalid={Boolean(errors.password)}
                  {...register('password')}
                />
                {errors.password ? (
                  <p className="text-xs text-danger">{errors.password.message}</p>
                ) : null}
              </div>

              <Button type="submit" className="w-full" loading={isSubmitting}>
                <LockKeyhole className="h-4 w-4" aria-hidden="true" />
                安全登录
              </Button>
            </form>
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
