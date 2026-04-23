import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session
from app.models.model import DetectionModel
from app.models.user import User


@pytest.fixture
def test_model(db_session: Session, test_user: Session) -> DetectionModel:
    import json
    model = DetectionModel(
        user_id=test_user.id,
        name="test-model",
        format="pt",
        input_types=json.dumps(["rgb"]),
        file_path="/path/to/model.pt",
        description="Test model",
        status="completed",
        label_config=json.dumps({0: "fire", 1: "smoke"}),
    )
    db_session.add(model)
    db_session.commit()
    db_session.refresh(model)
    return model


class TestTaskList:
    def test_list_tasks_empty(self, client: TestClient, auth_headers: dict):
        response = client.get("/api/tasks", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["items"] == []

    def test_list_tasks_with_auth(self, client: TestClient, auth_headers: dict):
        response = client.get("/api/tasks", headers=auth_headers)
        assert response.status_code == 200

    def test_list_tasks_no_auth(self, client: TestClient):
        response = client.get("/api/tasks")
        assert response.status_code == 401


class TestTaskCreate:
    def test_create_task_validation_invalid_type(
        self, client: TestClient, auth_headers: dict, test_model: DetectionModel
    ):
        response = client.post(
            "/api/tasks",
            headers=auth_headers,
            data={
                "name": "test-task",
                "task_type": "invalid",
                "input_types": '["rgb"]',
                "model_id": test_model.id,
                "source_type": "upload",
            },
        )
        assert response.status_code == 400
        assert "task_type" in response.json()["detail"]

    def test_create_task_validation_invalid_input_types(
        self, client: TestClient, auth_headers: dict, test_model: DetectionModel
    ):
        response = client.post(
            "/api/tasks",
            headers=auth_headers,
            data={
                "name": "test-task",
                "task_type": "image",
                "input_types": "not-json",
                "model_id": test_model.id,
                "source_type": "upload",
            },
        )
        assert response.status_code == 400

    def test_create_task_model_not_found(
        self, client: TestClient, auth_headers: dict
    ):
        response = client.post(
            "/api/tasks",
            headers=auth_headers,
            data={
                "name": "test-task",
                "task_type": "image",
                "input_types": '["rgb"]',
                "model_id": "nonexistent-model-id",
                "source_type": "upload",
            },
        )
        assert response.status_code == 404
