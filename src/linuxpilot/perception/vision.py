"""OCR elements (``o…`` refs); not implemented yet, so no element is a vision element."""

from __future__ import annotations

from linuxpilot.perception.model import Element


def is_vision_element(el: Element) -> bool:
    """True for OCR-derived elements (none until vision mode lands)."""
    return False
