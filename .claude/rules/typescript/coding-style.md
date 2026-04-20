# TypeScript 编码规范 (TypeScript Coding Standards)

## 全局规范
- **严格类型**: 严禁滥用 `any`。所有 API 响应数据必须定义对应的 `interface` 或 `type`。
- **不可变性**: 优先使用 `const`。除非必要，避免使用 `let`。
- **文件拆分**: 单个组件文件原则上不超过 300 行。逻辑复杂的组件应将业务逻辑抽离为 `composables` (Hooks)。

## Vue 3 & TS 最佳实践
- **Setup 语法糖**: 统一使用 `<script setup lang="ts">`。
- **响应式**: 统一使用 `ref` 和 `reactive`。
- **Props & Emits**: 必须使用 `defineProps` 和 `defineEmits` 的泛型定义方式。
- **样式**: 优先使用项目定义的 CSS 变量和原子类。

## Pinia 状态管理
- 所有全局状态必须放在 `src/stores` 目录下。
- Store 命名统一使用 `useXxxStore` 格式。

## 代码审查要点
- 是否存在未使用的 Import？
- 所有的异步调用是否都有 `try-catch` 或错误状态处理？
- 是否对 `null` 和 `undefined` 进行了安全检查？
