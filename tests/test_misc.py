"""Config loading, CLI, report helpers, evidence paths, settling, the engine runner and action edge cases."""

from __future__ import annotations

import time
from pathlib import Path

import anyio
import pytest

from linuxpilot.actions.choose import match_option, options_of
from linuxpilot.actions.menu import norm
from linuxpilot.actions.value import parse_bool, parse_number
from linuxpilot.cli import main
from linuxpilot.config import Config, load_config
from linuxpilot.errors import ErrorCode, LinuxPilotError
from linuxpilot.models import RawNode
from linuxpilot.session.evidence import Evidence
from linuxpilot.session.report import cap
from linuxpilot.session.runner import EngineRunner
from linuxpilot.session.settle import Settler
from support import demo


class TestConfig:
    def test_precedence(self, tmp_path):
        toml = tmp_path / "c.toml"
        toml.write_text('[tools]\nprofile = "minimal"\n[safety]\nmax_actions_per_minute = 30\ndeny_apps = ["slack"]\n')
        cfg = load_config(
            {"max_actions_per_minute": 10},
            config_path=toml,
            environ={"LINUXPILOT_PROFILE": "full", "LINUXPILOT_ALLOW_UNSAFE": "yes", "OTHER": "x"},
        )
        assert (cfg.profile, cfg.max_actions_per_minute, cfg.deny_apps, cfg.allow_unsafe) == (
            "full",
            10,
            ("slack",),
            True,
        )
        assert load_config(config_path=tmp_path / "missing.toml", environ={}) == Config(
            screenshots_dir=load_config(config_path=tmp_path / "x", environ={}).screenshots_dir
        )

    @pytest.mark.parametrize(
        "overrides",
        [
            {"bogus": 1},
            {"max_actions_per_minute": "0"},
            {"engine": "mac"},
            {"confirm_fallback": "maybe"},
            {"max_actions_per_minute": "x"},
        ],
    )
    def test_rejects_bad_settings(self, tmp_path, overrides):
        with pytest.raises(LinuxPilotError) as err:
            load_config(overrides, config_path=tmp_path / "none", environ={})
        assert err.value.code is ErrorCode.INVALID_ARGUMENT

    def test_bad_toml(self, tmp_path):
        bad = tmp_path / "c.toml"
        bad.write_text("this is = = not toml")
        with pytest.raises(LinuxPilotError):
            load_config(config_path=bad, environ={})


class TestCli:
    def test_fake_tools_call_pause(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("LINUXPILOT_STATE_DIR", str(tmp_path))
        monkeypatch.setenv("LINUXPILOT_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("LINUXPILOT_SCREENSHOTS_DIR", str(tmp_path / "shots"))
        assert main(["--engine", "fake", "tools"]) == 0
        assert "16 tools" in capsys.readouterr().out
        assert main(["--engine", "fake", "call", "linux_snapshot"]) == 0
        assert 'window "notes.txt - gedit"' in capsys.readouterr().out
        assert main(["--engine", "fake", "call", "linux_click", '{"ref": "e99"}']) == 1
        assert main(["--engine", "fake", "call", "linux_snapshot", "{not json"]) == 2
        assert main(["pause"]) == 0 and (tmp_path / "paused").exists()
        assert main(["resume"]) == 0 and not (tmp_path / "paused").exists()
        assert main(["--profile", "minimal", "--engine", "fake", "tools", "--json"]) == 0

    def test_bad_config_exit_code(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("LINUXPILOT_CONFIG_DIR", str(tmp_path))
        (tmp_path / "config.toml").write_text("typo_setting = 1\n")
        assert main(["--engine", "fake", "tools"]) == 2
        assert "Unknown setting" in capsys.readouterr().err


class TestHelpers:
    def test_cap(self):
        assert cap("short") == "short"
        long = "\n".join(f"line {i}" for i in range(1000))
        out = cap(long, 200)
        assert len(out) <= 200 and out.endswith("shows the rest)")

    def test_evidence_paths(self, tmp_path):
        ev = Evidence(tmp_path)
        assert ev.path_for(None, "click").name == "step-01-click.png"
        assert ev.path_for("sub/x.png", "a") == (tmp_path / "sub" / "x.png").resolve()
        for bad in ("../x.png", "/etc/x.png", "x.jpg.exe", "~/x.png"):
            with pytest.raises(LinuxPilotError):
                ev.path_for(bad, "a")
        with pytest.raises(LinuxPilotError):
            ev.to_screen(1, 1, None)

    def test_settler(self):
        now = [0.0]
        values = iter([1, 2, 2])
        s = Settler(first_look_s=0, poll_s=0.1, sleep=lambda d: now.__setitem__(0, now[0] + d), clock=lambda: now[0])
        assert s.settle(lambda: next(values), 1.0)
        changing = iter(range(100))
        assert not s.settle(lambda: next(changing), 0.5)
        assert s.cap("launch") == 10.0 and s.cap("anything") == 3.0

    def test_parsers(self):
        assert parse_bool("On") and not parse_bool("0")
        assert parse_number("1.5") == 1.5
        for bad in ("nan", "inf", "x"):
            with pytest.raises(LinuxPilotError):
                parse_number(bad)
        assert norm("_Save As...") == "save as" == norm("Save As…")

    def test_option_matching(self):
        opts = [RawNode(role="AXMenuItem", title=t) for t in ("Large", "Larger", "Small", "Small")]
        assert match_option(opts, "large", None).title == "Large"
        assert match_option(opts, "sm", None).title == "Small"
        assert match_option(opts, None, 3).title == "Small"
        with pytest.raises(LinuxPilotError):
            match_option(opts, "lar", None)  # ambiguous prefix
        with pytest.raises(LinuxPilotError):
            match_option(opts, None, None)
        container = RawNode(
            role="AXList",
            children=[
                RawNode(role="AXRow", title="A", children=[RawNode(role="AXStaticText", title="A")]),
                RawNode(role="AXGroup", children=[RawNode(role="AXRow", title="B")]),
            ],
        )
        assert [o.title for o in options_of(container)] == ["A", "B"]


class TestRunner:
    def test_lazy_creation_and_timeout(self):
        made = []

        def factory():
            made.append(1)
            return demo()

        runner = EngineRunner(factory, timeout_s=0.2)
        assert not runner.started and made == []

        async def scenario():
            assert await runner.run(lambda: runner.engine.list_apps()[0].name) == "gedit"
            with pytest.raises(LinuxPilotError) as err:
                await runner.run(time.sleep, 1.0)
            assert err.value.code is ErrorCode.TIMEOUT and not runner.started

        anyio.run(scenario)
        runner.close()
        assert len(made) == 1

    def test_factory_errors_propagate(self):
        def factory():
            raise LinuxPilotError(ErrorCode.NO_DESKTOP, "none")

        runner = EngineRunner(factory)

        async def scenario():
            with pytest.raises(LinuxPilotError) as err:
                await runner.run(lambda: runner.engine)
            assert err.value.code is ErrorCode.NO_DESKTOP

        anyio.run(scenario)


def test_no_desktop_imports_outside_engine():
    """Only engine/linux may talk to D-Bus or X11."""
    root = Path(__file__).parents[1] / "src" / "linuxpilot"
    for path in root.rglob("*.py"):
        if "engine/linux" in path.as_posix() or path.name == "cli.py":
            continue
        text = path.read_text()
        assert "import jeepney" not in text and "from jeepney" not in text and "Xlib" not in text, path


def test_no_other_project_names_in_source_or_docs():
    import hashlib
    import re

    banned = {  # sha256 of the lower-cased project names the code must never mention
        "33a02e0afd457f4f513caf167a0cacf037a2ed30fd3b8e8d98d0c821d27a7b8e",
        "adbb09b8e09ad27d64e80869024124972c3f68c2e0211628138f884cd038848b",
    }
    root = Path(__file__).parents[1]
    files = [*root.joinpath("src").rglob("*.py"), *root.joinpath("docs").rglob("*.md"), root / "README.md"]
    for path in files:
        words = set(re.findall(r"[a-z][a-z-]{4,}", path.read_text().casefold()))
        assert not {hashlib.sha256(w.encode()).hexdigest() for w in words} & banned, path


def test_text_changes_reports_display_updates():
    from linuxpilot.models import AppInfo, RawTree, Rect, WindowInfo
    from linuxpilot.session.report import text_changes

    def tree(display: str, secret: str = "x") -> RawTree:
        root = RawNode(
            role="AXWindow",
            children=[
                RawNode(role="AXUnknown", value=display),
                RawNode(role="AXStaticText", title="Label"),
                RawNode(role="AXTextField", value=secret, secure=True),
            ],
        )
        return RawTree(app=AppInfo("calc", 1), window=WindowInfo(1, 1, "calc", "w", Rect(0, 0, 1, 1)), root=root)

    assert text_changes(tree("2"), tree("2+3")) == ["2+3"]
    assert text_changes(tree("2"), tree("2", secret="changed")) == []  # secure text never reported
    assert text_changes(None, tree("2")) == []


def test_submit_prefers_activate_over_keyboard():
    from linuxpilot.actions.text import submit
    from linuxpilot.engine.fake import FakeEngine
    from support import app, node, screen, window

    engine = FakeEngine(
        {
            "s": screen(
                app(
                    "a",
                    [window("W", [node("entry", "Path", frame=[0, 0, 100, 20], actions=["activate"], goto="done")])],
                )
            ),
            "done": screen(),
        },
        "s",
        keyboard=False,
    )
    el = type("E", (), {"handle": ("fake.a", "/0/0")})()
    assert submit(engine, el) == "action:activate" and engine.current == "done"
