# COSS UI 组件来源与许可证说明

## 1. 组件来源

本项目 `frontend/src/components/ui/` 目录下的基础设计系统组件（包括 `Button`、`Card`、`Input`、`Badge`、`Dialog`、`Select`、`Tabs`、`Table`、`Alert`、`Skeleton`）遵循 **COSS UI**（[https://coss.com/ui](https://coss.com/ui) / [https://coss.com/ui/docs](https://coss.com/ui/docs)）的设计规范与源码纳入方式（copy-and-paste source ownership），底层无障碍与键盘交互原语基于 **Base UI**（`@base-ui/react`），样式基于 **Tailwind CSS**。

依据 `PRD.md` 第 0.3 / 8.2 节与 `docs/adr/ADR-003-frontend-and-providers.md`：

- 唯一主前端技术栈冻结为 **React + TypeScript + Tailwind CSS + COSS UI**。
- 组件源码纳入仓库自有目录，便于针对企业级知识治理场景（如四维 ACL、敏感等级提示、引用溯源与键盘无障碍操作）进行长期演进与安全审计。
- 严禁在本仓库中引入 Vue 或 shadcn-vue。

## 2. 许可证声明（MIT License）

COSS UI 组件库及 Base UI 原语均采用 **MIT License** 开源许可。以下保留原 MIT 许可文本：

```text
MIT License

Copyright (c) coss.com / Base UI Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
