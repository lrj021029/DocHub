# ADR-003：前端技术栈与解析/模型 Provider 双选架构

- 状态：已批准（Approved）
- 日期：2026-11-23

## 1. 前端技术栈冻结

- 唯一主前端：**React + TypeScript + Tailwind CSS + COSS UI**（彻底替代早期 Vue 3 方案）。
- 构建与生态：Vite + React Router + TanStack Query + React Hook Form + Zod + Vitest。
- COSS UI 组件按官方源码方式纳入 `frontend/src/components/ui/` 并保留来源与 MIT 许可证说明。
- 安全约束：Access Token 仅存于内存状态，Refresh Token 使用 `HttpOnly` Cookie 并配套 CSRF Token；SSE 使用 `fetch()` 流式 POST 携带 `Authorization: Bearer`，严禁将 JWT 放入 URL。

## 2. 文档解析与敏感外发策略

- **主解析引擎**：优先通过 `MinerUOfficialAdapter` 调用 MinerU 官方 API 处理复杂 PDF、扫描 PDF、表格、公式。
- **轻量解析器**：Markdown、DOCX、TXT、XLSX/XLS、CSV、JSON 由本地专用解析器经统一 `DocumentParser` 协议输出标准化 `ParsedBlock` 与 Markdown。
- **L2/L3 敏感文档**：预留 `LocalOCRParserAdapter` 接口；当 `LOCAL_OCR_ENABLED=false`（默认）且文档为 L2/L3 扫描 PDF 时，强制 Fail-Closed 进入 `BLOCKED_POLICY`，绝不降级外发 MinerU。

## 3. 模型 Provider 双选架构

- **LLM（对话/改写/FAQ 生成）**：统一 `OpenAICompatibleLLMProvider`，默认指向 `https://100milionseek.top/v1`（配置化 Base URL / Model / Key）。
- **Embedding & Reranker**：支持 **Qwen 官方模型 API（默认优先）** 与 **本地 BGE 模型接口（`LOCAL_BGE_*`）** 双 Provider 路由：
  - L0/L1（经组织授权）优先使用 Qwen 官方 API；
  - L2/L3 敏感文档若已开启本地 BGE 服务则路由至本地 BGE，否则触发 `EgressPolicyEngine` 安全拦截。
- 所有模型调用前均强制执行 `EgressPolicyEngine.check()` 并记录审计元数据。
