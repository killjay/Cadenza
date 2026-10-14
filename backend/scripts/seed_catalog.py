import sqlite3
import json
import os

CATALOG_DB = os.path.join(os.path.dirname(__file__), "..", "cadenza_backend", "data", "catalog.db")

def seed():
    os.makedirs(os.path.dirname(CATALOG_DB), exist_ok=True)
    conn = sqlite3.connect(CATALOG_DB)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS catalog_parts (
            sku TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            type TEXT NOT NULL,
            standard TEXT NOT NULL,
            parameters JSON NOT NULL
        )
    """)

    parts = [
        {
            "sku": "ISO-4017-M10x50",
            "category": "Fastener",
            "type": "Hexagon Head Bolt",
            "standard": "ISO 4017",
            "parameters": {"thread": "M10", "length": 50, "head_width": 17, "head_height": 6.4, "thread_length": 26}
        },
        {
            "sku": "ISO-4017-M10x20",
            "category": "Fastener",
            "type": "Hexagon Head Bolt",
            "standard": "ISO 4017",
            "parameters": {"thread": "M10", "length": 20, "head_width": 17, "head_height": 6.4, "thread_length": 20}
        },
        {
            "sku": "ISO-7089-M10",
            "category": "Fastener",
            "type": "Plain Washer",
            "standard": "ISO 7089",
            "parameters": {"inner_diameter": 10.5, "outer_diameter": 20, "thickness": 2}
        },
        {
            "sku": "ISO-4032-M10",
            "category": "Fastener",
            "type": "Hexagon Nut",
            "standard": "ISO 4032",
            "parameters": {"thread": "M10", "width": 17, "height": 8.4}
        },
    ]

    cursor.executemany("""
        INSERT OR REPLACE INTO catalog_parts (sku, category, type, standard, parameters)
        VALUES (:sku, :category, :type, :standard, :parameters)
    """, [{"sku": p["sku"], "category": p["category"], "type": p["type"], "standard": p["standard"], "parameters": json.dumps(p["parameters"])} for p in parts])

    conn.commit()
    conn.close()
    print(f"Catalog seeded with {len(parts)} parts at {CATALOG_DB}")

if __name__ == "__main__":
    seed()
