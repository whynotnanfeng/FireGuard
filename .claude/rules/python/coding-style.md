# Python 编码规范 (Python Coding Standards)

## 编码风格 (Coding Style)
- **遵循 PEP 8**: 缩进使用 4 个空格。
- **显式优于隐式**: 优先使用显式的导入和配置。
- **EAFP 原则**: 在处理潜在异常时，倾向于使用 `try-except` 而非预先检查（LBYL）。
- **类型提示 (Type Hints)**: 所有函数签名必须包含完整的类型提示（Python 3.10+ 风格，如 `int | None`）。

## FastAPI / SQLModel 最佳实践
- **模型分离**: Pydantic Schema（用于 API 请求/响应）与 SQLModel（用于数据库）应清晰分离以便于管理和验证。
- **异步原则**: 涉及 I/O 操作（DB 查询、网络请求、文件读写）必须使用 `async` 和 `await`。
- **依赖注入**: 数据库 Session 和认证逻辑应通过 FastAPI 的 `Depends` 注入。

## 注释与文档
- **Docstrings**: 每个公有类和函数必须包含 Google 风格的 Docstring，描述参数、返回值及可能的异常。
- **复杂逻辑**: 对于涉及 OpenCV 或 YOLO 的复杂检测逻辑，必须在代码中逐行注释。
