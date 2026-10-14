from dataclasses import dataclass, field

@dataclass
class AgentCard:
    name: str
    system_prompt: str
    output_schema: dict
    tools: list[str] = field(default_factory=list)
