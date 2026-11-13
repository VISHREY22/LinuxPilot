"""Screenshots as evidence: confined to one folder, numbered by step, with ids for pixel coordinates."""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

from linuxpilot.engine.base import Engine, Shot
from linuxpilot.errors import ErrorCode, LinuxPilotError
from linuxpilot.models import Rect
from linuxpilot.perception.model import Snapshot

SHOT_SIDE = 1280
SHOTS_KEPT = 32
_SUFFIXES = (".png",)


class Evidence:
    """Writes screenshots, remembers each one's scale and origin, draws refs on request."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.step = 0
        self.shots: OrderedDict[str, Shot] = OrderedDict()

    def path_for(self, name: str | None, action: str) -> Path:
        """A step-numbered path, or ``name`` resolved inside the evidence folder (never outside it)."""
        if not name:
            self.step += 1
            return self.root / f"step-{self.step:02d}-{action}.png"
        root = self.root.resolve()
        target = (root / Path(name).expanduser()).resolve()
        if (root != target and root not in target.parents) or target.suffix.lower() not in _SUFFIXES:
            raise LinuxPilotError(
                ErrorCode.INVALID_ARGUMENT,
                f"Screenshots go inside {root} as .png files, not {name!r}.",
                hint='Pass a file name such as "login-error.png", or no path.',
            )
        return target

    def shoot(
        self,
        engine: Engine,
        path: Path,
        *,
        rect: Rect | None = None,
        snap: Snapshot | None = None,
        annotate: bool = False,
    ) -> tuple[str, str]:
        """Save a screenshot; returns ``(screenshot_id, summary line)``."""
        if not engine.can_capture:
            raise LinuxPilotError(ErrorCode.PERMISSION_CAPTURE, "Screenshots are not available in this session.")
        shot = engine.screenshot(str(path), rect=rect, max_side=SHOT_SIDE)
        note = self._annotate(path, shot, snap) if annotate and snap is not None else ""
        shot_id = f"shot_{len(self.shots) + 1}"
        self.shots[shot_id] = shot
        while len(self.shots) > SHOTS_KEPT:
            self.shots.popitem(last=False)
        return shot_id, f"screenshot {path} ({shot.width}×{shot.height}, id {shot_id}, scale {shot.scale:.3g}){note}"

    def to_screen(self, x: float, y: float, shot_id: str | None) -> tuple[float, float]:
        """Image pixels of a screenshot → screen coordinates."""
        shot = self.shots.get(shot_id or "")
        if shot is None:
            raise LinuxPilotError(
                ErrorCode.INVALID_ARGUMENT,
                f"Unknown screenshot_id {shot_id!r}.",
                hint="Use the id a linux_screenshot result printed (shot_…), from this session.",
            )
        if not (0 <= x <= shot.width and 0 <= y <= shot.height):
            raise LinuxPilotError(
                ErrorCode.INVALID_ARGUMENT, f"({x:g},{y:g}) is outside the {shot.width}×{shot.height} image."
            )
        return shot.origin_x + x / shot.scale, shot.origin_y + y / shot.scale

    @staticmethod
    def _annotate(path: Path, shot: Shot, snap: Snapshot) -> str:
        if snap.window is not None and not snap.window.coords_trusted:
            return " — refs not drawn: this window does not report screen positions (Wayland)"
        from PIL import Image, ImageDraw

        with Image.open(path) as img:
            canvas = img.convert("RGB")
        draw = ImageDraw.Draw(canvas)
        for el in snap.all_elements:
            if not el.interactive or el.frame is None:
                continue
            f = el.frame
            box = [
                (f.x - shot.origin_x) * shot.scale,
                (f.y - shot.origin_y) * shot.scale,
                (f.x + f.w - shot.origin_x) * shot.scale,
                (f.y + f.h - shot.origin_y) * shot.scale,
            ]
            draw.rectangle(box, outline=(255, 0, 140), width=2)
            width = draw.textlength(el.ref)
            draw.rectangle([box[0], box[1], box[0] + width + 6, box[1] + 13], fill=(255, 0, 140))
            draw.text((box[0] + 3, box[1]), el.ref, fill="white")
        canvas.save(path, format="PNG")
        return f" — refs of snapshot {snap.snapshot_id} drawn on it"
