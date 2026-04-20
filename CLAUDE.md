# FireGuard (DetPlatform) Project Guidelines

## 1. 核心开发理念 (Core Principles - Karpathy Style)
- **编码前思考**: 明确假设，避免盲目猜测。遇到歧义先沟通。
- **简洁优先**: 只写解决问题所需的最少代码。拒绝过度抽象和推测性开发。
- **手术刀式修改**: 只触碰必须修改的代码，匹配现有风格。
- **目标驱动执行**: 先定义成功标准，通过循环验证确保目标达成。

## 2. 全栈技术栈与环境 (Tech Stack & Environment)
- **后端 (Backend)**: FastAPI + SQLModel + Python 3.10+
  - 核心库: `ultralytics` (YOLO), `opencv-python`, `sqlmodel`, `websockets`
- **前端 (Frontend)**: Vue 3 + TypeScript + Vite
  - 核心库: `ant-design-vue`, `pinia`, `vue-router`
- **开发工具**: 优先使用 `pnpm` (前端) 和 `pip` (后端)

## 3. 开发流程 (Development Workflow)
- **调研优先 (Search-First)**: 在引入新功能或依赖前，先搜索现有代码库或成熟库，避免重复造轮子。
- **规划阶段**: 复杂任务需先列出分步计划并获取确认。
- **API 通讯**: 遵循 RESTful 规范。前端调用需严格匹配后端 Pydantic 模型定义的 Schema。

## 4. 全栈验证循环 (Verification Loop)
在提交或完成功能前，必须按顺序执行以下验证：

### 第一步：后端验证 (Backend)
```bash
# 静态检查 (需安装 pyright/ruff)
pyright backend/app
ruff check backend/app
# 运行测试
pytest backend/tests
```

### 第二步：前端验证 (Frontend)
```bash
# 类型与 Lint 检查
cd frontend && npx vue-tsc --noEmit && npm run lint
# 运行测试 (如有)
cd frontend && npm run test
```

### 第三步：安全检查 (Security)
- 严禁在代码中硬编码任何 API Key、Secret 或密码。
- 严禁在生产代码中残留敏感信息的 `print()` 或 `console.log()`。

## 5. 编码规范参考 (Detailed Rules)
详细规则请查阅 `.claude/rules/` 目录：
- [通用测试规范](.claude/rules/common/testing.md)
- [安全审查清单](.claude/rules/common/security.md)
- [Python 编码规范](.claude/rules/python/coding-style.md)
- [Vue/TypeScript 开发规范](.claude/rules/typescript/coding-style.md)
