"""`linuxpilot setup`: asks once for what is missing, skips what is ready, reports every step."""

from __future__ import annotations

from linuxpilot.engine.linux import onboarding
from linuxpilot.engine.linux.onboarding import NeedsRelogin, Step


def _steps(log):
    def ran(name, result="ok"):
        def run():
            log.append(name)
            if isinstance(result, Exception):
                raise result
            return result

        return run

    return [
        Step("ready one", "", lambda: True, ran("ready one")),
        Step("missing", "why", lambda: False, ran("missing")),
        Step("needs login", "why", lambda: False, ran("needs login", NeedsRelogin("log in again"))),
        Step("broken", "why", lambda: False, ran("broken", RuntimeError("no"))),
        Step("unsupported", "why", lambda: False, ran("unsupported"), lambda: "not here"),
    ]


def test_setup_asks_once_and_runs_only_missing_steps(monkeypatch, capsys):
    log, asked = [], []
    monkeypatch.setattr(onboarding, "steps", lambda: _steps(log))
    monkeypatch.setattr(onboarding, "ensure_session_env", lambda: None)
    code = onboarding.run_setup(ask=lambda q: asked.append(q) or "")
    assert len(asked) == 1 and log == ["missing", "needs login", "broken"]
    out = capsys.readouterr().out
    assert code == 1 and "Log out and back in once to finish: needs login" in out and "Not set up: broken" in out


def test_setup_with_nothing_missing_asks_nothing(monkeypatch, capsys):
    monkeypatch.setattr(onboarding, "steps", lambda: [Step("ready", "", lambda: True, lambda: "x")])
    monkeypatch.setattr(onboarding, "ensure_session_env", lambda: None)
    assert onboarding.run_setup(ask=lambda q: (_ for _ in ()).throw(AssertionError("asked"))) == 0
    assert "Nothing will ask for permission again" in capsys.readouterr().out


def test_declining_changes_nothing(monkeypatch):
    log = []
    monkeypatch.setattr(onboarding, "steps", lambda: _steps(log))
    monkeypatch.setattr(onboarding, "ensure_session_env", lambda: None)
    assert onboarding.run_setup(ask=lambda q: "n") == 1 and log == []


def test_packaged_setup_data_exists():
    assert onboarding._data("enable-uinput.sh").is_file()
    assert onboarding._data("gnome-extension", onboarding.EXTENSION_UUID, "extension.js").is_file()
