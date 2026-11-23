import { ArrowRight, ShieldCheck, FileText, MessageSquareText, Sparkles } from 'lucide-react';
import { Link } from 'react-router-dom';
import { ModuleWorkspace } from '@/components/common/ModuleWorkspace';
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

/**
 * PRD 模块 2：总览工作台。
 * 呈现当前 Organization 下的治理入口、安全红线摘要与关键模块快捷导航。
 */
export function DashboardPage() {
  const quickLinks = [
    {
      title: 'AI 问答工作台',
      desc: '选择授权 Knowledge Space 发起混合检索与 SSE 流式问答',
      to: '/chat',
      icon: MessageSquareText,
    },
    {
      title: '文档维护中心',
      desc: '单文件/批量上传、MinerU 与轻量解析器分阶段任务跟踪',
      to: '/documents',
      icon: FileText,
    },
    {
      title: '文档四维 ACL 配置',
      desc: '配置 Global / Department / Role / User Allow OR 授权与版本 Diff',
      to: '/acl',
      icon: ShieldCheck,
    },
    {
      title: 'FAQ 挖掘与审核',
      desc: '查看 HDBSCAN 高频聚类候选并执行双人分离人工审核',
      to: '/faq-mining',
      icon: Sparkles,
    },
  ];

  return (
    <ModuleWorkspace
      moduleCode="PRD-02"
      title="总览工作台"
      description="面向当前 Organization 的企业知识治理驾驶舱。仅展示经后端权威 ACL 过滤后的可见资产与健康指标。"
      apiContract="GET /api/v1/readyz · GET /api/v1/analytics/overview"
      governanceBadges={['组织强隔离 (RLS)', 'COSS UI 基线']}
      metrics={[
        { label: '授权知识空间', value: '--', hint: '待 Slice 1/2 组织与 Space 接口接入' },
        { label: '已激活文档版本', value: '--', hint: '仅统计通过 Quarantine 与 ACL 校验的文档' },
        { label: '待审核 FAQ 候选', value: '--', hint: '强制人工审核，创建人不可自审' },
        { label: '开放 Knowledge Gap', value: '--', hint: '不含 ACL 拒绝与越权事件' },
      ]}
      headerActions={
        <Link to="/chat">
          <Button size="sm">
            进入 AI 问答
            <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
          </Button>
        </Link>
      }
    >
      <div className="space-y-4">
        <Alert variant="info" title="Phase 0 前端工程基线已就绪">
          当前界面严格遵循 React + TypeScript + Tailwind CSS + COSS UI 技术栈；Access Token
          仅驻留内存，SSE 使用 Fetch POST 流式认证，所有业务数据将在后续 Vertical Slice 真实对接。
        </Alert>

        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          {quickLinks.map((item) => {
            const IconComp = item.icon;
            return (
              <Card key={item.to} className="transition-colors hover:border-line-strong">
                <CardHeader className="flex-row items-center justify-between space-y-0">
                  <div className="flex items-center gap-2.5">
                    <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-surface-muted text-ink">
                      <IconComp className="h-4 w-4" aria-hidden="true" />
                    </div>
                    <CardTitle>{item.title}</CardTitle>
                  </div>
                  <Badge variant="outline">模块入口</Badge>
                </CardHeader>
                <CardContent className="flex items-center justify-between gap-3">
                  <CardDescription className="text-xs">{item.desc}</CardDescription>
                  <Link to={item.to}>
                    <Button variant="outline" size="sm">
                      进入
                    </Button>
                  </Link>
                </CardContent>
              </Card>
            );
          })}
        </div>
      </div>
    </ModuleWorkspace>
  );
}
