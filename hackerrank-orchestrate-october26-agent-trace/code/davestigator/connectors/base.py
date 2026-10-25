"""Connector contract shared by every data-store family.

A connector answers three questions and nothing else:
  discover()  which collections exist (schema only)
  find()      which records in a mapped collection carry the subject's identifiers
  coverage    whether that search covered the whole collection (FULL / PARTIAL / NOT_CHECKED)

Connectors expose no write methods. Records they return are turned into Observations by the
same `interpret_record()` used for dataset files, so the engine does not change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Protocol

COVERAGE_FULL = "FULL"
COVERAGE_PARTIAL = "PARTIAL"
COVERAGE_NOT_CHECKED = "NOT_CHECKED"


class ConnectorError(Exception):
    """Generic connector failure (unreachable, timeout, bad mapping)."""


class DriverMissing(ConnectorError):
    def __init__(self, family: str, extra: str):
        super().__init__(f"{family} driver missing: pip install -e \"code[{extra}]\"")
        self.extra = extra


class NotReadOnly(ConnectorError):
    """Credentials can modify data; Data Investigator refuses to run with them."""


class CredentialsMissing(ConnectorError):
    pass


@dataclass
class ColumnInfo:
    name: str
    type: str
    nullable: bool = True


@dataclass
class CollectionInfo:
    source: str  # table / collection / index / key pattern / bucket prefix
    columns: list[ColumnInfo]
    rows: int | None = None
    primary_key: list[str] = field(default_factory=list)
    readable: bool = True
    note: str = ""


@dataclass
class RawRecord:
    record_id: str
    fields: dict  # field name -> value, as stored
    observed_at: datetime


@dataclass
class CoverageResult:
    connector: str
    source: str
    status: str  # FULL | PARTIAL | NOT_CHECKED
    reason: str = ""
    records: int = 0


@dataclass
class ReadOnlyReport:
    ok: bool
    detail: str
    server: str = ""


class Connector(Protocol):
    name: str
    kind: str

    def test(self) -> ReadOnlyReport: ...

    def discover(self) -> list[CollectionInfo]: ...

    def sample(self, source: str, column: str, n: int) -> list: ...

    def find(self, cmap, values: dict[str, set[str]], limit: int) -> tuple[list[RawRecord], CoverageResult]: ...

    def close(self) -> None: ...


def flatten_path(doc: dict, path: str):
    """Read a dotted path (`profile.email`) from a nested document."""
    cur = doc
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def iter_values(values: dict[str, set[str]]) -> Iterable[tuple[str, str]]:
    for kind, vals in values.items():
        for v in sorted(vals):
            yield kind, v
