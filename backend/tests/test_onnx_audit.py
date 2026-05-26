import onnx
import json
import sys
from pathlib import Path

def audit_onnx(model_path):
    print(f"\n{'='*60}")
    print(f"DEEP AUDIT FOR: {model_path}")
    print(f"{'='*60}")
    
    try:
        model = onnx.load(model_path)
    except Exception as e:
        print(f"Error loading model: {e}")
        return

    print("\n[1] BASIC INFO")
    print(f"  Producer: {model.producer_name} v{model.producer_version}")
    print(f"  Model Version: {model.model_version}")
    print(f"  IR Version: {model.ir_version}")

    print("\n[2] CUSTOM METADATA PROPS")
    if not model.metadata_props:
        print("  (Empty)")
    for prop in model.metadata_props:
        print(f"  Key: '{prop.key}'")
        try:
            # Try to format JSON values for readability
            parsed = json.loads(prop.value)
            print(f"  Value: {json.dumps(parsed, indent=4)}")
        except:
            print(f"  Value: {prop.value}")

    print("\n[3] GRAPH INPUTS")
    for inp in model.graph.input:
        print(f"  Name: {inp.name}")

    print("\n[4] GRAPH OUTPUTS")
    for out in model.graph.output:
        print(f"  Name: {out.name}")

    print("\n[5] SCANNING NAMES IN INITIALIZERS/NODES")
    # Sometimes labels are hardcoded in node names or doc_strings
    label_hints = []
    for node in model.graph.node:
        if "label" in node.name.lower() or "class" in node.name.lower():
             label_hints.append(f"Node: {node.name}")
    
    if label_hints:
        print("  Potential hints found in graph structure:")
        for h in label_hints[:5]: print(f"    {h}")
    else:
        print("  No label hints found in graph names.")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python audit_onnx.py <path_to_model>")
    else:
        audit_onnx(sys.argv[1])
