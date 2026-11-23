# ADR-001：工程治理、多 Agent 协作与规格冲突裁决

- 状态：已批准（Approved）
- 日期：2026-11-23
- 依据：`PRD.md`、`TECHNICAL_SPEC.md` 及用户批准指令

## 1. 文档冲突裁决

1. **并行 Agent 数量**：`TECHNICAL_SPEC.md` 存在 4 / 无限制 / 8 三处不一致；用户最终批准由主控 Agent 根据领域与阶段依赖动态决定并行 Agent 数量，通过 `swarm` 协作并使用独立 Git Worktree 隔离开发。
2. **FAQ 发布状态枚举**：采纳 `TECHNICAL_SPEC.md` §10.5 的三态模型（`UNPUBLISHED`、`PUBLISHED`、`DISABLED`），与 `Lifecycle`、`Source Health` 三轴完全解耦。
3. **Document ACL 写入口**：收敛为 `POST /api/v1/documents/{document_id}/acl/revisions`（携带 `expected_revision`、`reason`、`Idempotency-Key`），同时保留 `GET /api/v1/documents/{document_id}/acl` 与 `GET /api/v1/documents/{document_id}/acl/revisions` 读取接口，避免双写入口导致审计不一致。
4. **Qdrant Payload 字段统一**：统一使用 `space_id`，Dense 与 Sparse 向量存放于 Qdrant Named Vectors / Sparse Vectors，Payload 仅存放受控过滤字段（含轻量 ACL 投影用于防召回饥饿），绝不直接将 Payload 文本传入 Reranker 或 LLM。

## 2. 工程红线

- 严禁修改 `PRD.md` 和 `TECHNICAL_SPEC.md`。
- 全项目代码（Python / TypeScript / SQL / 脚本）统一采用**中文注释**。
- 严禁以 Mock 冒充真实业务执行结果；每个 Vertical Slice 完成后必须执行真实测试验证。
- 严禁将任何 API Key、JWT、密码写入代码、前端、日志或 Git 历史。
