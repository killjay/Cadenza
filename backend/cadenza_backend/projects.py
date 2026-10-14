"""Explicit project saves with atomic optimistic updates."""
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from cadenza_contracts.project import ProjectDocument, load_document


class SaveConflict(ValueError):
    pass


class ProjectRepository:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        try:
            with connection:
                connection.execute("CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, version INTEGER NOT NULL, document TEXT NOT NULL, updated_at TEXT NOT NULL)")
                yield connection
        finally:
            connection.close()

    def list(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT id,name,version,updated_at FROM projects ORDER BY updated_at DESC,id").fetchall()
        return [dict(zip(("id", "name", "version", "updated_at"), row)) for row in rows]

    def load(self, project_id: str) -> tuple[ProjectDocument, int]:
        with self.connect() as db:
            row = db.execute("SELECT document,version FROM projects WHERE id=?", (project_id,)).fetchone()
        if row is None:
            raise KeyError(project_id)
        return load_document(json.loads(row[0])), row[1]

    def save(self, document: ProjectDocument, project_id: str | None, expected_version: int) -> tuple[str, int]:
        payload = ProjectDocument.model_validate(document.model_dump()).model_dump_json()
        with self.connect() as db:
            if project_id is None:
                project_id = "project_" + uuid4().hex
                version = 1
                db.execute("INSERT INTO projects VALUES (?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                           (project_id, document.name, version, payload))
            else:
                version = expected_version + 1
                changed = db.execute("UPDATE projects SET name=?, version=?, document=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=? AND version=?",
                                     (document.name, version, payload, project_id, expected_version))
                if changed.rowcount != 1:
                    raise SaveConflict("Project changed in another session; reopen it before saving.")
        return project_id, version
