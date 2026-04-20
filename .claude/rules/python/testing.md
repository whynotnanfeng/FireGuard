# Python 测试指南 (Python Testing Guide)

## 工具链
- **Test Runner**: `pytest`
- **Mocking**: `unittest.mock` 或 `pytest-mock`
- **Async Support**: `pytest-asyncio`

## 测试分类
1. **单元测试 (Unit Tests)**: 针对单个 Service 或 Utils 函数。
2. **集成测试 (Integration Tests)**: 测试 Service 与数据库的交互。
3. **API 测试**: 通过 `TestClient` 或 `AsyncClient` 模拟真实的 HTTP 请求。

## 执行命令
```bash
# 运行所有测试并显示覆盖率
pytest --cov=app tests/

# 运行特定文件夹的测试
pytest tests/api/
```

## 注意事项
- 测试文件夹必须包含 `__init__.py`。
- 每个测试函数应只验证一个逻辑点。
- 严禁在测试中真实调用远程硬件（如摄像头），应对底层 OpenCV 读取逻辑进行 Mock。
