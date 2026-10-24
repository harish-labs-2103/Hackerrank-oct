"""Console factory and small rendering helpers (text status is always printed, colour optional)."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

STATUS_STYLE = {"COMPLETE": "green", "INCOMPLETE": "yellow", "NOT_APPLICABLE": "dim"}


@dataclass
class UIState:
    verbose: bool = False
    quiet: bool = False
    no_color: bool = False
    fmt: str = "text"


def make_console(ui: UIState, stderr: bool = False) -> Console:
    no_color = ui.no_color or bool(os.environ.get("NO_COLOR")) or not sys.stdout.isatty()
    return Console(stderr=stderr, no_color=no_color, highlight=False, soft_wrap=False, width=None if sys.stdout.isatty() else 120)


def banner(title: str, subtitle: str = "") -> Panel:
    body = f"[bold]{title}[/bold]" + (f"\n{subtitle}" if subtitle else "")
    return Panel(body, expand=False)


def table(*cols: str, title: str | None = None) -> Table:
    t = Table(title=title, show_lines=False, header_style="bold")
    for c in cols:
        t.add_column(c)
    return t


def status_text(status: str) -> str:
    style = STATUS_STYLE.get(status, "")
    return f"[{style}]{status}[/{style}]" if style else status
