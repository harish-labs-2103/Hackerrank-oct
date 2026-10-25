"""Mapping files: how each collection in a data store becomes something the engine understands.

mappings.draft.yaml is generated (rules or model) and reviewed by a person; mappings.yaml is the
approved copy. Audits refuse to run on collections that are not approved.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

IDENT_KINDS = ("email", "phone", "email_hash", "user_ref", "name")
# Canonical field names understood by evidence.interpret (identifier kind -> field)
CANONICAL = {"email": "email", "phone": "phone", "email_hash": "email_hash", "user_ref": "customer_reference",
             "name": "full_name"}


class Condition(BaseModel):
    """A record-level condition, evaluated in Python on fetched fields (never sent as query text)."""

    model_config = ConfigDict(extra="forbid")
    field: str
    equals: str | int | bool | None = None
    not_null: bool | None = None
    is_null: bool | None = None

    def holds(self, rec: dict) -> bool:
        from .base import flatten_path

        v = flatten_path(rec, self.field) if "." in self.field else rec.get(self.field)
        if self.not_null is not None:
            return (v is not None) == self.not_null
        if self.is_null is not None:
            return (v is None) == self.is_null
        if isinstance(self.equals, bool):
            return str(v).strip().lower() in ({"1", "true", "t", "yes"} if self.equals else {"0", "false", "f", "no"})
        return v is not None and str(v) == str(self.equals)


class StateRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deleted_when: Condition | None = None  # soft delete -> ABSENT
    active_when: Condition | None = None  # data flows
    restorable_when: Condition | None = None  # backups


class CollectionMap(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    system: str
    category: str
    id: str
    identifiers: dict[str, str] = Field(default_factory=dict)  # kind -> field path
    text_fields: list[str] = Field(default_factory=list)  # free text scanned for emails/phones
    state: StateRules = StateRules()
    role: Literal["evidence", "identity"] = "evidence"
    review: Literal["pending", "approved"] = "pending"
    suggested_by: str = "rules"
    note: str = ""

    @field_validator("identifiers")
    @classmethod
    def _kinds(cls, v: dict[str, str]):
        bad = [k for k in v if k not in IDENT_KINDS]
        if bad:
            raise ValueError(f"unknown identifier kinds {bad}; allowed {IDENT_KINDS}")
        return v


class ConnectorMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    collections: list[CollectionMap] = Field(default_factory=list)


class MappingFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = 1
    connectors: dict[str, ConnectorMapping] = Field(default_factory=dict)


def load_mapping(path: Path) -> MappingFile:
    if not path.is_file():
        return MappingFile()
    return MappingFile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def save_mapping(path: Path, m: MappingFile) -> None:
    path.write_text(yaml.safe_dump(m.model_dump(mode="json", exclude_none=True), sort_keys=False, allow_unicode=True),
                    encoding="utf-8")


def mapping_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


def approval_problems(m: MappingFile, connector: str, known_categories: set[str]) -> list[str]:
    problems = []
    cm = m.connectors.get(connector)
    if cm is None or not cm.collections:
        return [f"{connector}: no collections in the draft"]
    for c in cm.collections:
        where = f"{connector}:{c.source}"
        if c.review != "approved":
            problems.append(f"{where}: still marked review: pending")
        if c.category not in known_categories and not (c.role == "identity" and c.category == "IDENTITY_SOURCE"):
            problems.append(f'{where}: category "{c.category}" is not in deletion_rules.csv')
        if c.role == "identity" and not ({"email"} & set(c.identifiers) and "user_ref" in c.identifiers):
            problems.append(f"{where}: identity collections need email and user_ref fields")
        if c.role == "evidence" and not c.identifiers and not c.text_fields:
            problems.append(f"{where}: no identifier fields, records could never be linked")
        if not c.id:
            problems.append(f"{where}: no id field")
    return problems
