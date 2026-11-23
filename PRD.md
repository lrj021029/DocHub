# 知识库管理平台 PRD

> 产品需求规格说明书（Product Requirements Document）  
> 文档版本：v1.0  
> 需求基线：六轮 Grill Me 访谈  
> 项目形态：独立产品、Monorepo、Windows 本机开发、Antigravity Vibe Coding  
> 状态说明：本文中的 CONFIRMED 为用户明确确认；PROPOSED 为工程建议，尚待实现前确认；TBD 为尚未确定，不得由实现 Agent 擅自补齐。

---

## 0. 决策状态与文档使用规则

### 0.1 状态定义

| 状态 | 含义 | 对开发的约束 |
|---|---|---|
| CONFIRMED | 用户已明确选择 | 实现必须遵守，变更需记录 ADR 并取得批准 |
| PROPOSED | 依据已确认要求提出的工程方案 | 可以作为设计基线，但不得伪装成用户已确认事实 |
| TBD | 尚未确定 | 保留开放；不得自行填写固定值或假装已经验收 |

### 0.2 文档优先级

1. 用户明确确认的决策及后续更正。
2. 本 PRD 中标注为 CONFIRMED 的产品要求。
3. `TECHNICAL_SPEC.md` 中经确认的技术契约。
4. PROPOSED 设计仅在没有冲突时作为默认实施建议。
5. TBD 必须通过配置、能力探测或后续决策处理，不得硬编码猜测。

### 0.3 前端技术栈更正

- 第一轮曾选择 Vue 3 + TypeScript + Tailwind + shadcn-vue。
- 第六轮补充明确要求使用 COSS UI。
- COSS UI 官方定位为 React 组件库，基于 Base UI 和 Tailwind CSS。
- **本规格采用最新选择：React + TypeScript + Tailwind CSS + COSS UI。此前 Vue 3 选择被本轮最新要求替代，不采用 Vue 与 React 混合主前端。**
- React 具体版本、构建工具、路由、状态管理仍属于依赖冻结事项；不得未经核验写入固定版本。

### 0.4 MinerU API 边界

- CONFIRMED：通过用户已有 Key 调用 MinerU 官方 API；不在本机部署 MinerU GPU 服务作为主解析路径。
- CONFIRMED：MinerU 作为复杂 PDF、扫描 PDF、表格、公式等文档的主要解析能力；其他格式采用轻量专用解析器并由外层按格式路由。
- TBD：当前账户实际对应的 API Host、API 版本、套餐限制、可用 tier、文件限制、配额和具体响应模型。
- 实现必须通过 MinerU Adapter 隔离 API 差异。API Key 只能保存在服务端 Secret 配置中。
- L2/L3 敏感文档默认不得发送到外部 MinerU 服务。若没有获准的本地解析/OCR 路径，应安全阻断并显示待处理状态，不得自动外发。

---

## 1. 产品概述

### 1.1 产品定位

构建一个面向组织内部知识治理的知识库管理平台，提供知识多源维护、文档解析与结构化切片、四维细粒度数据授权、AI 鉴权检索与问答、知识引用溯源、FAQ 自动挖掘和审核发布、知识缺口闭环，以及知识运营分析。

产品目标不是简单的“上传 PDF 后向量检索聊天”，而是形成可治理、可审计、可追溯、权限一致的企业知识服务系统。

### 1.2 核心价值

1. 将 PDF、Markdown、Word、TXT、Excel、CSV、JSON 等异构知识源转化为可检索的结构化知识单元。
2. 通过 Organization、Knowledge Space 和 Document ACL 分层治理知识访问范围。
3. 在检索、重排、模型上下文、FAQ、缓存、历史对话和运营入口中持续执行权限规则。
4. 通过混合检索、Reranker、结构化引用和知识约束提高回答质量。
5. 从历史问答中发现高频需求，经过人工审核沉淀 FAQ，并识别知识缺口。

### 1.3 产品成熟度目标

- CONFIRMED：实际生产级产品目标。
- 实现方式采用可在 Windows 本机 Docker Desktop 环境运行的参考部署。
- 生产级目标不等于本机单节点部署已经具备高可用、灾备或企业生产 SLA。
- 所有可靠性、安全性和性能承诺必须经过测试数据验证。

### 1.4 核心演示场景

**场景 A：跨部门财务与薪酬制度问答隔离**

- 财务报销标准配置为当前 Knowledge Space 的全局可读。
- 高管薪酬与股权激励细则仅授权给 HR 部门和管理层角色。
- 普通员工提问差旅制度时，得到带引用的正常回答。
- 普通员工提问薪酬细则时，系统不泄露文档存在性、标题、摘要、内容或引用；按照敏感级别返回受限提示。
- HR/管理层用户在满足 ACL 时可以正常获得答案和引用。

**场景 B：客服 FAQ 沉淀与知识缺口补足**

- 一周内多个用户反复询问生鲜食品破损退款规则。
- 系统清洗问题、去重、聚类、统计频次和独立用户数。
- 挖掘引擎依据有权访问的知识来源生成 FAQ 候选及引用。
- 知识管理员审核、修改、发布；FAQ 进入受 ACL 保护的高速匹配路径。
- “海外直邮保税仓清关延误”等未命中或证据不足的问题进入 Knowledge Gap。
- 管理员创建知识补充任务、导入材料、审核发布，并回归验证问题是否解决。

---

## 2. 项目范围

### 2.1 本期范围（CONFIRMED）

| 领域 | 功能范围 |
|---|---|
| 身份与组织 | JWT 登录、用户管理、多 Organization、部门树、多部门成员、组织角色、功能权限 |
| Knowledge Space | 多 Space、公开/私有入口、Space 级配置和成员访问控制 |
| 文档维护 | 单文件、批量文件/文件夹导入；PDF、Markdown、Word、TXT、Excel、CSV、JSON |
| 文档解析 | MinerU 官方 API 处理复杂 PDF/扫描件/表格/公式；轻量解析器处理简单格式 |
| 文档存储 | PostgreSQL 管业务元数据，MinIO 保存原始文档和解析制品 |
| 知识切片 | 结构感知 Chunking、Parent-Child 层级、表格/公式/图片结构保留 |
| 向量化 | 多 Provider、多语言 Embedding、本地与外部模式、索引版本迁移 |
| 检索 | Qdrant Dense + BM25/Sparse、RRF、Reranker、多 Space 检索 |
| 数据权限 | Global、Department、Role、User 四维 Allow OR；Document 级 ACL；Space 入口 ACL |
| AI 问答 | 登录态鉴权、Query Rewrite、Multi-Query、SSE、Markdown、引用溯源、多轮上下文 |
| FAQ | 实时/定时/手动挖掘、HDBSCAN、增量匹配、人工审核、发布、缓存 |
| 知识缺口 | 未命中/低相关性/授权证据不足识别、聚类、任务流转、闭环验证 |
| 运营分析 | PV/UV、文档与 Chunk 数、FAQ 命中、Token、耗时、引用热度、缺口解决率 |
| 审计 | 问答审计、ACL 安全审计、FAQ 审批审计、来源版本追踪 |
| 运维 | Docker Compose、Prometheus、Grafana、Loki、Tempo、OpenTelemetry、备份与恢复 |
| 测试 | L0–L3 测试、RAG Benchmark、ACL 对抗测试、MinerU 解析回归 |

### 2.2 本期不默认包含的能力（TBD 或范围外）

- 企业网盘连接器、SharePoint/Confluence 等外部知识同步：TBD，未纳入已确认首期格式范围。
- 网页 URL/HTML 爬取：TBD。
- 知识图谱、GraphRAG：未确认，不得作为首期必做。
- 多模态音视频知识库：未确认。
- 跨 Organization 检索：禁止作为默认行为；当前请求必须绑定一个 Organization。
- 自动发布 FAQ：不允许，FAQ 必须人工审核。
- 未经用户确认的任意第三方模型或解析服务：不允许自动启用。

---

## 3. 用户、组织与权限模型

### 3.1 用户角色

| 角色 | 主要职责 |
|---|---|
| 普通用户 / 提问者 | 登录、选择有权访问的 Space、问答、查看当前可访问的引用、提交反馈 |
| 知识管理员 | 导入与管理文档、切片复核、配置 Document ACL、审核 FAQ、处理知识缺口 |
| 系统管理员 | 用户与组织管理、部门树、角色与功能权限、模型参数、全平台运营监控 |
| 审计人员 | 在授权范围内查看业务审计、安全审计、问答来源及权限决策记录 |

具体 SystemRole 与 OrganizationRole 分离。系统管理权限不自动授予知识读取权限。

### 3.2 组织结构

```text
Platform
  └── Organization
       ├── OrganizationMember
       ├── Departments (tree)
       ├── DepartmentMembership (multi-membership)
       ├── OrganizationRoles
       └── KnowledgeSpaces
            ├── SpaceMembership / Space Access Policy
            └── Documents
                 └── Document ACL
```

- 用户可以加入多个 Organization。
- 每个请求必须绑定一个当前 Organization，不默认跨组织检索。
- 一个 Organization 可以有多个 Knowledge Space。
- Knowledge Space 支持公开/私有入口；Space 入口授权和 Document ACL 是两层独立校验。
- Global 的作用域是当前 Space 内满足入口条件的用户，不是平台级全局公开。
- Space 公开与私有的精确成员判定细节如未在实施前明确，记录为 TBD/PROPOSED，不得推导为跨组织公开。

### 3.3 系统角色与组织角色

- SystemRole：管理平台或组织基础设施能力。
- OrganizationRole：当前 Organization 内的业务角色，例如 Employee、HR、FinanceManager、KnowledgeManager。
- 功能权限使用独立 Permission 树，例如 `document:create`、`document:delete`、`faq:publish`。
- Document ACL 的 Role 主体只能引用当前 Organization 的 OrganizationRole。
- SystemAdmin 不自动获得全部 Document 读取权。
- Break-glass 紧急访问必须是独立、限时、填写理由并产生日志的机制。

### 3.4 四维 Document ACL

默认无任何读取授权。授权判定采用 Allow OR：

```text
ALLOW(user, document) =
    GLOBAL_MATCH
 OR DEPARTMENT_MATCH
 OR ROLE_MATCH
 OR USER_MATCH
```

其中：

- GLOBAL：当前 Knowledge Space 的全局可读范围。
- DEPARTMENT：当前有效部门成员关系命中；部门授权可配置是否包含后代部门，并支持动态或快照继承。
- ROLE：用户当前有效 OrganizationRole 命中。
- USER：用户个人授权命中，支持永久和限时授权。

本期不采用 Deny Override。若用户没有任一 Allow 命中，则拒绝。

### 3.5 部门成员关系

- 一个用户可以同时属于多个部门。
- 可指定主要部门；所有当前有效成员关系参与授权并集。
- 成员关系包含类型、状态、生效时间和到期时间。
- 部门授权可选择动态继承或快照继承。

### 3.6 文档导入初始权限

- 新文档进入 Quarantine/待授权状态，不能被普通问答检索。
- Knowledge Space 可以配置默认 ACL 模板，但默认模板不替代管理员对高敏感文档的确认。
- 文档未完成 ACL 配置、审核或发布时，不得进入 Active Retrieval Index。

### 3.7 敏感等级与提示规则

默认四级：

| 等级 | 含义 | 默认外部模型/解析服务处理 |
|---|---|---|
| L0 Public | 公开 | 允许 |
| L1 Internal | 内部 | Organization 授权后允许 |
| L2 Confidential | 机密 | 默认本地处理，禁止默认外发 |
| L3 Restricted | 受限 | 禁止外部发送 |

Organization 可进一步收紧规则，不得突破系统最高安全限制。

- L0–L3 的精确数据分类标准、字段例子和提示文案：TBD，由组织策略配置。
- 对受限知识的普通用户提示必须避免暴露文档存在性、标题、摘要、路径、Chunk 内容和引用。
- ACL 拒绝不是知识缺口，不参与普通 Knowledge Gap 聚合。

---

## 4. 文档维护与知识处理

### 4.1 支持格式

首期明确支持：PDF、Markdown、Word（DOCX）、TXT、Excel、CSV、JSON。

URL/网页、企业网盘等来源仍为 TBD。

### 4.2 文件导入

- 单文件上传、批量文件上传、文件夹拖拽导入。
- 根据文件大小选择 API 代理上传或 MinIO 预签名/Multipart 上传。
- 上传完成后进行文件 Hash、大小、实际格式、MIME、扩展名、病毒/恶意文件策略检查。
- 对象 Key 由后端生成，客户端不能指定任意存储路径。
- 上传、解析、切片、向量化、索引是分阶段任务，前端显示阶段进度、失败原因和可重试状态。
- 上传阈值、最大文件大小、最大页数、并发和保留时间：TBD。

### 4.3 文档解析

- MinerU 官方 API 是复杂 PDF、扫描 PDF、表格、公式的主解析能力。
- 简单格式由专用轻量解析器处理，并通过 Parser Adapter 统一输出。
- MinerU 官方 API 的实际 Host、账户 API 版本、认证和限额必须通过当前 Key 对应的官方文档/能力测试确认。
- MinerU Key 仅存后端 Secret 配置，不进入前端、代码、日志或 Git。
- MinerU 解析为异步任务，需显示任务 ID、状态、逐文件结果、错误、重试和取消能力。
- 原始文件、结构化 JSON、Markdown、页面渲染资源建立版本关联。
- 扫描 PDF OCR 能力按敏感等级路由；不允许为完成解析而绕过外发策略。

### 4.4 解析结果

权威解析产物包含：

- 结构化 JSON：Parsed Blocks、类型、文本、表格/公式结构、页码、Bounding Box。
- Markdown：供检索、预览和内容处理使用。
- PDF 页面渲染图及关联图片资源：供引用定位、人工校验。

JSON/Block 结构是结构化内容事实来源；Markdown 是派生表示；原始文件保持不可变。

### 4.5 Chunking

- 使用 Structure-aware Chunking + Parent-Child。
- 根据章节、段落、列表、表格、公式和图片结构切分。
- Child Chunk 用于检索；Parent Chunk 用于补充上下文。
- Chunk 参数按文档类型自动选择，并允许 Knowledge Space 覆盖默认值。
- 表格保留表头，跨页表格可尝试合并；公式保留 LaTeX/结构表示；图片保留 OCR/图注/页码/位置。
- Chunk 大小、Parent 大小、Overlap、Token 计数器和切分规则版本：TBD。
- 每个 Chunk 继承所属 Document 的 ACL，不允许 Chunk 自行放宽权限。

### 4.6 文档与索引版本

- DocumentVersion 不可变。
- 每次文档内容变化生成新版本。
- ParsedArtifact、ParsedBlock、Chunk Set、Embedding Index Generation 与 DocumentVersion 关联。
- 支持索引构建、验证、激活、退休和回滚。
- 内容 Hash、Parser Version、Chunker Version、Embedding Profile 和 Index Generation 必须可追溯。

---

## 5. AI 鉴权检索与问答

### 5.1 检索链路

```text
User Question
  -> Authenticate current user and Organization
  -> Resolve active departments and OrganizationRoles
  -> Validate selected Knowledge Spaces
  -> FAQ pre-check (optional fast path)
  -> Query rewrite / Multi-query
  -> Per-Space Query Embedding
  -> Qdrant Dense + BM25/Sparse Retrieval
  -> Per-Space RRF
  -> Candidate Chunk IDs
  -> PostgreSQL authoritative ACL re-check
  -> Load authorized chunk content only
  -> Per-Space reranking
  -> Cross-Space rank fusion (RRF)
  -> Context builder
  -> LLM generation
  -> Citation validation and SSE stream
  -> Async audit / analytics event
```

- 用户可手动选择多个 Knowledge Space；跨空间只发生在当前 Organization 内，除非未来另有明确授权设计。
- 每个 Space 可以使用不同 Embedding Profile。Query 必须按各 Space 对应模型分别向量化。
- 不直接比较不同 Embedding 模型的原始相似度分数；跨 Space 使用基于排名的 RRF。
- Qdrant 预过滤提高效率；PostgreSQL 最终授权是权威判定。
- 未授权 Chunk 不得进入正文读取、Reranker、LLM Prompt、引用或普通用户可见日志。

### 5.2 RAG 策略

- Hybrid Retrieval：Dense Vector + Qdrant BM25/Sparse + RRF。
- Reranker：支持本地和外部 Provider；敏感内容受外发策略约束。
- Query Rewrite：利用多轮上下文将省略表达补全为独立检索问题。
- Multi-Query：可作为可配置增强，具体生成数量 TBD。
- FAQ 预检：FAQ 候选召回、ACL 校验、置信度判断；高置信度直接返回，其他情况继续完整 RAG。
- 每轮问答重新计算当前权限，不直接沿用前一轮授权结果。

### 5.3 回答策略

Knowledge Space 可配置回答策略：

- 严格依赖授权知识；证据不足时拒答/提示缺口。
- 谨慎允许通用模型知识补充，但必须明确区分来源。
- 默认策略与每 Space 覆盖方式需要配置并审计。
- 文档内容是非可信数据；文档中的指令不能覆盖 System Prompt、权限策略或工具安全边界。
- 模型不得自行推断用户可访问的知识范围。

### 5.4 SSE 与前端展示

- 使用 SSE 流式传输。
- 事件至少区分：`message_start`、`retrieval_started`、`retrieval_completed`、`citation`、`token`、`message_complete`、`message_error`。
- 支持流式 Markdown、代码高亮、引用卡片、页面定位和权限缺失提示。
- 引用卡片打开时重新执行当前权限检查。
- 历史会话重新打开时按当前权限和敏感等级重新渲染；失去权限的历史正文必须隐藏或脱敏。
- Access Token 在前端内存，Refresh Token 使用 HttpOnly Cookie；SSE 认证实现方式需与该设计兼容，不得把长期 JWT 放入 URL。

### 5.5 问答审计

每轮问答至少记录：

- Conversation ID、Message ID、Organization、Space、User、时间。
- 原始问题与脱敏分析副本之间的关联。
- RetrievalAudit：候选 Chunk ID、授权 Chunk ID、拒绝数量/分类、检索参数版本。
- 实际进入模型的 Chunk/Source IDs。
- Citation、模型 Provider/Model、Token、延时、错误和拒答原因。
- ACL Revision、Space Policy Version、Embedding/Index Generation。

安全审计详情不暴露给普通用户。

---

## 6. FAQ 自动沉淀

### 6.1 触发方式

支持实时增量匹配、定时批量重聚类、手动触发。具体调度周期 TBD。

### 6.2 挖掘流程

```text
Question Events
  -> PII / Sensitive Data Redaction
  -> Question Normalization
  -> Anti-spam / Deduplication
  -> Embedding
  -> Incremental assignment to existing clusters
  -> Periodic HDBSCAN rebuild
  -> Cluster version and metrics
  -> Retrieve authorized sources
  -> Generate FAQ candidate answer
  -> Citation validation
  -> Candidate confidence and risk assessment
  -> Human review
  -> Publish immutable FAQVersion
  -> Cache warm / invalidation events
```

### 6.3 聚类与权限

- 批量聚类采用 HDBSCAN；增量问题可以对现有簇执行相似度匹配/近似分配。
- 增量分配不等同于完整重聚类；新簇发现、拆分和合并需要定期重聚类。
- 不同权限域的问题不得通过 FAQ 候选内容相互泄露。
- 允许在严格脱敏和最小群体保护下进行匿名跨权限统计。
- 最小群体阈值、差分隐私、聚类相似度阈值和时间窗口：TBD。

### 6.4 频次与反刷量

统计总提问量、独立用户数、独立会话数、部门覆盖率、重复率。支持短时间重复贡献限制、异常频率检测和垃圾输入过滤。阈值支持全局默认、Organization、Knowledge Space 多级配置。

### 6.5 FAQ 答案与来源

- FAQ 答案为结构化 Segment，支持文本、表格、链接、图片等内容类型。
- 每段保存来源文档版本、Chunk、Citation 和 ACL 关系。
- FAQ 默认继承来源最严格权限；管理员可缩小权限。
- 若要扩大 FAQ 可见范围，必须保证公开段落不依赖受限来源，并经过相应审核。
- 手工 FAQ 必须标记 MANUAL，填写事实依据、有效期限和维护人。

### 6.6 FAQ 生命周期

生命周期、发布状态、来源健康状态分离：

- Lifecycle：CANDIDATE、PENDING_REVIEW、REJECTED、ARCHIVED 等。
- Publication：PUBLISHED、DISABLED。
- Source Health：HEALTHY、STALE、EXPIRED。

候选 FAQ 必须人工审核。审核人不能与创建人相同；敏感级别决定额外审批槽位。具体审批人员组合和等级映射：TBD。

### 6.7 FAQ 匹配和缓存

- 精确匹配 + 语义匹配 + BM25 + 可选 Query Rewrite。
- 直接返回阈值按模型和 FAQ 类型配置，Space 提供默认值；具体数值通过 Benchmark 确定。
- Redis 缓存结构化答案、Segment 和引用元数据；敏感正文缓存策略按等级控制。
- 每次命中必须重新授权；Cache Key 包含 Organization、Space、FAQ Version、ACL Revision、来源版本等关键维度。
- 缓存事件失效 + TTL + 预热 + 周期性对账。
- Redis 或权威授权服务不可用时不得返回无法确认权限的缓存正文。

---

## 7. Knowledge Gap 知识缺口

### 7.1 进入缺口池的条件

可进入：无召回、低相关性、有权证据不足、经评估的模型无法回答、用户反馈答案错误。

不得进入普通知识缺口：

- 仅召回未授权文档。
- ACL 拒绝。
- 越权访问尝试。
- LLM Provider 故障。
- MinerU 或基础设施故障。

这些事件进入安全审计或运维故障体系。

### 7.2 聚合维度

支持 Organization、Knowledge Space、Department 多维聚合；原始问题脱敏，低样本敏感问题隐藏或合并。具体最小人数阈值 TBD。

### 7.3 任务生命周期

至少支持：DETECTED、TRIAGED、ASSIGNED、IN_PROGRESS、CONTENT_SUBMITTED、REVIEWING、RESOLVED、REJECTED、DUPLICATED、CLOSED、REOPENED。

KnowledgeGap 与 KnowledgeGapTask 分离，任务包含负责人、优先级、截止时间和关联 FAQ/Document。

### 7.4 闭环验收

补充文档发布后，重新执行对应 Gap 的 RAG 回归问题；只有达到配置的检索/回答质量标准后，才允许关闭为 RESOLVED。阈值 TBD。

---

## 8. 页面与信息架构

### 8.1 页面模块

1. 登录与当前 Organization 切换。
2. 总览工作台。
3. Organization / Department / User / Role 管理。
4. Knowledge Space 管理与成员入口授权。
5. 知识维护中心：文档台账、上传、版本、解析进度、Chunk 预览。
6. 文档权限配置：Global、Department、Role、User 四维授权。
7. AI 问答工作台：Space 多选、历史会话、SSE、引用和权限提示。
8. FAQ 挖掘与审核中心。
9. 已发布 FAQ 管理与缓存状态。
10. Knowledge Gap 清单与任务管理。
11. 数据运营看板。
12. 模型、MinerU API、Embedding、Reranker、阈值等系统配置。
13. 业务审计与安全审计。
14. RAG / Parser Benchmark。

### 8.2 前端体验要求

- React + TypeScript + Tailwind CSS + COSS UI。
- 组件应满足键盘操作、可访问性、明确 Loading/Empty/Error 状态。
- 复杂管理表格应支持筛选、排序、分页、批量操作和权限控制。
- 文档解析进度分阶段展示，不使用虚构进度。
- 引用卡片支持文档、页码、章节、引用片段、原 PDF 页面高亮。
- 运营看板按当前 Organization 和授权范围过滤。

---

## 9. 运营指标

至少覆盖：

- PV、UV、活跃会话数。
- 文档、DocumentVersion、有效 Chunk 数量。
- 导入成功率、解析失败率、OCR 失败率、索引构建耗时。
- 高频问题、FAQ Candidate/PUBLISHED 数量。
- FAQ 命中率、直接回答率、用户有用率、STALE 比例。
- Knowledge Gap 数量、解决率、平均处理时间。
- Dense/BM25 召回耗时、Reranker 耗时、首 Token 延迟、完整响应延迟。
- Token 消耗、模型调用成本（如 Provider 可返回成本信息）。
- 权限拒绝事件仅在安全授权的看板中展示具体详情；普通运营看板不得泄露受限资源。

知识覆盖率的计算公式需要基于可解释的评估集和组织范围定义，不能仅用“FAQ 数量/提问数”伪造覆盖率。

---

## 10. 非功能需求与安全红线

### 10.1 安全红线

以下任何一项发生，相关版本不得发布：

1. 未授权 Chunk 进入 Reranker。
2. 未授权 Chunk 进入 LLM Prompt。
3. FAQ 缓存绕过 ACL。
4. 跨 Organization 数据读取。
5. 已撤权用户在重新打开历史对话时读取受限回答。
6. 多来源 FAQ 错误扩大来源权限。
7. 文档标题、引用、搜索建议、热门排行等旁路暴露受限知识。
8. MinerU / LLM / Embedding / Reranker 外部请求违反敏感级别策略。
9. 前端、Git、日志泄露 API Key、JWT 或其他 Secret。

### 10.2 安全与隐私

- 组织隔离：应用层过滤 + PostgreSQL RLS。
- Space 入口检查 + Document ACL 最终检查。
- 四维 Allow OR，默认拒绝。
- ACL Revision、审计 Diff、Break-glass 审计。
- 文档提示注入作为不可信内容处理。
- 原始问答日志、脱敏聚类数据、FAQ 候选分开保存。
- 数据保留期限、RPO/RTO、密钥轮换：TBD。

### 10.3 性能与容量

- 目标并发：20 个并发用户（CONFIRMED，具体请求模型仍需定义）。
- 响应延迟目标：通过 Benchmark 确定（CONFIRMED）。
- 文档导入并发：按资源自适应（CONFIRMED），数值 TBD。
- 初始文档规模、Chunk 数、平均文件大小、单文件上限：TBD。
- Docker 内存/CPU/GPU/磁盘预算：TBD。

### 10.4 可观测性

CONFIRMED：Prometheus + Grafana + Loki + Tempo + OpenTelemetry。

### 10.5 备份与恢复

CONFIRMED：定时备份、校验、恢复演练。

TBD：备份周期、保留期限、RPO、RTO、异地备份、加密密钥恢复流程。

---

## 11. 验收标准

### 11.1 身份和组织

- [ ] 用户登录、刷新、登出与会话撤销正常。
- [ ] 用户可以加入多个 Organization，并切换当前 Organization。
- [ ] 部门树、多部门成员、主要部门和有效期正确。
- [ ] SystemRole 与 OrganizationRole 分离。
- [ ] 功能菜单和操作权限在后端强制校验，而不是仅隐藏前端按钮。
- [ ] PostgreSQL RLS 的跨组织越权测试全部通过。

### 11.2 Space 与 ACL

- [ ] 公开/私有 Space 入口规则正常。
- [ ] 新文档默认 Quarantine，未授权不能检索。
- [ ] Global、Department、Role、User 任一 Allow 命中即可访问。
- [ ] 部门动态/快照继承符合配置。
- [ ] 临时个人授权到期后即时拒绝。
- [ ] ACL Revision、变更人、时间和 Diff 可追溯。
- [ ] Qdrant Payload 更新延迟期间 PostgreSQL 最终授权仍阻断已撤销权限。
- [ ] Break-glass 有理由、时限和审计记录。

### 11.3 文档处理

- [ ] 支持 PDF、Markdown、DOCX、TXT、Excel、CSV、JSON 导入。
- [ ] 支持单文件、批量和文件夹拖拽。
- [ ] MinerU 官方 API 真实请求、轮询和结果下载成功。
- [ ] API Key 不出现在前端、Git 或日志。
- [ ] L2/L3 文件不会被意外发送至外部解析 API。
- [ ] 结构化 Block、Markdown、页面渲染制品可关联。
- [ ] Chunk Parent-Child 与原始页码/坐标可追溯。
- [ ] 解析/切片/Embedding/Index 分阶段失败可恢复。

### 11.4 RAG 与问答

- [ ] Dense + BM25/Sparse + RRF 检索可运行。
- [ ] 多 Space 按各自 Embedding Profile 查询并按排名融合。
- [ ] PostgreSQL 最终 ACL 复核在 Reranker/LLM 前执行。
- [ ] 未授权内容绝不进入 Reranker、LLM 或普通日志。
- [ ] SSE 事件可处理开始、检索、引用、Token、完成和错误。
- [ ] 多轮 Query Rewrite 运行时重新鉴权。
- [ ] 引用可定位原文档、页码和页面高亮。
- [ ] 历史对话按当前权限重新渲染。

### 11.5 FAQ 与知识缺口

- [ ] 实时、定时、手动 FAQ 挖掘触发正常。
- [ ] HDBSCAN 批量重聚类与增量分配分开实现。
- [ ] 频次、独立用户、去重和异常刷量指标正确。
- [ ] FAQ Candidate 由授权来源生成并附带引用。
- [ ] FAQ 必须人工审核，创建者不能自审。
- [ ] FAQ 多来源段落 ACL 不扩大来源权限。
- [ ] FAQ 命中每次重新鉴权；缓存失效可靠。
- [ ] ACL 拒绝不进入 Knowledge Gap。
- [ ] Knowledge Gap 能生成任务、关联文档并通过回归验证关闭。

### 11.6 运营、可观测性与恢复

- [ ] PV/UV、Token、延迟、文档热度和 FAQ 指标可核验。
- [ ] 受限文档不出现在普通看板。
- [ ] Prometheus/Grafana/Loki/Tempo/OTel 观测链路可用。
- [ ] PostgreSQL、MinIO、Qdrant 备份可校验并恢复。
- [ ] L0–L3 测试可自动执行。
- [ ] MinerU 版本升级触发解析回归测试。

---

## 12. TBD 登记表

以下项目未获得明确参数或选择，必须保持 TBD：

| ID | TBD 项 | 影响 |
|---|---|---|
| T-01 | PostgreSQL 主版本及扩展版本 | UUID v7、RLS、迁移兼容性 |
| T-02 | React 版本、构建工具、Router、状态管理、表单方案 | 前端工程初始化 |
| T-03 | MinerU API Host、API 版本、套餐、Tier、限额、响应结构 | 文档解析集成 |
| T-04 | LLM/Embedding/Reranker Provider、Model、Base URL、尺寸和配额 | RAG 能力与成本 |
| T-05 | Docker 内存、CPU、GPU、磁盘预算 | 本机部署容量 |
| T-06 | 文档规模、最大文件、平均页数、总 Chunk 数 | 容量与性能 |
| T-07 | L2/L3 扫描 PDF 本地 OCR 替代方案 | 高敏感文档解析 |
| T-08 | Chunk Token 大小、Parent 大小、Overlap | 检索质量 |
| T-09 | Dense/BM25 Top-K、RRF 参数、Reranker Top-K | 检索质量 |
| T-10 | FAQ 频次阈值、时间窗口、独立用户阈值 | FAQ 候选生成 |
| T-11 | HDBSCAN 参数、增量匹配阈值、重聚类周期 | 聚类质量 |
| T-12 | FAQ 直接命中阈值与置信度校准 | FAQ 快速回答 |
| T-13 | 低样本统计隐藏门槛/差分隐私方案 | 跨权限聚类统计 |
| T-14 | 组织/部门/Space 默认成员策略的完整细则 | 权限判定 |
| T-15 | Break-glass 审批槽位、超时和理由要求 | 紧急访问 |
| T-16 | 历史回答按 L0-L3 处理矩阵 | 历史会话 |
| T-17 | Redis 缓存 TTL、预热频率、对账周期 | FAQ 缓存 |
| T-18 | 数据保留期限、RPO、RTO、备份周期 | 数据恢复 |
| T-19 | 性能 SLO/P95/P99 | 性能验收 |
| T-20 | MinerU/模型 API 成本预算 | 运行成本 |
| T-21 | LLM 外部数据处理条款与各 Organization 授权记录 | 数据外发合规 |
| T-22 | 具体部署版本和镜像 Digest | 可重复部署 |

---

## 13. 依赖与外部资料

- MinerU 官方开发 API：<https://mineru.io/developer>
- MinerU OpenDataLab API 文档：<https://opendatalab.github.io/MinerU/next/api/>
- MinerU Tasks API：<https://opendatalab.github.io/MinerU/zh/usage/tasks/>
- Qdrant Hybrid Queries：<https://qdrant.tech/documentation/search/hybrid-queries/>
- Qdrant Multitenancy：<https://qdrant.tech/documentation/guides/multiple-partitions/>
- PostgreSQL Row Security：<https://www.postgresql.org/docs/current/ddl-rowsecurity.html>
- COSS UI：<https://coss.com/ui/docs>
- SQLAlchemy Async：<https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html>
- Celery：<https://docs.celeryq.dev/>
