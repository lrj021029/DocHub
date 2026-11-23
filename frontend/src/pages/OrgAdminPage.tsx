import { Building2, FolderTree, ShieldCheck, UserCog, Users } from 'lucide-react';
import {
  Alert,
  Badge,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from '@/components/ui';
import { DepartmentsTab } from '@/features/iam/DepartmentsTab';
import { MembersTab } from '@/features/iam/MembersTab';
import { RolesPermissionsTab } from '@/features/iam/RolesPermissionsTab';
import { UsersAdminTab } from '@/features/iam/UsersAdminTab';
import { useAuth } from '@/lib/auth-store';

/**
 * PRD 模块 3（Slice 1 真实对接）：Organization / Department / User / Role 管理台。
 * 严格绑定当前活跃 Organization，通过三个独立选项卡覆盖：
 * 1. 成员管理（筛选、邀请添加、启用/停用）
 * 2. 部门树与多部门兼职（树形层级、建子部门、调父部门、多部门兼职与有效期）
 * 3. 组织角色与权限树（SystemRole / OrganizationRole 物理分离展示与角色授权）
 */
export function OrgAdminPage() {
  const { currentOrgId, organizations, user } = useAuth();
  const activeOrg = organizations.find((org) => org.id === currentOrgId) ?? null;

  return (
    <div className="space-y-5">
      {/* 页面头部上下文 */}
      <div className="flex flex-col gap-2 border-b border-line pb-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="space-y-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="outline" className="font-mono text-[10px]">
              PRD-03
            </Badge>
            <h1 className="text-xl font-semibold tracking-tight text-ink">
              组织、部门与角色管理
            </h1>
            <Badge variant="neutral">FORCE RLS 强隔离</Badge>
            <Badge variant="warning">SystemRole ≠ 文档读权</Badge>
          </div>
          <p className="text-sm text-ink-secondary">
            所有列表与变更均严格限定在当前 Organization 内执行；按钮按权限优雅显隐，后端仍强制二次鉴权。
          </p>
          <p className="font-mono text-[11px] text-ink-muted">
            当前活跃组织：{activeOrg ? `${activeOrg.name} (${activeOrg.id})` : currentOrgId ?? '未绑定'} · 当前用户：{user?.email ?? '—'}
          </p>
        </div>
      </div>

      {!currentOrgId ? (
        <Alert variant="warning" title="尚未绑定当前 Organization">
          请先在页面顶部「当前组织」切换器中选择一个有效组织，或重新登录以初始化组织上下文。
        </Alert>
      ) : (
        <Tabs defaultValue="members">
          <TabsList aria-label="组织治理模块切换">
            <TabsTrigger value="members" className="gap-1.5">
              <Users className="h-3.5 w-3.5" aria-hidden="true" />
              成员管理
            </TabsTrigger>
            <TabsTrigger value="departments" className="gap-1.5">
              <FolderTree className="h-3.5 w-3.5" aria-hidden="true" />
              部门树与多部门兼职
            </TabsTrigger>
            <TabsTrigger value="roles" className="gap-1.5">
              <ShieldCheck className="h-3.5 w-3.5" aria-hidden="true" />
              组织角色与权限树
            </TabsTrigger>
            <TabsTrigger value="platform-users" className="gap-1.5">
              <UserCog className="h-3.5 w-3.5" aria-hidden="true" />
              平台账号管理
            </TabsTrigger>
          </TabsList>

          <TabsContent value="members">
            <MembersTab orgId={currentOrgId} />
          </TabsContent>

          <TabsContent value="departments">
            <DepartmentsTab orgId={currentOrgId} />
          </TabsContent>

          <TabsContent value="roles">
            <RolesPermissionsTab orgId={currentOrgId} />
          </TabsContent>

          <TabsContent value="platform-users">
            <UsersAdminTab currentOrgId={currentOrgId} currentOrgName={activeOrg?.name ?? null} />
          </TabsContent>
        </Tabs>
      )}

      <div className="flex items-center gap-2 text-[11px] text-ink-muted">
        <Building2 className="h-3.5 w-3.5" aria-hidden="true" />
        <span>
          后端 API：/api/v1/users · /api/v1/organizations/&#123;org_id&#125;/members · /departments · /roles · /api/v1/permissions
        </span>
      </div>
    </div>
  );
}
