import sqlite3
import json
import os
from typing import Any

CATALOG_DB = os.path.join(os.path.dirname(__file__), "data", "catalog.db")

def query_catalog(criteria: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Query the standard parts catalog based on criteria (e.g. {'type': 'Hexagon Head Bolt', 'thread': 'M10'}).
    """
    if not os.path.exists(CATALOG_DB):
        return []
        
    conn = sqlite3.connect(CATALOG_DB)
    cursor = conn.cursor()
    
    query = "SELECT sku, category, type, standard, parameters FROM catalog_parts WHERE 1=1"
    params = []
    
    if "type" in criteria:
        query += " AND type = ?"
        params.append(criteria["type"])
    if "category" in criteria:
        query += " AND category = ?"
        params.append(criteria["category"])
        
    for k, v in criteria.items():
        if k not in ["type", "category", "standard"]:
            query += f" AND json_extract(parameters, '$.{k}') = ?"
            params.append(v)
            
    cursor.execute(query, params)
    results = []
    for row in cursor.fetchall():
        results.append({
            "sku": row[0],
            "category": row[1],
            "type": row[2],
            "standard": row[3],
            "parameters": json.loads(row[4])
        })
        
    conn.close()
    return results

def get_part_by_sku(sku: str) -> dict[str, Any] | None:
    if not os.path.exists(CATALOG_DB):
        return None
        
    conn = sqlite3.connect(CATALOG_DB)
    cursor = conn.cursor()
    cursor.execute("SELECT sku, category, type, standard, parameters FROM catalog_parts WHERE sku = ?", (sku,))
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        return None
        
    return {
        "sku": row[0],
        "category": row[1],
        "type": row[2],
        "standard": row[3],
        "parameters": json.loads(row[4])
    }
