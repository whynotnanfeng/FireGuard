import pytest
import io
import json
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from sqlmodel import Session
from app.models.model import DetectionModel

@pytest.fixture(autouse=True)
def mock_background_tasks():
    # Prevent background tasks from starting and causing event loop conflicts
    with patch("app.services.task_runner.task_runner.start"), \
         patch("app.services.task_runner.stream_manager.start_monitor"):
        yield

def test_model_upload_auto_populate_metadata(client: TestClient, auth_headers: dict, db_session: Session):
    """
    Verify that uploading a model with ONNX metadata automatically 
    populates/overwrites the label_config in the database.
    """
    # 1. Setup mock detector
    mock_detector = MagicMock()
    mock_detector.metadata_label_map = {"0": "person", "1": "fire"}
    mock_detector.class_names = ["person", "fire"]
    
    # 2. Mock get_detector in the models router
    # Note: the import is inside the function in the router, 
    # so we patch the local import in the module.
    with patch("app.routers.models.get_detector", return_value=mock_detector):
        # Prepare a dummy file
        dummy_file = io.BytesIO(b"fake-onnx-content")
        
        # User explicitly provides a DIFFERENT mapping to test overwrite
        user_label_config = {"0": "manual-person"}
        
        # 3. Perform upload
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
        
        # 4. Verify Overwrite Priority
        # The returned data should match the MOCK DETECTOR metadata, NOT the user input
        assert data["label_config"] == {"0": "person", "1": "fire"}
        
        # Verify persistence in DB
        db_model = db_session.get(DetectionModel, data["id"])
        assert json.loads(db_model.label_config) == {"0": "person", "1": "fire"}

def test_model_upload_no_metadata(client: TestClient, auth_headers: dict, db_session: Session):
    """
    Verify that if no metadata is detected, the user's label_config is preserved.
    """
    mock_detector = MagicMock()
    mock_detector.metadata_label_map = {} # No metadata
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
        
        # Should persist the user's manual mapping
        assert data["label_config"] == {"0": "manual-only"}
