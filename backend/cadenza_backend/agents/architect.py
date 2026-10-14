import json
from pydantic import BaseModel, Field
from cadenza_backend.agents.model_client import ModelClient
from cadenza_backend.agents.core import AgentCard
from cadenza_backend.contracts_bridge import Ledger

class BOMItem(BaseModel):
    name: str = Field(description="Name of the part")
    is_standard: bool = Field(description="True if this is a standard catalog part (e.g. fastener), False if custom geometry")
    description: str = Field(description="Description or requirements for the part")

class ArchitectOutput(BaseModel):
    is_assembly: bool = Field(description="True if the prompt describes an assembly with multiple parts, False for a single continuous part")
    is_edit: bool = Field(description="True if this is modifying existing geometry, False if creating new")
    bom: list[BOMItem] = Field(default_factory=list, description="Bill of Materials for the assembly. Empty if not an assembly.")
    summary: str = Field(description="Brief explanation of the architectural decision")

async def run_architect(prompt: str, ledger: Ledger, client: ModelClient, has_target: bool = False,
                        *, images: list[tuple[str, bytes]] | None = None,
                        all_ledgers: dict[str, Ledger] | None = None,
                        active_ledger_ids: list[str] | None = None) -> ArchitectOutput:
    sys_prompt = (
        "You are the CADenza Architect. Decide how to fulfill the user's request. "
        "If they are asking for an assembly with multiple discrete parts (e.g., 'a box with a lid and 4 screws', 'a robotic arm joint'), "
        "set is_assembly=true and generate a BOM. "
        "If they are just asking for a single continuous shape (e.g., 'a plate with a hole', 'a bracket'), set is_assembly=false. "
        "If they are modifying an existing design (or have clicked a specific target), set is_edit=true. "
        "Use the supplied current_ledgers and available_ledgers to distinguish existing geometry from requested additions. "
        "An attached drawing can specify an edit, a new part or an assembly; its presence alone does not mean creation. "
        "Use images as drawing evidence, including part identities and dimensions in the BOM; do not invent unreadable dimensions. "
        "For new assembly composition from available sources, set is_assembly=true and is_edit=false. "
        "For edits to existing dimensions or placements, set is_edit=true."
    )
    
    documents = all_ledgers if all_ledgers is not None else {ledger.project_id: ledger}
    selected = {lid: documents[lid] for lid in (active_ledger_ids or []) if lid in documents}
    selected = selected or {ledger.project_id: ledger}
    user_context = {
        "instruction": prompt, "has_target": has_target, "primary_ledger_id": ledger.project_id,
        "active_ledger_ids": active_ledger_ids or [],
        "current_ledgers": {lid: doc.model_dump(mode="json") for lid, doc in selected.items()},
        "available_ledgers": {lid: {"name": doc.metadata.name, "is_assembly": doc.assembly is not None,
                                    "feature_count": len(doc.features)} for lid, doc in documents.items()},
    }
    
    card = AgentCard(
        name="architect",
        system_prompt=sys_prompt,
        output_schema=ArchitectOutput.model_json_schema()
    )
    
    res = await client.complete_json(
        stage="architect",
        agent_card=card,
        user=json.dumps(user_context),
        images=images,
    )
    
    return ArchitectOutput.model_validate(res)
