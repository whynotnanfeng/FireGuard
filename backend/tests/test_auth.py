import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session


class TestAuthRegister:
    def test_register_success(self, client: TestClient):
        response = client.post(
            "/api/auth/register",
            json={"username": "newuser", "password": "password123"},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["username"] == "newuser"
        assert "id" in data
        assert "created_at" in data
        assert "password" not in data
        assert "password_hash" not in data

    def test_register_duplicate_username(self, client: TestClient, test_user: Session):
        response = client.post(
            "/api/auth/register",
            json={"username": "testuser", "password": "password123"},
        )
        assert response.status_code == 400
        assert "already taken" in response.json()["detail"]

    def test_register_short_username(self, client: TestClient):
        response = client.post(
            "/api/auth/register",
            json={"username": "a", "password": "password123"},
        )
        assert response.status_code == 400
        assert "2-50 characters" in response.json()["detail"]

    def test_register_short_password(self, client: TestClient):
        response = client.post(
            "/api/auth/register",
            json={"username": "validuser", "password": "12345"},
        )
        assert response.status_code == 400
        assert "at least 6 characters" in response.json()["detail"]

    def test_register_long_username(self, client: TestClient):
        response = client.post(
            "/api/auth/register",
            json={"username": "a" * 51, "password": "password123"},
        )
        assert response.status_code == 400
        assert "2-50 characters" in response.json()["detail"]


class TestAuthLogin:
    def test_login_success(self, client: TestClient, test_user: Session):
        response = client.post(
            "/api/auth/login",
            json={"username": "testuser", "password": "testpassword123"},
        )
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"

    def test_login_wrong_password(self, client: TestClient, test_user: Session):
        response = client.post(
            "/api/auth/login",
            json={"username": "testuser", "password": "wrongpassword"},
        )
        assert response.status_code == 401
        assert "Incorrect username or password" in response.json()["detail"]

    def test_login_nonexistent_user(self, client: TestClient):
        response = client.post(
            "/api/auth/login",
            json={"username": "nonexistent", "password": "password123"},
        )
        assert response.status_code == 401


class TestAuthMe:
    def test_get_me_success(self, client: TestClient, auth_headers: dict):
        response = client.get("/api/auth/me", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["username"] == "testuser"

    def test_get_me_no_token(self, client: TestClient):
        response = client.get("/api/auth/me")
        assert response.status_code == 401

    def test_get_me_invalid_token(self, client: TestClient):
        response = client.get(
            "/api/auth/me",
            headers={"Authorization": "Bearer invalid-token"},
        )
        assert response.status_code == 401
