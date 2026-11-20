import os
import pytest
from pathlib import Path
from typing import Generator
from sqlmodel import Session, SQLModel, create_engine
from fastapi.testclient import TestClient

os.environ["SECRET_KEY"] = "test-secret-key-for-testing-only"
os.environ["ACCESS_TOKEN_EXPIRE_MINUTES"] = "60"

# --- Test Database Isolation ---
TEST_DB_URL = "sqlite:///test.db"
test_engine = create_engine(TEST_DB_URL, connect_args={"check_same_thread": False})

from app.main import app
from app.database import get_session
from app.models.user import User
from app.dependencies import hash_password
from app.limiter import limiter

# Every test shares the same client address, so unrelated tests would trip the
# login rate limiter. Rate limiting itself is not under test here.
limiter.enabled = False


@pytest.fixture(scope="function")
def db_session() -> Generator[Session, None, None]:
    # Use test_engine instead of the production engine
    SQLModel.metadata.create_all(test_engine)
    with Session(test_engine) as session:
        yield session
        session.close()
    SQLModel.metadata.drop_all(test_engine)


@pytest.fixture(scope="function")
def client(db_session: Session) -> Generator[TestClient, None, None]:
    def override_get_session():
        yield db_session

    app.dependency_overrides[get_session] = override_get_session
    # Instantiate without the context manager on purpose: entering it runs the
    # lifespan, which starts MediaMTX, Redis and the inference pool. Unit tests
    # must not depend on those external services.
    test_client = TestClient(app)
    yield test_client
    test_client.close()
    app.dependency_overrides.clear()


@pytest.fixture
def test_user(db_session: Session) -> User:
    user = User(
        username="testuser",
        password_hash=hash_password("testpassword123"),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def auth_headers(client: TestClient, test_user: User) -> dict:
    response = client.post(
        "/api/auth/login",
        json={"username": "testuser", "password": "testpassword123"},
    )
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}
