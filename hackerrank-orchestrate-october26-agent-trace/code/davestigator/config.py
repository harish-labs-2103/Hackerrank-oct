"""Configuration: built-in defaults, optionally overridden by davestigator.yaml.

No secrets live here. The only runtime knobs from the environment are OLLAMA_HOST and
DAVESTIGATOR_CONFIG (the old names data-investigator.yaml / DATA_INVESTIGATOR_CONFIG
are still read as a fallback).
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_NAME = "davestigator.yaml"
LEGACY_CONFIG_NAME = "data-investigator.yaml"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AppCfg(_Strict):
    log_level: str = "INFO"


class DatasetCfg(_Strict):
    root: str = "dataset"


class ReportsCfg(_Strict):
    directory: str = "reports"
    format: str = "json"


class OutputCfg(_Strict):
    path: str = "output.csv"


class InvestigationCfg(_Strict):
    default_mode: str = "read_only"
    max_agent_iterations: int = 6
    max_tool_calls: int = 12
    case_timeout_s: int = 60
    workers: int = 1


class AgentCfg(_Strict):
    enabled: str = "auto"  # auto | on | off
    provider: str = "ollama"
    model: str = "gemma3:4b"
    num_ctx: int = 8192
    cache_dir: str = "code/cache/llm"
    offline: bool = False  # cache-only replay


class SecurityCfg(_Strict):
    allow_destructive_actions: bool = False
    require_approval: bool = True
    allow_reveal: bool = False


class ConnectorCfg(_Strict):
    name: str
    kind: str  # sql | search | document | kv | object
    url_env: str  # environment variable holding the connection URL (never the URL itself)
    options: dict = {}


class LiveCfg(_Strict):
    mappings: str = "mappings.yaml"
    draft: str = "mappings.draft.yaml"
    rules: str = "dataset/deletion_rules.csv"  # policy used for live audits
    journal_dir: str = "reports/evidence"


class Settings(_Strict):
    app: AppCfg = AppCfg()
    dataset: DatasetCfg = DatasetCfg()
    reports: ReportsCfg = ReportsCfg()
    output: OutputCfg = OutputCfg()
    investigation: InvestigationCfg = InvestigationCfg()
    agent: AgentCfg = AgentCfg()
    security: SecurityCfg = SecurityCfg()
    connectors: list[ConnectorCfg] = []
    live: LiveCfg = LiveCfg()

    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else REPO_ROOT / p


def config_path(explicit: str | None = None) -> Path:
    chosen = explicit or os.environ.get("DAVESTIGATOR_CONFIG") or os.environ.get("DATA_INVESTIGATOR_CONFIG")
    if chosen:
        return Path(chosen)
    current, legacy = REPO_ROOT / CONFIG_NAME, REPO_ROOT / LEGACY_CONFIG_NAME
    return legacy if legacy.is_file() and not current.is_file() else current


def load_settings(explicit: str | None = None) -> tuple[Settings, Path | None]:
    p = config_path(explicit)
    if p.is_file():
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return Settings.model_validate(data), p
    if explicit:
        raise FileNotFoundError(f"config file not found: {p}")
    return Settings(), None


def default_yaml() -> str:
    return yaml.safe_dump(Settings().model_dump(), sort_keys=False)
