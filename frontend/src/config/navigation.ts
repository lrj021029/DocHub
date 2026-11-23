import {
  Activity,
  BarChart3,
  BookCheck,
  Building2,
  ClipboardCheck,
  FileText,
  FolderKanban,
  Gauge,
  HelpCircle,
  LayoutDashboard,
  LogIn,
  MessageSquareText,
  Settings2,
  ShieldCheck,
  Sparkles,
  type LucideIcon,
} from 'lucide-react';

/** 导航分组定义 */
export type NavGroupKey = 'workspace' | 'governance' | 'knowledge' | 'intelligence' | 'operations';

/** 单个模块导航项定义（严格映射 PRD 第 8.1 节全部 14 个页面模块） */
export interface NavItemConfig {
  /** 唯一标识，供路由与自动化测试定位 */
  id: string;
  /** 路由路径 */
  path: string;
  /** 侧边栏展示名称 */
  label: string;
  /** 所属导航分组 */
  group: NavGroupKey;
  /** 图标组件 */
  icon: LucideIcon;
  /** 简短职责描述 */
  description: string;
  /** 对应 PRD 章节序号（1-14） */
  prdModuleNo: number;
}

/** 导航分组中文标题 */
export const NAV_GROUP_LABELS: Record<NavGroupKey, string> = {
  workspace: '核心工作台',
  governance: '组织与权限治理',
  knowledge: '知识资产维护',
  intelligence: 'FAQ 与知识闭环',
  operations: '运营、配置与评测',
};

/**
 * 全部 14 个 PRD 页面模块的权威导航清单。
 * AppShell 侧边栏渲染除登录页外的 13 个工作台入口，并在底部账号区提供登录/切换账号入口。
 */
export const WORKSPACE_NAV_ITEMS: readonly NavItemConfig[] = [
  {
    id: 'dashboard',
    path: '/',
    label: '总览工作台',
    group: 'workspace',
    icon: LayoutDashboard,
    description: '当前 Organization 知识资产、健康状态与核心入口总览',
    prdModuleNo: 2,
  },
  {
    id: 'chat',
    path: '/chat',
    label: 'AI 问答工作台',
    group: 'workspace',
    icon: MessageSquareText,
    description: '多 Space 选择、SSE 流式回答、引用溯源与实时权限校验',
    prdModuleNo: 7,
  },
  {
    id: 'org-admin',
    path: '/org-admin',
    label: '组织与角色管理',
    group: 'governance',
    icon: Building2,
    description: '多 Organization、部门树、多部门成员、SystemRole 与 OrganizationRole 分离管理',
    prdModuleNo: 3,
  },
  {
    id: 'spaces',
    path: '/spaces',
    label: 'Knowledge Space 管理',
    group: 'governance',
    icon: FolderKanban,
    description: '公开/私有入口策略、Space 成员授权与检索回答策略配置',
    prdModuleNo: 4,
  },
  {
    id: 'acl',
    path: '/acl',
    label: '文档权限 ACL',
    group: 'governance',
    icon: ShieldCheck,
    description: 'Global / Department / Role / User 四维 Allow OR 授权与 Revision 追溯',
    prdModuleNo: 6,
  },
  {
    id: 'documents',
    path: '/documents',
    label: '文档中心',
    group: 'knowledge',
    icon: FileText,
    description: '多源上传导入、版本台账、分阶段解析进度与 Parent-Child Chunk 预览',
    prdModuleNo: 5,
  },
  {
    id: 'faq-mining',
    path: '/faq-mining',
    label: 'FAQ 挖掘审核',
    group: 'intelligence',
    icon: Sparkles,
    description: '实时/定时/手动挖掘触发、HDBSCAN 聚类与双人人工审核队列',
    prdModuleNo: 8,
  },
  {
    id: 'faq-published',
    path: '/faq-published',
    label: '已发布 FAQ',
    group: 'intelligence',
    icon: BookCheck,
    description: 'Lifecycle / Publication / Source Health 三轴治理与缓存版本状态',
    prdModuleNo: 9,
  },
  {
    id: 'knowledge-gaps',
    path: '/knowledge-gaps',
    label: 'Knowledge Gap',
    group: 'intelligence',
    icon: HelpCircle,
    description: '无召回/证据不足缺口聚合、补充任务流转与回归关闭验收',
    prdModuleNo: 10,
  },
  {
    id: 'analytics',
    path: '/analytics',
    label: '运营分析',
    group: 'operations',
    icon: BarChart3,
    description: '按组织隔离的 PV/UV、FAQ 命中率、Token 耗时与引用热度指标',
    prdModuleNo: 11,
  },
  {
    id: 'settings',
    path: '/settings',
    label: '系统配置',
    group: 'operations',
    icon: Settings2,
    description: 'LLM / Qwen / Local BGE Provider、MinerU API 与 L0-L3 外发策略配置',
    prdModuleNo: 12,
  },
  {
    id: 'audit',
    path: '/audit',
    label: '审计中心',
    group: 'operations',
    icon: ClipboardCheck,
    description: '业务审计与安全审计分离视图（ACL Deny、Break-glass、外发拦截）',
    prdModuleNo: 13,
  },
  {
    id: 'benchmark',
    path: '/benchmark',
    label: 'Benchmark',
    group: 'operations',
    icon: Gauge,
    description: 'RAG 评测指标、MinerU 解析回归与 ACL Violation = 0 硬门槛校验',
    prdModuleNo: 14,
  },
] as const;

/** 登录与认证页面导航元信息（PRD 模块 1） */
export const LOGIN_NAV_ITEM: NavItemConfig = {
  id: 'login',
  path: '/login',
  label: '登录与会话',
  group: 'workspace',
  icon: LogIn,
  description: '安全登录、内存 Access Token 会话管理与 Organization 绑定',
  prdModuleNo: 1,
};

/** 后端健康状态辅助图标 */
export const HEALTH_ICON = Activity;
