import { useEffect, useState, type ReactNode } from 'react';
import { Navigate, useLocation } from 'react-router-dom';
import { Skeleton } from '@/components/ui';
import { useAuth } from '@/lib/auth-store';

export interface RequireAuthProps {
  children: ReactNode;
}

/**
 * 受保护路由守卫：
 * - 若内存中已持有 Access Token，直接放行（若尚未加载用户详情则后台静默拉取 /auth/me）；
 * - 若内存中无 Token，在跳转 /login 前先执行一次静默 POST /api/v1/auth/refresh（基于 HttpOnly Cookie + CSRF）；
 * - 静默续期失败则跳转 /login 并保留原始目标路径。
 */
export function RequireAuth({ children }: RequireAuthProps) {
  const location = useLocation();
  const { isAuthenticated, hasBootstrapped, user, refreshSession, refreshProfile } = useAuth();
  const [checking, setChecking] = useState(!isAuthenticated && !hasBootstrapped);

  useEffect(() => {
    let active = true;

    async function bootstrap() {
      if (isAuthenticated) {
        if (!user) {
          await refreshProfile();
        }
        if (active) setChecking(false);
        return;
      }

      if (hasBootstrapped) {
        if (active) setChecking(false);
        return;
      }

      setChecking(true);
      const token = await refreshSession();
      if (token) {
        await refreshProfile();
      }
      if (active) {
        setChecking(false);
      }
    }

    void bootstrap();
    return () => {
      active = false;
    };
  }, [hasBootstrapped, isAuthenticated, refreshProfile, refreshSession, user]);

  if (checking) {
    return (
      <div
        className="flex min-h-screen items-center justify-center bg-surface p-6"
        aria-busy="true"
        data-testid="auth-bootstrap-loading"
      >
        <div className="w-full max-w-sm space-y-3 rounded-xl border border-line bg-surface-elevated p-6 shadow-xs">
          <p className="text-sm font-medium text-ink">正在校验安全会话...</p>
          <p className="text-xs text-ink-muted">
            正在通过 HttpOnly Cookie 尝试静默续期，不依赖本地明文存储。
          </p>
          <Skeleton shape="line" className="w-3/4" />
          <Skeleton shape="block" className="h-10 w-full" />
        </div>
      </div>
    );
  }

  if (!isAuthenticated) {
    return (
      <Navigate
        to="/login"
        replace
        state={{ from: `${location.pathname}${location.search}` }}
      />
    );
  }

  return <>{children}</>;
}
