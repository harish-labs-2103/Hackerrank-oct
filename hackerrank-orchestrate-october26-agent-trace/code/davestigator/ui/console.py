"""Console factory and enhanced rendering helpers (text status is always printed, colour optional)."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

STATUS_STYLE = {
    "COMPLETE": "bold green",
    "INCOMPLETE": "bold red",
    "NOT_APPLICABLE": "dim",
}

STATUS_BADGES = {
    "COMPLETE": "[bold white on #16a34a]  COMPLETE  [/]",
    "INCOMPLETE": "[bold white on #dc2626]  INCOMPLETE — GHOST DATA FOUND  [/]",
    "NOT_APPLICABLE": "[bold black on #cbd5e1]  NOT APPLICABLE  [/]",
}


@dataclass
class UIState:
    verbose: bool = False
    quiet: bool = False
    no_color: bool = False
    fmt: str = "text"


def make_console(ui: UIState, stderr: bool = False) -> Console:
    no_color = ui.no_color or bool(os.environ.get("NO_COLOR")) or not sys.stdout.isatty()
    return Console(
        stderr=stderr,
        no_color=no_color,
        highlight=False,
        soft_wrap=False,
        width=None if sys.stdout.isatty() else 120,
    )


def banner(title: str, subtitle: str = "", border_style: str = "cyan") -> Panel:
    body = f"[bold cyan]{title}[/bold cyan]" + (f"\n[dim]{subtitle}[/dim]" if subtitle else "")
    return Panel(body, expand=False, box=box.ROUNDED, border_style=border_style)


def panel(body: str, title: str | None = None, border_style: str = "cyan", subtitle: str | None = None) -> Panel:
    return Panel(
        body,
        title=f"[bold]{title}[/bold]" if title else None,
        subtitle=f"[dim]{subtitle}[/dim]" if subtitle else None,
        expand=False,
        box=box.ROUNDED,
        border_style=border_style,
    )


def table(*cols: str, title: str | None = None, border_style: str = "cyan") -> Table:
    t = Table(title=title, show_lines=False, header_style="bold cyan", box=box.ROUNDED, border_style=border_style)
    for c in cols:
        t.add_column(c)
    return t


def status_text(status: str) -> str:
    style = STATUS_STYLE.get(status, "")
    return f"[{style}]{status}[/{style}]" if style else status


def status_badge(status: str) -> str:
    return STATUS_BADGES.get(status, f"[bold]{status}[/bold]")


def timeline_badge(kind: str) -> str:
    if kind == "REQUEST":
        return "[bold cyan][REQUEST ][/bold cyan]"
    if kind == "CLAIM":
        return "[bold sky_blue2][CLAIM   ][/bold sky_blue2]"
    if kind == "OBSERVED":
        return "[bold magenta][OBSERVED][/bold magenta]"
    if kind == "AUDIT_AT":
        return "[bold yellow][AUDIT_AT][/bold yellow]"
    return f"[{kind:<8}]"


def file_icon(ext: str) -> str:
    return ext.upper()
