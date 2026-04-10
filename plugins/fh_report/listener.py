"""
FH_Report Plugin — EventListener
No DCS events are handled. This file satisfies the DCSServerBot plugin structure.
"""
from core import EventListener
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .commands import FHReport


class FHReportEventListener(EventListener["FHReport"]):
    """Placeholder listener."""
    pass
