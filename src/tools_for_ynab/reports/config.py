"""Optional `reports.yaml`: what counts as spending and how sensitive the callouts are."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


class ReportError(ValueError):
    pass


@dataclass
class ReportConfig:
    # Category groups whose name contains any of these (case-insensitive) are left out:
    # closed trips and one-off projects would otherwise look like spending trends.
    exclude_groups: list[str] = field(default_factory=lambda: ["completed"])
    exclude_categories: list[str] = field(default_factory=list)  # exact names, case-insensitive
    window_months: int = 3  # compare the last N complete months with the N before
    min_change_dollars: float = 25.0  # per month; smaller moves aren't worth a mention
    min_change_percent: float = 30.0
    max_items: int = 6  # per section
    top_drivers: int = 2


def load_config(path: Path | None) -> ReportConfig:
    if path is None or not path.exists():
        return ReportConfig()
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ReportError(f"{path.name}: expected a mapping at the top level")
    unknown = set(raw) - set(ReportConfig.__dataclass_fields__)
    if unknown:
        raise ReportError(f"{path.name}: unknown keys {sorted(unknown)}")
    cfg = ReportConfig(**raw)
    for name in ("exclude_groups", "exclude_categories"):
        if not isinstance(getattr(cfg, name), list):
            raise ReportError(f"{path.name}: {name} must be a list")
    if cfg.window_months < 1:
        raise ReportError(f"{path.name}: window_months must be at least 1")
    return cfg
