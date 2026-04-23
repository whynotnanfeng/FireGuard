# ONNX Model Export Guide

To improve the user experience and ensure consistency, we recommend embedding class labels directly into your ONNX model's metadata. This allows the FireGuard platform to automatically populate the "Label Configuration" during model upload.

## Automatic Label Extraction
When you upload a `.onnx` file, the platform looks for the following keys in the model's `metadata_props`:
- `names` (Recommended: Dictionary mapping string IDs to labels)
- `classes`
- `categories`

## Export Script Example
Use the following Python script after exporting your model (e.g., from YOLOv8) to inject the necessary metadata.

```python
import onnx
import json

def inject_metadata(model_path, output_path, names_dict):
    """
    Inject class names into ONNX metadata.
    
    Args:
        model_path: Path to the original .onnx file
        output_path: Path to save the modified .onnx file
        names_dict: Dictionary mapping class IDs to names, 
                    e.g., {0: 'fire', 1: 'smoke'}
    """
    # Load model
    model = onnx.load(model_path)
    
    # Standardize names_dict keys to strings for JSON
    json_names = {str(k): str(v) for k, v in names_dict.items()}
    names_json = json.dumps(json_names)

    # Check if 'names' metadata already exists, update it if so
    meta_found = False
    for prop in model.metadata_props:
        if prop.key == 'names':
            prop.value = names_json
            meta_found = True
            break
    
    # If not found, add a new property
    if not meta_found:
        meta = model.metadata_props.add()
        meta.key = 'names'
        meta.value = names_json

    # Save modified model
    onnx.save(model, output_path)
    print(f"Successfully injected metadata into {output_path}")

# Example Usage:
if __name__ == "__main__":
    my_names = {
        0: "smoke",
        1: "fire",
        2: "person"
    }
    inject_metadata("best.onnx", "best_with_meta.onnx", my_names)
```

## Why do this?
- **Speed**: No manual entry of label IDs and names.
- **Accuracy**: Prevents typos or ID mismatches between the model and the platform configuration.
- **Overwrite Priority**: If the platform detects metadata, it will prioritize it over manual inputs during the upload process to ensure the system is correctly synced with the model.
