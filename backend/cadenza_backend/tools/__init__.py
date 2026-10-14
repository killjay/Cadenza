from .base import Tool, ToolRegistry

# Global registry instance
registry = ToolRegistry()

# Import tool modules so they register themselves
from . import hardware
from . import sandbox
from . import probe

__all__ = ["registry", "Tool"]
