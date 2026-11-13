"""Matching what a person calls an app to installed entries and running apps."""

from __future__ import annotations

import pytest

from linuxpilot.engine.base import InstalledApp
from linuxpilot.errors import ErrorCode, LinuxPilotError
from linuxpilot.models import AppInfo, Rect, WindowInfo
from linuxpilot.session import appmatch

GEDIT = InstalledApp(
    "org.gnome.gedit.desktop", "Text Editor", "gedit %U", keywords=("Text", "Editor", "Plaintext", "gedit")
)
MONITOR = InstalledApp("gnome-system-monitor.desktop", "System Monitor", "gnome-system-monitor")
POWER = InstalledApp("org.gnome.PowerStats.desktop", "Power Monitor", "gnome-power-statistics")
CALC = InstalledApp("org.gnome.Calculator.desktop", "Calculator", "gnome-calculator", keywords=("arithmetic",))
KATE = InstalledApp("org.kde.kate.desktop", "Kate", "kate -b %U", generic_name="Text Editor")
FIREFOX = InstalledApp("firefox_firefox.desktop", "Firefox", "/snap/bin/firefox %u", generic_name="Web Browser")
CHROME = InstalledApp("google-chrome.desktop", "Google Chrome", "/usr/bin/google-chrome-stable %U",
                      generic_name="Web Browser", wm_class="Google-chrome")  # fmt: skip
ALL = [GEDIT, MONITOR, POWER, CALC, KATE, FIREFOX, CHROME]


def test_query_forms_derive_words_from_desktop_ids():
    assert appmatch.query_forms("org.gnome.TextEditor.desktop") == [
        ("org.gnome.texteditor", 0),
        ("texteditor", 1),
        ("text editor", 1),
    ]
    assert appmatch.query_forms("/usr/bin/gedit") == [("/usr/bin/gedit", 0), ("gedit", 1)]
    assert appmatch.query_forms("Text Editor") == [("text editor", 0)]
    assert appmatch.query_forms("  ") == []


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Text Editor", GEDIT),  # name beats Kate's generic name
        ("text editor", GEDIT),
        ("org.gnome.TextEditor", GEDIT),  # a guessed desktop id that is not installed
        ("org.gnome.SystemMonitor", MONITOR),
        ("gnome-system-monitor.desktop", MONITOR),
        ("gedit", GEDIT),  # program
        ("Calculater", CALC),  # typo
        ("calculator app", CALC),
        ("arithmetic", CALC),  # keyword
        ("google-chrome", CHROME),
        ("Google-chrome", CHROME),  # window class
        ("firefox", FIREFOX),  # snap program
        ("kate", KATE),
    ],
)
def test_resolve(query, expected):
    assert appmatch.resolve(query, ALL) == expected


def test_ambiguous_and_not_found_offer_choices():
    with pytest.raises(LinuxPilotError) as err:
        appmatch.resolve("monitor", ALL)
    assert err.value.code is ErrorCode.INVALID_ARGUMENT
    assert "System Monitor" in err.value.message and "Power Monitor" in err.value.message
    with pytest.raises(LinuxPilotError) as err:
        appmatch.resolve("browser", ALL)
    assert "Firefox" in err.value.message and "Google Chrome" in err.value.message
    with pytest.raises(LinuxPilotError) as err:
        appmatch.resolve("Calcu", ALL)
    assert err.value.code is ErrorCode.APP_NOT_FOUND and "Calculator" in (err.value.hint or "")
    with pytest.raises(LinuxPilotError) as err:
        appmatch.resolve("photoshop", ALL)
    assert err.value.code is ErrorCode.APP_NOT_FOUND and "installed" in (err.value.hint or "")


def test_search_keeps_substrings_and_matches():
    assert appmatch.search(None, ALL) == ALL
    assert appmatch.search("calc", ALL) == [CALC]
    assert [a.name for a in appmatch.search("editor", ALL)][:2] == ["Kate", "Text Editor"]


def test_running_apps_and_windows_of_an_entry():
    apps = [AppInfo("gedit", 10), AppInfo("x", 11, bundle_id="org.gnome.Calculator"), AppInfo("other", 12)]
    assert [a.pid for a in appmatch.running_of([GEDIT, CALC], apps)] == [10, 11]
    assert appmatch.running_of([], apps) == []
    frame = Rect(0, 0, 10, 10)
    windows = [
        WindowInfo(1, 10, "gedit", "a", frame),
        WindowInfo(2, 10, "gedit", "b", frame, focused=True),
        WindowInfo(3, 12, "other", "c", frame),
    ]
    assert [w.window_id for w in appmatch.windows_of(GEDIT, windows)] == [2, 1]
