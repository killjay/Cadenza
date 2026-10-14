from typing import Any

class Tool:
    name: str
    description: str
    schema: dict
    
    def execute(self, context: dict | None = None, **kwargs) -> Any:
        raise NotImplementedError("Subclasses must implement execute()")

    async def execute_async(self, context: dict | None = None, **kwargs) -> Any:
        return self.execute(context=context, **kwargs)

class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}
        
    def register(self, tool: Tool):
        self._tools[tool.name] = tool
        
    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise ValueError(f"Tool '{name}' not found in registry.")
        return self._tools[name]
