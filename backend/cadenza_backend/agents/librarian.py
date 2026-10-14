from typing import Any
from pydantic import BaseModel, Field
from cadenza_backend.agents.model_client import ModelClient
from cadenza_backend.agents.core import AgentCard
from cadenza_backend.catalog import query_catalog

class CatalogQuery(BaseModel):
    category: str | None = Field(default=None, description="e.g., 'Fastener'")
    type: str | None = Field(default=None, description="e.g., 'Hexagon Head Bolt'")
    parameters: dict[str, Any] = Field(default_factory=dict, description="e.g., {'thread': 'M10'}")

class LibrarianOutput(BaseModel):
    queries: list[CatalogQuery] = Field(description="Queries to run against the catalog")
    summary: str = Field(description="Summary of the catalog search strategy")

async def run_librarian(prompt: str, client: ModelClient,
                        *, images: list[tuple[str, bytes]] | None = None) -> tuple[str, list[dict[str, Any]]]:
    """
    Analyzes a prompt to extract catalog queries, executes them, and returns the results.
    """
    sys_prompt = (
        "You are the CADenza Librarian. The user wants to add standard parts (like bolts, nuts, washers) "
        "to their assembly. Extract their requirements into a set of precise queries. \n"
        "Available categories: 'Fastener'. Available types: 'Hexagon Head Bolt', 'Plain Washer', 'Hexagon Nut'. \n"
        "Key parameters for fasteners include 'thread' (e.g., 'M10') and 'length' (e.g., 50)."
    )
    
    # Use the model client to parse user intent into structured queries
    card = AgentCard(
        name="librarian",
        system_prompt=sys_prompt,
        output_schema=LibrarianOutput.model_json_schema()
    )
    
    res = await client.complete_json(
        stage="librarian",
        agent_card=card,
        user=prompt,
        images=images,
    )
    
    result = LibrarianOutput.model_validate(res)
    
    all_results = []
    for q in result.queries:
        criteria = {}
        if q.category: criteria["category"] = q.category
        if q.type: criteria["type"] = q.type
        criteria.update(q.parameters)
        
        matches = query_catalog(criteria)
        all_results.append({
            "query": q.model_dump(),
            "matches": matches
        })
        
    return result.summary, all_results
