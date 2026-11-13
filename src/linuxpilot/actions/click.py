"""Clicking an element: accessibility action → selection → keyboard → pointer, recording every attempt."""

from __future__ import annotations

from dataclasses import dataclass, field

from linuxpilot.engine.base import Button, Engine
from linuxpilot.errors import ErrorCode, LinuxPilotError
from linuxpilot.perception.model import Element

CLICK_ACTIONS = ("click", "press", "activate", "jump", "toggle", "open", "select")
DOUBLE_ACTIONS = ("activate", "open", "edit")
CONTEXT_ACTIONS = ("menu", "showmenu", "popup", "context menu")
ROW_ROLES = frozenset({"AXRow", "AXCell", "AXMenuItem", "AXRadioButton"})
SPACE_ROLES = frozenset({"AXButton", "AXCheckBox", "AXRadioButton"})


@dataclass(slots=True)
class Attempts:
    """What a ladder tried, for ``ELEMENT_NOT_ACTIONABLE`` details and the result line."""

    tried: list[str] = field(default_factory=list)

    def failed(self, el: Element, what: str = "click") -> LinuxPilotError:
        return LinuxPilotError(
            ErrorCode.ELEMENT_NOT_ACTIONABLE,
            f"Could not {what} {el.ref} {el.role} {el.name or ''}".rstrip() + ".",
            hint="Try a menu (linux_menu), a keyboard shortcut (linux_keys), linux_set_value or linux_select.",
            details={"attempts": self.tried},
        )


def _first_action(engine: Engine, el: Element, preferred: tuple[str, ...], attempts: Attempts) -> str | None:
    available = engine.actions(el.handle)
    for name in sorted(available, key=lambda a: preferred.index(a) if a in preferred else len(preferred)):
        if name not in preferred:
            break
        attempts.tried.append(f"action:{name}")
        if engine.do_action(el.handle, name):
            return f"action:{name}"
    return None


def _pointer(
    engine: Engine, el: Element, *, trusted: bool, button: Button, count: int, attempts: Attempts
) -> str | None:
    if not trusted or not engine.has_pointer or el.center is None:
        return None
    attempts.tried.append(f"pointer:{button}x{count}")
    engine.click(el.center.x, el.center.y, button=button, count=count)
    return "pointer"


def app_has_focus(engine: Engine, el: Element) -> bool:
    """Keys reach the element's app: its window is the active one (keys never go to another app)."""
    active = engine.active_window()
    handle = active.handle if active is not None else None
    return isinstance(handle, tuple) and isinstance(el.handle, tuple) and handle[0] == el.handle[0]


def _keyboard(engine: Engine, el: Element, key: str, attempts: Attempts) -> str | None:
    if not engine.has_keyboard:
        return None
    attempts.tried.append(f"focus+{key}")
    if not engine.focus(el.handle) or not app_has_focus(engine, el):
        return None
    engine.press_keys(key)
    return f"keys:{key}"


def click_element(
    engine: Engine, el: Element, *, button: Button = "left", count: int = 1, coords_trusted: bool = True
) -> str:
    """Click ``el`` the most robust way available; returns the strategy that worked."""
    attempts = Attempts()
    if button == "left" and count == 1:
        done = _first_action(engine, el, CLICK_ACTIONS, attempts)
        if done is None and el.ax_role in ROW_ROLES:
            attempts.tried.append("select")
            done = "select" if engine.select_in_parent(el.handle) else None
        done = done or _pointer(engine, el, trusted=coords_trusted, button=button, count=1, attempts=attempts)
        done = done or _keyboard(engine, el, "space" if el.ax_role in SPACE_ROLES else "Return", attempts)
    elif button == "left":
        done = _first_action(engine, el, DOUBLE_ACTIONS, attempts)
        done = done or _pointer(engine, el, trusted=coords_trusted, button=button, count=count, attempts=attempts)
        done = done or _keyboard(engine, el, "Return", attempts)
    elif button == "right":
        done = _first_action(engine, el, CONTEXT_ACTIONS, attempts)
        done = done or _pointer(engine, el, trusted=coords_trusted, button=button, count=count, attempts=attempts)
        done = done or _keyboard(engine, el, "shift+F10", attempts)
    else:
        done = _pointer(engine, el, trusted=coords_trusted, button=button, count=count, attempts=attempts)
    if done is None:
        raise attempts.failed(el)
    return done
