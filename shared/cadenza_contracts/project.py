"""Versioned editable projects; compiled artifacts are rebuilt on open."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator

from cadenza_contracts.ledger import Ledger
from cadenza_contracts.references import validate_references


class ProjectDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_version: Literal[1, 2] = 1
    name: str = Field(min_length=1, max_length=200)
    primary_ledger_id: str
    ledgers: dict[str, Ledger]
    history: list[dict[str, Ledger]] = Field(default_factory=list)
    redo: list[dict[str, Ledger]] = Field(default_factory=list)

    @field_validator("document_version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("Project document version must be an integer.")
        return value

    @model_validator(mode="after")
    def validate_snapshots(self) -> ProjectDocument:
        for snapshot in [self.ledgers, *self.history, *self.redo]:
            if self.primary_ledger_id not in snapshot:
                raise ValueError("Missing primary ledger")
            revision = snapshot[self.primary_ledger_id].revision
            for lid, ledger in snapshot.items():
                if ledger.project_id != lid or ledger.revision != revision:
                    raise ValueError("Inconsistent ledger identity or snapshot revision")
                if self.document_version == 1 and ledger.assembly and any(j.target_a.mode == "planar_face" for j in ledger.assembly.joints):
                    raise ValueError("Planar attachments require project document version 2.")
                validate_references(ledger.model_dump(mode="json"), snapshot)
        return self


def load_document(raw: dict[str, Any]) -> ProjectDocument:
    """Accept versions 1/2 or an explicit legacy single-ledger JSON document."""
    data = deepcopy(raw)
    if "document_version" not in data and data.get("schema_version") == 1:
        ledger = Ledger.model_validate(data)
        data = {"document_version": 1, "name": ledger.metadata.name,
                "primary_ledger_id": ledger.project_id, "ledgers": {ledger.project_id: data}}
    return ProjectDocument.model_validate(data)


def document_version(snapshots: list[dict[str, Ledger]]) -> Literal[1, 2]:
    return 2 if any(l.assembly and any(j.target_a.mode == "planar_face" for j in l.assembly.joints)
                    for snapshot in snapshots for l in snapshot.values()) else 1
