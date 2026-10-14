import json
import os
from typing import Any, Dict

from .base import Tool
from . import registry

def get_hardware_db_path() -> str:
    """Return the path to the hardware database directory."""
    # This assumes cadenza_backend/tools/hardware.py, so data is at ../data/hardware
    return os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "hardware")

class HardwareLookupTool(Tool):
    name = "lookup_hardware_dimensions"
    description = "Look up standard manufacturing dimensions (clearance hole, tap drill, counterbore, etc.) for a specific fastener. Use this whenever the user requests standard hardware (e.g., 'M8 bolt hole')."
    schema = {
        "name": "lookup_hardware_dimensions",
        "description": "Look up standard manufacturing dimensions (clearance hole, tap drill, counterbore, etc.) for a specific fastener. Use this whenever the user requests standard hardware (e.g., 'M8 bolt hole').",
        "input_schema": {
            "type": "object",
            "properties": {
                "fastener_type": {
                    "type": "string",
                    "description": "The category of fastener. Currently supported: 'metric_socket_head'"
                },
                "size": {
                    "type": "string",
                    "description": "The nominal size (e.g., 'M6', 'M8', 'M10')"
                }
            },
            "required": ["fastener_type", "size"]
        }
    }

    def execute(self, **kwargs) -> Dict[str, Any]:
        fastener_type = kwargs.get("fastener_type", "").strip().lower()
        size = kwargs.get("size", "").strip().upper()
        
        file_path = os.path.join(get_hardware_db_path(), f"{fastener_type}.json")
        
        if not os.path.exists(file_path):
            return {"error": f"Fastener type '{fastener_type}' not found in database. Currently available: metric_socket_head"}
            
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                
            if size not in data:
                available_sizes = list(data.keys())
                return {"error": f"Size '{size}' not found for {fastener_type}. Available sizes: {', '.join(available_sizes)}"}
                
            return data[size]
        except Exception as e:
            return {"error": f"Failed to read database: {str(e)}"}

# Register the tool
registry.register(HardwareLookupTool())
