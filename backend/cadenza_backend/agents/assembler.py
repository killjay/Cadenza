from typing import Any
from pydantic import BaseModel, Field
from cadenza_backend.agents.model_client import ModelClient
from cadenza_backend.agents.core import AgentCard
from cadenza_contracts.ledger import Joint, Component

class AssemblerOutput(BaseModel):
    components: list[Component] = Field(description="The components to instantiate in this assembly")
    joints: list[Joint] = Field(description="The joints required to assemble the parts")
    summary: str = Field(description="Summary of the assembly process")
    assumptions: list[dict[str, Any]] = Field(default_factory=list, description="Any assumptions made during assembly")
    needs_clarification: str | None = Field(default=None, description="Ask a question if assembly is impossible")

async def run_assembler(prompt: str, parts_context: str, client: ModelClient,
                        *, images: list[tuple[str, bytes]] | None = None) -> AssemblerOutput:
    sys_prompt = (
        "You are the CADenza Assembler. You are given a list of available sub-assemblies (parts) as Ledgers, along with the user's assembly instructions. "
        "Your job is to generate the `Component` objects to instantiate these parts into the assembly, and the `Joint` objects to connect them. "
        "The context contains the accepted primary_ledger, sourced_parts, secondary_ledgers and requested bom. "
        "Reference sourced_parts or existing primary assembly parts using part_id; reference secondary ledgers using sub_assembly_id. "
        "Return only new components and joints; do not repeat already accepted components. Instantiate a reusable part multiple times with unique component IDs when needed. "
        "Never invent source IDs or silently omit required parts. You cannot generate missing custom parts here: ask for them using needs_clarification. "
        "Catalog bolts are simplified nominal fastener geometry, not a certified reproduction of a standard. "
        "Only rigid attachment joints are implemented. If motion joints are required, ask for clarification; never substitute rigid for requested motion. "
        "Joint target points/normals are assembly coordinates after initial component placement, before joints are solved. "
        "A anchors B; each B may have only one incoming joint, with no cycles or closed loops. "
        "Supply both outward normals to align them oppositely, or omit both for translation only. Initial placement controls twist. "
        "Do not use reference_feature in joint targets; persistent feature-bound anchors are not implemented. "
        "If a previous attempt was rejected, regenerate the entire proposed addition against the accepted base, fixing the reported error. "
        "Rejected operations are context only and were never accepted. If clarification is needed, return empty components and joints."
    )
    
    card = AgentCard(
        name="assembler",
        system_prompt=sys_prompt + (" Use the attached drawings for component identity and placement dimensions. "
            "Do not infer exact dimensions from pixel scale. Ask for clarification when required dimensions "
            "are unreadable or conflict with the instruction." if images else ""),
        output_schema=AssemblerOutput.model_json_schema()
    )
    
    res = await client.complete_json(
        stage="assembler",
        agent_card=card,
        user=f"Instructions: {prompt}\n\nAvailable Parts:\n{parts_context}",
        images=images,
    )
    
    return AssemblerOutput.model_validate(res)
