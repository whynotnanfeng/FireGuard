import pytest
import io
import json
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from sqlmodel import Session
from app.models.model import DetectionModel

@pytest.fixture(autouse=True)
def mock_background_tasks():
    # Prevent background tasks from starting and conflicting with the event loop.
    with patch("app.services.task_runner.task_runner.start"), \
         patch("app.services.task_runner.stream_manager.start_monitor"):
        yield

def test_model_upload_auto_populate_metadata(client: TestClient, auth_headers: dict, db_session: Session):
    """
    Uploading a model with ONNX metadata overwrites the user's label_config in the DB.
    """
    mock_detector = MagicMock()
    mock_detector.metadata_label_map = {"0": "person", "1": "fire"}
    mock_detector.class_names = ["person", "fire"]
    
    # The router imports get_detector locally, so patch it in the router module.
    with patch("app.routers.models.get_detector", return_value=mock_detector):
        dummy_file = io.BytesIO(b"fake-onnx-content")
        
        # Differs from the metadata mapping to prove the overwrite.
        user_label_config = {"0": "manual-person"}
        
        response = client.post(
            "/api/models",
            headers=auth_headers,
            data={
                "name": "Metadata Test Model",
                "input_types": '["rgb"]',
                "description": "testing auto-population",
                "label_config": json.dumps(user_label_config)
            },
            files={"file": ("test.onnx", dummy_file, "application/octet-stream")}
        )
        
        assert response.status_code == 201
        data = response.json()
        
        # Detector metadata wins over the user mapping.
        assert data["label_config"] == {"0": "person", "1": "fire"}
        
        db_model = db_session.get(DetectionModel, data["id"])
        assert json.loads(db_model.label_config) == {"0": "person", "1": "fire"}

def test_model_upload_no_metadata(client: TestClient, auth_headers: dict, db_session: Session):
    """
    Without ONNX metadata the user's label_config is kept.
    """
    mock_detector = MagicMock()
    mock_detector.metadata_label_map = {}
    mock_detector.class_names = []
    
    with patch("app.routers.models.get_detector", return_value=mock_detector):
        dummy_file = io.BytesIO(b"fake-onnx-content")
        user_label_config = {"0": "manual-only"}
        
        response = client.post(
            "/api/models",
            headers=auth_headers,
            data={
                "name": "Manual Model",
                "input_types": '["rgb"]',
                "label_config": json.dumps(user_label_config)
            },
            files={"file": ("manual.onnx", dummy_file, "application/octet-stream")}
        )
        
        assert response.status_code == 201
        data = response.json()
        
        assert data["label_config"] == {"0": "manual-only"}
