"""AT-SPI nodes as plain values, and state bitfield decoding (pure Python)."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from linuxpilot.engine.linux.a11y.roles_table import ROLE_NAMES, STATE_BITS, STATE_NAMES
from linuxpilot.models import Rect

SpiHandle = tuple[str, str]
"""``(bus name, object path)`` of an accessible."""

IFACE_PREFIX = "org.a11y.atspi."


def decode_states(words: Sequence[int]) -> frozenset[str]:
    """State names from AT-SPI's two 32-bit words (bit ``i`` of word ``i // 32``)."""
    names: set[str] = set()
    for bit, name in STATE_NAMES.items():
        word = bit // 32
        if word < len(words) and words[word] >> (bit % 32) & 1:
            names.add(name)
    return frozenset(names)


def encode_states(names: Sequence[str] | frozenset[str]) -> list[int]:
    """The inverse of :func:`decode_states` (fixtures and tests)."""
    words = [0, 0]
    for name in names:
        bit = STATE_BITS[name]
        words[bit // 32] |= 1 << (bit % 32)
    return words


def role_name(role: int | str) -> str:
    """Role nick (``push-button``) from its number or nick."""
    return role if isinstance(role, str) else ROLE_NAMES.get(int(role), "unknown")


def short_interfaces(interfaces: Sequence[str]) -> frozenset[str]:
    """``org.a11y.atspi.Text`` → ``Text``."""
    return frozenset(i.removeprefix(IFACE_PREFIX) for i in interfaces)


@dataclass(slots=True, eq=False)
class SpiNode:
    """One accessible as read from AT-SPI (before mapping to AX-style roles)."""

    handle: SpiHandle
    role: str
    name: str = ""
    description: str = ""
    states: frozenset[str] = frozenset()
    interfaces: frozenset[str] = frozenset()
    frame: Rect | None = None
    actions: tuple[str, ...] = ()
    text: str | None = None
    value: float | None = None
    identifier: str | None = None
    attributes: dict[str, str] = field(default_factory=dict)
    child_count: int | None = None
    children: list[SpiNode] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def walk(self) -> Iterator[SpiNode]:
        """Pre-order traversal."""
        stack: list[SpiNode] = [self]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(reversed(node.children))

    @property
    def showing(self) -> bool:
        return "showing" in self.states and "visible" in self.states
