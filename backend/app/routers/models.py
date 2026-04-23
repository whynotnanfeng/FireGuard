import json
import logging
import os
import shutil
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlmodel import Session, select

from app.config import config
from app.database import get_session
from app.dependencies import check_storage_limit, get_current_user
from app.models.model import DetectionModel
from app.models.task import Task
from app.models.user import User
from app.services.detector import get_detector, Detector
import tempfile

router = APIRouter(prefix="/models", tags=["models"])
logger = logging.getLogger(__name__)

ALLOWED_FORMATS = {".onnx"}


# ── Schemas ──────────────────────────────────────────────────────────────────

class ModelResponse(BaseModel):
    id: str
    name: str
    format: str
    input_types: list
    description: str
    status: str
    label_config: Optional[dict]
    default_threshold: float
    class_names: list
    created_at: str


class ModelListResponse(BaseModel):
    total: int
    items: List[ModelResponse]


class ModelUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    label_config: Optional[dict] = None


def _to_response(m: DetectionModel) -> ModelResponse:
    return ModelResponse(
        id=m.id,
        name=m.name,
        format=m.format,
        input_types=json.loads(m.input_types),
        description=m.description,
        status=m.status,
        label_config=json.loads(m.label_config) if m.label_config else None,
        default_threshold=m.default_threshold,
        class_names=json.loads(m.class_names) if m.class_names else [],
        created_at=m.created_at.isoformat(),
    )


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("", response_model=ModelListResponse)
def list_models(
    skip: int = 0,
    limit: int = 20,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    query = select(DetectionModel).where(
        DetectionModel.user_id == current_user.id
    ).offset(skip).limit(limit)
    items = session.exec(query).all()

    total_query = select(DetectionModel).where(DetectionModel.user_id == current_user.id)
    total = len(session.exec(total_query).all())

    return ModelListResponse(total=total, items=[_to_response(m) for m in items])


@router.post("/analyze")
async def analyze_model(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """
    Extract metadata from a model file without saving it to the database.
    Used for real-time label extraction in the UI.
    """
    _, ext = os.path.splitext(file.filename or "")
    ext = ext.lower()
    if ext not in ALLOWED_FORMATS:
        raise HTTPException(status_code=400, detail=f"Only {ALLOWED_FORMATS} files allowed")

    # Save to temp file for analysis
    fd, temp_path = tempfile.mkstemp(suffix=ext)
    try:
        content = await file.read()
        with os.fdopen(fd, 'wb') as tmp:
            tmp.write(content)
        
        # Analyze using Detector (bypass singleton cache for one-off analysis)
        detector = Detector(temp_path)
        return {
            "label_config": detector.metadata_label_map or {},
            "class_names": detector.class_names,
            "default_threshold": float(getattr(detector.model, 'conf_thresh', 0.25)) if hasattr(detector, 'model') else 0.25
        }
    except Exception as e:
        logger.error(f"Analysis failed: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to analyze model: {str(e)}")
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


@router.post("", response_model=ModelResponse, status_code=201)
async def create_model(
    name: str = Form(...),
    input_types: str = Form(...),          # JSON array string e.g. '["rgb"]'
    description: str = Form(default=""),
    label_config: Optional[str] = Form(default=None), # JSON string of mapping
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    # Validate file extension
    _, ext = os.path.splitext(file.filename or "")
    ext = ext.lower()
    if ext not in ALLOWED_FORMATS:
        raise HTTPException(status_code=400, detail=f"Only {ALLOWED_FORMATS} files allowed")

    # Validate input_types JSON
    try:
        types_list = json.loads(input_types)
        if not isinstance(types_list, list) or not types_list:
            raise ValueError
        for t in types_list:
            if t not in ("rgb", "ir"):
                raise ValueError
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail='input_types must be JSON array of "rgb"/"ir"')

    # Read file content
    content = await file.read()
    file_size = len(content)

    # Check storage limit
    if not check_storage_limit(current_user.id, file_size):
        raise HTTPException(status_code=400, detail="Storage limit exceeded. Please delete some tasks or models.")

    # Create DB record first
    dm = DetectionModel(
        user_id=current_user.id,
        name=name,
        format=ext.lstrip("."),
        input_types=json.dumps(types_list),
        file_path="",
        description=description,
        status="creating",
        label_config=label_config
    )
    session.add(dm)
    session.commit()
    session.refresh(dm)

    # Save file
    model_dir = config.MODELS_DIR / current_user.id
    model_dir.mkdir(parents=True, exist_ok=True)
    file_path = model_dir / f"{dm.id}{ext}"
    with open(file_path, "wb") as f:
        f.write(content)

    # Update record
    dm.file_path = str(file_path)
    dm.status = "completed"
    
    try:
        detector = get_detector(str(file_path))
        dm.class_names = json.dumps(detector.class_names)
            
        if hasattr(detector, 'model') and hasattr(detector.model, 'conf_thresh'):
            dm.default_threshold = float(detector.model.conf_thresh)
    except Exception as e:
        logger.warning(f"Failed to extract model metadata: {e}")
        dm.class_names = "[]"
        dm.default_threshold = 0.25
    
    session.add(dm)
    session.commit()
    session.refresh(dm)

    return _to_response(dm)


@router.get("/{model_id}", response_model=ModelResponse)
def get_model(
    model_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    dm = session.get(DetectionModel, model_id)
    if not dm:
        raise HTTPException(status_code=404, detail="Model not found")
    if dm.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    return _to_response(dm)


@router.put("/{model_id}", response_model=ModelResponse)
def update_model(
    model_id: str,
    body: ModelUpdateRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    # Forced sync check log to bypass any logging buffering
    try:
        log_file = config.BASE_DIR / "logs" / "sync_check.log"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"{datetime.utcnow().isoformat()} - Model: {model_id} - Payload: {json.dumps(body.dict())}\n")
    except:
        pass

    logger.info(f"DEBUG: update_model called for {model_id} with body: {body.dict()}")
    dm = session.get(DetectionModel, model_id)
    if not dm:
        raise HTTPException(status_code=404, detail="Model not found")
    if dm.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    if body.name is not None:
        dm.name = body.name
    if body.description is not None:
        dm.description = body.description
    if body.label_config is not None:
        logger.info(f"Updating label_config for model {model_id}: {body.label_config}")
        dm.label_config = json.dumps(body.label_config)
    
    session.add(dm)
    session.commit()
    session.refresh(dm)
    logger.info(f"Model {model_id} updated successfully")
    return _to_response(dm)


@router.delete("/{model_id}")
def delete_model(
    model_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    dm = session.get(DetectionModel, model_id)
    if not dm:
        raise HTTPException(status_code=404, detail="Model not found")
    if dm.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    # Check if any task references this model
    ref_tasks = session.exec(select(Task).where(Task.model_id == model_id)).all()
    if ref_tasks:
        ids = [t.id for t in ref_tasks]
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete model: currently used by tasks {ids}",
        )

    # Delete file
    if dm.file_path and os.path.exists(dm.file_path):
        os.remove(dm.file_path)

    session.delete(dm)
    session.commit()
    return {"message": "Model deleted"}
