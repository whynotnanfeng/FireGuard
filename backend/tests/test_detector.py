import pytest
import numpy as np
from unittest.mock import MagicMock, patch
from app.services.detector import get_detector, Detector, Detection

@pytest.fixture
def mock_onnx_session():
    with patch("onnxruntime.InferenceSession") as mock_session:
        # Mock session behavior
        session_instance = mock_session.return_value
        session_instance.get_modelmeta.return_value.custom_metadata_map = {"names": '{"0": "person", "1": "car"}'}
        session_instance.get_inputs.return_value = [MagicMock(name="input")]
        session_instance.get_outputs.return_value = [MagicMock(name="output")]
        
        # Mock run output: [x1, y1, x2, y2, conf, cls] 
        # Row: [10, 10, 100, 100, 0.9, 0] (Class 0: person)
        session_instance.run.return_value = [np.array([[[10, 10, 100, 100, 0.9, 0]]], dtype=np.float32)]
        yield session_instance

def test_detector_singleton(mock_onnx_session):
    d1 = get_detector("dummy.onnx")
    d2 = get_detector("dummy.onnx")
    assert d1 is d2

def test_label_mapping_isolation(mock_onnx_session):
    detector = Detector("dummy.onnx")
    detector.session = mock_onnx_session
    detector.input_name = "input"
    detector.class_names = ["person", "car"]
    
    img = np.zeros((640, 640, 3), dtype=np.uint8)
    
    # Test case 1: Custom mapping A
    map_a = {0: "Fire"}
    dets_a = detector.detect(img, label_mapping=map_a)
    assert dets_a[0].class_name == "Fire"
    
    # Test case 2: Custom mapping B
    map_b = {0: "Smoke"}
    dets_b = detector.detect(img, label_mapping=map_b)
    assert dets_b[0].class_name == "Smoke"
    
    # Test case 3: Fallback to model metadata
    dets_none = detector.detect(img, label_mapping=None)
    assert dets_none[0].class_name == "person"

def test_mapping_with_string_keys(mock_onnx_session):
    detector = Detector("dummy.onnx")
    detector.session = mock_onnx_session
    detector.input_name = "input"
    detector.class_names = ["person", "car"]
    
    img = np.zeros((640, 640, 3), dtype=np.uint8)
    
    # JSON often parses keys as strings
    map_str = {"0": "Alert"}
    dets = detector.detect(img, label_mapping=map_str)
    assert dets[0].class_name == "Alert"

def test_rt_detr_variant_mapping(mock_onnx_session):
    detector = Detector("dummy.onnx")
    detector.is_rgbir = True # Force RT-DETR logic path
    detector.session = mock_onnx_session
    detector.class_names = ["person"]
    
    # RT-DETR expected outputs: pred_boxes, pred_scores
    # pred_boxes: [1, N, 4], pred_scores: [1, N, 3] (3 classes as per current impl)
    boxes = np.zeros((1, 1, 4), dtype=np.float32)
    boxes[0, 0] = [0.5, 0.5, 0.2, 0.2] # cx, cy, w, h
    scores = np.zeros((1, 1, 3), dtype=np.float32)
    scores[0, 0, 0] = 0.95
    
    mock_onnx_session.run.return_value = [boxes, scores]
    
    img_rgb = np.zeros((640, 640, 3), dtype=np.uint8)
    img_ir = np.zeros((640, 640, 3), dtype=np.uint8)
    
    map_rt = {0: "Multimodal_Fire"}
    dets = detector.detect([img_rgb, img_ir], label_mapping=map_rt)
    assert dets[0].class_name == "Multimodal_Fire"

def test_videostream_mapping_propagation():
    from app.services.video_stream import VideoStream
    mock_det = MagicMock()
    # Mock return value for detect
    mock_det.detect.return_value = []
    
    mapping = {"0": "CustomLabel"}
    vs = VideoStream("task-1", "rtsp://test", mock_det, token=None, label_mapping=mapping)
    
    assert vs.label_mapping == mapping
    
    # Simulate a frame capture and detection logic
    # instead of calling the infinite loop _grab_and_detect, 
    # we test the specific detect call logic that was updated.
    vs._latest_frames = [np.zeros((640, 640, 3), dtype=np.uint8)]
    
    # We mock out the loop and dependencies
    with patch("app.services.task_runner.stream_manager.is_active", return_value=True), \
         patch("time.time", side_effect=[100, 100]), \
         patch("time.sleep"):
        
        # We manually trigger the detection block logic
        # or we just call _grab_and_detect and make it stop immediately
        vs._stopped = False
        def stop_after_one(*args, **kwargs):
            vs._stopped = True
            return [] # mock detections
        
        mock_det.detect.side_effect = stop_after_one
        
        vs._grab_and_detect()
        
        # Verify mapping was passed to detect
        args, kwargs = mock_det.detect.call_args
        assert kwargs["label_mapping"] == mapping

def test_metadata_extraction():
    """Verify that detector correctly extract labels from various metadata formats."""
    with patch("onnxruntime.InferenceSession") as mock_session:
        session_instance = mock_session.return_value
        session_instance.get_inputs.return_value = [MagicMock(name="input")]
        session_instance.get_outputs.return_value = [MagicMock(name="output")]
        
        # Scenario 1: Dictionary names (YOLO format)
        session_instance.get_modelmeta.return_value.custom_metadata_map = {
            "names": '{"0": "smoke", "1": "fire"}'
        }
        det1 = Detector("test1.onnx")
        assert det1.metadata_label_map == {"0": "smoke", "1": "fire"}
        assert det1.class_names == ["smoke", "fire"]
        
        # Scenario 2: List format
        session_instance.get_modelmeta.return_value.custom_metadata_map = {
            "classes": '["cat", "dog"]'
        }
        det2 = Detector("test2.onnx")
        assert det2.metadata_label_map == {"0": "cat", "1": "dog"}
        assert det2.class_names == ["cat", "dog"]

        # Scenario 3: Mixed numbering/Unordered dict (should be sorted by int keys)
        session_instance.get_modelmeta.return_value.custom_metadata_map = {
            "names": '{"10": "last", "0": "first"}'
        }
        det3 = Detector("test3.onnx")
        assert det3.metadata_label_map == {"10": "last", "0": "first"}
        assert det3.class_names == ["first", "last"] # Correctly sorted
