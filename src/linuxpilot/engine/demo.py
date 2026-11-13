"""The bundled demo desktop for ``--engine fake`` and tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from linuxpilot.engine.base import InstalledApp
from linuxpilot.engine.fake import FakeEngine

DEMO_PATH = Path(__file__).parent / "fixtures" / "demo_desktop.json"


def load_desktop(path: str | Path | None = None) -> dict[str, Any]:
    return json.loads(Path(path or DEMO_PATH).read_text())


def demo_engine(path: str | Path | None = None, **kwargs: Any) -> FakeEngine:
    """A :class:`FakeEngine` built from a desktop file (``start``, ``screens``, ``keys``, ``installed``,
    ``launches``, ``chromium``)."""
    data = load_desktop(path)
    keys = {tuple(k.split("|", 1)): v for k, v in data.get("keys", {}).items()}
    installed = [InstalledApp(**{**a, "keywords": tuple(a.get("keywords", ()))}) for a in data.get("installed", [])]
    return FakeEngine(
        data["screens"],
        kwargs.pop("start", data.get("start", next(iter(data["screens"])))),
        keys=keys,
        installed=installed,
        launches=data.get("launches", {}),
        chromium=data.get("chromium", ()),
        **kwargs,
    )
