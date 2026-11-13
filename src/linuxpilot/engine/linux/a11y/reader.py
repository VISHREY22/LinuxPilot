"""Reading AT-SPI: apps, top-level windows, and window trees (bulk cache first, bounded walk as fallback)."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from linuxpilot.engine.linux.a11y.actions import MAX_ACTIONS
from linuxpilot.engine.linux.a11y.bus import A11Y, PROPS, Bus, BusCallError, Call
from linuxpilot.engine.linux.a11y.model import SpiHandle, SpiNode, decode_states, role_name, short_interfaces
from linuxpilot.models import AppInfo, Rect

log = logging.getLogger(__name__)
REGISTRY = ("org.a11y.atspi.Registry", "/org/a11y/atspi/accessible/root")
CACHE_PATH = "/org/a11y/atspi/cache"
WALK_MAX_NODES = 1500
WALK_MAX_DEPTH = 60
WALK_DEADLINE_S = 1.5
DETAIL_MAX_NODES = 1500
DETAIL_DEADLINE_S = 1.2
MANAGED_CHILDREN_MAX = 60
CAPTURE_DEADLINE_S = 6.0
"""A full capture's walk budget (a truncated capture loses elements and turns refs stale)."""
WAVE = 48
"""Stack nodes expanded together in one batched step of a walk."""
BIG_CONTAINER = 120
"""Above this many children (list and table rows, cells) only the on-screen run of children is read."""
VISIBLE_RUN_MAX = 400
PROBES = 48
TEXT_PREVIEW = 200
SCREEN = 0
DETAIL_ROLES = frozenset(
    {
        "push-button",
        "toggle-button",
        "check-box",
        "radio-button",
        "switch",
        "menu-item",
        "check-menu-item",
        "radio-menu-item",
        "menu",
        "link",
        "page-tab",
        "combo-box",
        "entry",
        "password-text",
        "spin-button",
        "text",
        "terminal",
        "slider",
        "dial",
        "scroll-bar",
        "list-item",
        "tree-item",
        "table-row",
        "table-cell",
        "list",
        "list-box",
        "tree",
        "tree-table",
        "table",
        "scroll-pane",
        "viewport",
        "page-tab-list",
        "tool-bar",
        "heading",
        "label",
        "static",
        "dialog",
        "alert",
        "frame",
        "window",
        "document-web",
        "document-frame",
        "progress-bar",
        "image",
        "icon",
        "column-header",
        "table-column-header",
        "paragraph",
        "notification",
    }
)
"""Roles whose frames, actions and values perception can use; other nodes are structure only."""
_TEXT_ROLES = frozenset({"entry", "editbar", "spin-button", "text", "terminal", "paragraph", "combo-box"})


@dataclass(slots=True)
class _Item:
    handle: SpiHandle
    parent: SpiHandle | None
    index: int
    child_count: int
    interfaces: frozenset[str]
    name: str
    role: str
    description: str
    states: frozenset[str]
    children: list[SpiHandle] = field(default_factory=list)


def parse_items(raw: Sequence[Sequence]) -> dict[SpiHandle, _Item]:
    """``Cache.GetItems`` reply → items by handle. Accepts the 10-field and the 9-field layouts."""
    items: dict[SpiHandle, _Item] = {}
    for entry in raw:
        if len(entry) == 10:
            node, _app, parent, index, count, ifaces, name, role, desc, states = entry
        elif len(entry) == 9:
            node, parent, index, count, ifaces, name, role, desc, states = entry
        else:
            continue
        handle = (str(node[0]), str(node[1]))
        parent_handle = (
            (str(parent[0]), str(parent[1])) if parent and parent[1] not in ("", "/org/a11y/atspi/null") else None
        )
        items[handle] = _Item(
            handle,
            parent_handle,
            int(index),
            int(count),
            short_interfaces(ifaces),
            str(name),
            role_name(int(role)),
            str(desc),
            decode_states(states),
        )
    for item in items.values():
        if item.parent in items:
            items[item.parent].children.append(item.handle)
    for item in items.values():
        item.children.sort(key=lambda h: items[h].index)
    return items


def _pid_desktop_id(pid: int) -> str | None:
    """The launching ``.desktop`` id from the process environment, when readable."""
    try:
        env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return None
    values = dict(v.split(b"=", 1) for v in env if b"=" in v)
    path, launched_pid = values.get(b"GIO_LAUNCHED_DESKTOP_FILE"), values.get(b"GIO_LAUNCHED_DESKTOP_FILE_PID")
    if not path or launched_pid != str(pid).encode():  # inherited from a parent launched by GIO: not this app
        return None
    return os.path.basename(path.decode(errors="replace")).removesuffix(".desktop")


class TreeReader:
    """Reads apps, windows and trees from the accessibility bus."""

    def __init__(self, a11y: Bus) -> None:
        self.bus = a11y
        self._apps: dict[str, AppInfo] = {}
        self._no_cache: set[str] = set()
        self._no_id: set[str] = set()

    # ------------------------------------------------------------------ apps and windows
    def app_roots(self) -> list[SpiHandle]:
        children = self.bus.call(*REGISTRY, A11Y + "Accessible", "GetChildren")[0]
        return [(str(b), str(p)) for b, p in children]

    def app_info(self, root: SpiHandle) -> AppInfo:
        """Name, pid, toolkit and desktop id of an app (cached per bus name)."""
        cached = self._apps.get(root[0])
        if cached is not None:
            return cached
        name = self.bus.get(*root, A11Y + "Accessible", "Name") or root[0]
        try:
            toolkit = self.bus.get(*root, A11Y + "Application", "ToolkitName")
        except BusCallError:
            toolkit = None
        try:
            pid = int(
                self.bus.call(
                    "org.freedesktop.DBus",
                    "/org/freedesktop/DBus",
                    "org.freedesktop.DBus",
                    "GetConnectionUnixProcessID",
                    "s",
                    (root[0],),
                )[0]
            )
        except BusCallError:
            pid = 0
        info = AppInfo(name=str(name), pid=pid, bundle_id=_pid_desktop_id(pid) if pid else None, toolkit=toolkit)
        self._apps[root[0]] = info
        return info

    def apps(self) -> list[tuple[AppInfo, SpiHandle]]:
        """Running accessible apps with their root handles (apps that vanish mid-call are skipped)."""
        found = []
        live = set()
        for root in self.app_roots():
            live.add(root[0])
            try:
                found.append((self.app_info(root), root))
            except BusCallError as exc:
                log.debug("skipping app %s: %s", root, exc)
        for gone in set(self._apps) - live:
            self._apps.pop(gone, None)
        return found

    def toplevels(self, root: SpiHandle, *, with_frames: bool = True) -> list[SpiNode]:
        """An app's top-level accessibles (windows, dialogs, popups) without their children."""
        nodes: list[SpiNode] = []
        for child in self.bus.call(*root, A11Y + "Accessible", "GetChildren")[0]:
            handle = (str(child[0]), str(child[1]))
            try:
                nodes.append(self._node(handle, with_frame=with_frames))
            except BusCallError as exc:
                log.debug("skipping top-level %s: %s", handle, exc)
        return nodes

    def _node(self, handle: SpiHandle, *, with_frame: bool) -> SpiNode:
        role = role_name(int(self.bus.call(*handle, A11Y + "Accessible", "GetRole")[0]))
        name = str(self.bus.get(*handle, A11Y + "Accessible", "Name"))
        states = decode_states(self.bus.call(*handle, A11Y + "Accessible", "GetState")[0])
        node = SpiNode(handle=handle, role=role, name=name, states=states)
        if with_frame:
            node.frame = self.frame(handle)
        return node

    def frame(self, handle: SpiHandle) -> Rect | None:
        try:
            x, y, w, h = self.bus.call(*handle, A11Y + "Component", "GetExtents", "u", (SCREEN,))[0]
        except BusCallError:
            return None
        return Rect(float(x), float(y), float(w), float(h))

    # ------------------------------------------------------------------ trees
    def items(self, bus_name: str) -> dict[SpiHandle, _Item] | None:
        """Every cached node of an app in one call, or ``None`` when the app has no usable cache."""
        if bus_name in self._no_cache:
            return None
        try:
            raw = self.bus.call(bus_name, CACHE_PATH, A11Y + "Cache", "GetItems", timeout=5.0)[0]
        except BusCallError as exc:
            if exc.unsupported:
                self._no_cache.add(bus_name)
            return None
        return parse_items(raw) or None

    def tree(
        self, handle: SpiHandle, *, max_nodes: int = WALK_MAX_NODES, items: dict[SpiHandle, _Item] | None = None
    ) -> tuple[SpiNode, bool]:
        """The subtree at ``handle`` with details for the nodes that matter; ``(node, truncated)``."""
        if items is not None and handle in items:
            node = self.from_items(handle, items)
            truncated = self._expand_managed(node, items)
        else:  # a full capture may take longer than a quick fingerprint: big web pages need 2-3 s
            node, truncated = self.walk(handle, max_nodes=max_nodes, deadline_s=CAPTURE_DEADLINE_S)
        truncated |= self.details(node)
        return node, truncated

    def from_items(self, handle: SpiHandle, items: dict[SpiHandle, _Item]) -> SpiNode:
        def make(item: _Item) -> SpiNode:
            return SpiNode(
                handle=item.handle,
                role=item.role,
                name=item.name,
                description=item.description,
                states=item.states,
                interfaces=item.interfaces,
                child_count=item.child_count,
            )

        root = make(items[handle])
        stack = [(root, items[handle])]
        while stack:
            node, item = stack.pop()
            for child in item.children:
                child_node = make(items[child])
                node.children.append(child_node)
                stack.append((child_node, items[child]))
        return root

    def _expand_managed(self, root: SpiNode, items: dict[SpiHandle, _Item]) -> bool:
        """Nodes that manage their descendants keep children out of the cache: read the first few live."""
        truncated = False
        for node in list(root.walk()):
            # the cache may report 0 children for these (LibreOffice documents): ask the node itself
            if "manages-descendants" in node.states and not node.children and node.showing:
                kids, cut = self.walk_children(node.handle, MANAGED_CHILDREN_MAX)
                node.children = kids
                truncated |= cut
        return truncated

    def walk_children(self, handle: SpiHandle, limit: int) -> tuple[list[SpiNode], bool]:
        try:
            children = self.bus.call(*handle, A11Y + "Accessible", "GetChildren")[0]
        except BusCallError:
            return [], True
        out = []
        for b, p in children[:limit]:
            try:
                out.append(self._full_node((str(b), str(p))))
            except BusCallError:
                continue
        return out, len(children) > limit

    def _showing(self, child: tuple[str, str]) -> bool:
        try:
            return "showing" in decode_states(
                self.bus.call(str(child[0]), str(child[1]), A11Y + "Accessible", "GetState")[0]
            )
        except BusCallError:
            return False

    def _on_screen(self, children: list) -> list:
        """The showing runs of a long list/table's children (found by sampling, then widened); off-screen rows
        would use up the walk's budget before the rest of the window is read. Headers and rows are separate runs."""
        step = max(1, len(children) // PROBES)
        runs: list[tuple[int, int]] = []
        for probe in range(0, len(children), step):
            if any(lo <= probe < hi for lo, hi in runs) or not self._showing(children[probe]):
                continue
            lo, hi = probe, probe + 1
            while lo > 0 and probe - lo < step and self._showing(children[lo - 1]):
                lo -= 1
            while hi < len(children) and hi - lo < VISIBLE_RUN_MAX and self._showing(children[hi]):
                hi += 1
            runs.append((lo, hi))
            if sum(h - lo_ for lo_, h in runs) >= VISIBLE_RUN_MAX:
                break
        if not runs:
            return children[:MANAGED_CHILDREN_MAX]
        return [c for lo, hi in runs for c in children[lo:hi]]

    def _full_node(self, handle: SpiHandle) -> SpiNode:
        node = self._node(handle, with_frame=False)
        try:
            node.interfaces = short_interfaces(self.bus.call(*handle, A11Y + "Accessible", "GetInterfaces")[0])
        except BusCallError:
            node.interfaces = frozenset()
        return node

    def _full_nodes(self, handles: list[SpiHandle], *, light: bool = False) -> list[SpiNode | None]:
        """Role, name, states (and interfaces unless ``light``) of many nodes in batched calls; ``None`` for
        nodes that vanished."""
        per = 3 if light else 4
        calls: list[Call] = []
        for bus_name, path in handles:
            calls += [
                (bus_name, path, A11Y + "Accessible", "GetRole", None, ()),
                (bus_name, path, PROPS, "Get", "ss", (A11Y + "Accessible", "Name")),
                (bus_name, path, A11Y + "Accessible", "GetState", None, ()),
            ]
            if not light:
                calls.append((bus_name, path, A11Y + "Accessible", "GetInterfaces", None, ()))
        replies = self.bus.call_many(calls)
        nodes: list[SpiNode | None] = []
        for k, handle in enumerate(handles):
            role, name, states, *rest = replies[k * per : (k + 1) * per]
            if isinstance(role, BusCallError) or isinstance(name, BusCallError) or isinstance(states, BusCallError):
                nodes.append(None)
                continue
            node = SpiNode(
                handle=handle, role=role_name(int(role[0])), name=str(name[0][1]), states=decode_states(states[0])
            )
            if not light:
                node.interfaces = frozenset() if isinstance(rest[0], BusCallError) else short_interfaces(rest[0][0])
            nodes.append(node)
        return nodes

    def walk(
        self,
        handle: SpiHandle,
        *,
        max_nodes: int = WALK_MAX_NODES,
        max_depth: int = WALK_MAX_DEPTH,
        deadline_s: float = WALK_DEADLINE_S,
        include_hidden: bool = False,
        light: bool = False,
    ) -> tuple[SpiNode, bool]:
        """Depth-first read with node, depth and time budgets; hidden subtrees are skipped unless asked. The next
        ``WAVE`` nodes on the stack are expanded together (one batch for their children lists, one for the children),
        so deep, narrow trees (web pages) are not read one round trip per level. ``light`` leaves out interfaces
        (enough for change fingerprints)."""
        root = self._full_node(handle)
        deadline = time.monotonic() + deadline_s
        count, truncated = 1, False
        stack: list[tuple[SpiNode, int]] = [(root, 0)]
        while stack and not truncated:
            wave: list[tuple[SpiNode, int]] = []
            while stack and len(wave) < WAVE:
                node, depth = stack.pop()
                if depth < max_depth and (include_hidden or node.showing or depth == 0):
                    wave.append((node, depth))
            if not wave:
                break
            replies = self.bus.call_many([(*n.handle, A11Y + "Accessible", "GetChildren", None, ()) for n, _ in wave])
            wanted: list[tuple[SpiNode, int, SpiHandle]] = []
            for (node, depth), reply in zip(wave, replies, strict=True):
                if isinstance(reply, BusCallError):
                    continue
                children = reply[0]
                if len(children) > BIG_CONTAINER and not include_hidden:
                    children = self._on_screen(children)
                room = max_nodes - count - len(wanted)
                if len(children) > room or time.monotonic() > deadline:
                    truncated = True
                    children = children[: max(room, 0)] if time.monotonic() <= deadline else []
                wanted += [(node, depth, (str(b), str(p))) for b, p in children]
                if truncated:
                    break
            fetched = self._full_nodes([h for _, _, h in wanted], light=light)
            added: list[tuple[SpiNode, int]] = []
            for (parent, depth, _), child in zip(wanted, fetched, strict=True):
                if child is not None:
                    parent.children.append(child)
                    added.append((child, depth + 1))
            count += len(added)
            # pushed in order, so the last child is read first, as before: content areas usually come after
            # toolbars, so a big window that runs out of budget still keeps them
            stack.extend(added)
        return root, truncated

    def details(self, root: SpiNode) -> bool:
        """Frames, actions, text, values and ids for showing nodes of useful roles (bounded, batched); True if
        cut short."""
        deadline = time.monotonic() + DETAIL_DEADLINE_S
        chosen: list[SpiNode] = []
        cut = False
        stack = [root]
        while stack:
            node = stack.pop()
            if node is not root and not node.showing:
                continue
            stack.extend(reversed(node.children))
            if node.role not in DETAIL_ROLES and not node.interfaces & {"Action", "Text", "Value"}:
                continue
            if len(chosen) >= DETAIL_MAX_NODES or time.monotonic() > deadline:
                cut = True
                break
            chosen.append(node)
        self._fill_many(chosen)
        return cut

    def _fill_many(self, nodes: list[SpiNode]) -> None:
        """Two batches: every frame/action count/value/text/id at once, then the action names."""
        calls: list[Call] = []
        wants: list[tuple[SpiNode, str]] = []
        for node in nodes:
            b, p = node.handle
            ifaces = node.interfaces
            if "Component" in ifaces or not ifaces:
                calls.append((b, p, A11Y + "Component", "GetExtents", "u", (SCREEN,)))
                wants.append((node, "frame"))
            if "Action" in ifaces:
                calls.append((b, p, PROPS, "Get", "ss", (A11Y + "Action", "NActions")))
                wants.append((node, "nactions"))
            if "Value" in ifaces:
                calls.append((b, p, PROPS, "Get", "ss", (A11Y + "Value", "CurrentValue")))
                wants.append((node, "value"))
            if "Text" in ifaces and node.role != "password-text" and (node.role in _TEXT_ROLES or not node.name):
                calls.append((b, p, A11Y + "Text", "GetText", "ii", (0, TEXT_PREVIEW)))
                wants.append((node, "text"))
            if b not in self._no_id:
                calls.append((b, p, PROPS, "Get", "ss", (A11Y + "Accessible", "AccessibleId")))
                wants.append((node, "id"))
        counts: list[tuple[SpiNode, int]] = []
        for (node, what), reply in zip(wants, self.bus.call_many(calls), strict=True):
            if isinstance(reply, BusCallError):
                if what == "id" and reply.unsupported:
                    self._no_id.add(node.handle[0])
                continue
            try:
                if what == "frame":
                    x, y, w, h = reply[0]
                    node.frame = Rect(float(x), float(y), float(w), float(h))
                elif what == "nactions":
                    counts.append((node, min(max(int(reply[0][1]), 0), MAX_ACTIONS)))
                elif what == "value":
                    node.value = float(reply[0][1])
                elif what == "text":
                    node.text = str(reply[0])
                else:
                    node.identifier = str(reply[0][1]) or None
            except (TypeError, ValueError, IndexError) as exc:
                log.debug("details of %s (%s): %s", node.handle, what, exc)
        # names one by one per index: GetActions aborts apps whose actions have an empty field
        name_calls = [(*n.handle, A11Y + "Action", "GetName", "i", (i,)) for n, c in counts for i in range(c)]
        names = iter(self.bus.call_many(name_calls))
        for node, count in counts:
            got = [next(names) for _ in range(count)]
            node.actions = tuple(str(r[0]).casefold() for r in got if not isinstance(r, BusCallError))

    def fingerprint(self, root: SpiNode) -> int:
        """Changes when names, roles, states or structure change."""
        return hash(tuple((n.handle, n.name, n.role, n.states, len(n.children)) for n in root.walk()))
