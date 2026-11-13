"""What a person calls an app (``Text Editor``, ``org.gnome.TextEditor``, ``gedit``) → installed and running apps.

Every installed entry is scored against the query and the forms derived from it (a desktop id's last
segment, its words split at case changes and separators). Scores come from the entries' own fields
(name, generic name, keywords, desktop id, program, window class): nothing here is app-specific.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from difflib import SequenceMatcher

from linuxpilot.engine.base import InstalledApp
from linuxpilot.errors import ErrorCode, LinuxPilotError
from linuxpilot.models import AppInfo, WindowInfo

EXACT = 100
"""The query is the app's name, desktop id, id stem, program or window class."""
GENERIC_EXACT = 94
ALL_WORDS = 92
"""Every query word is a word of the label (``editor`` → ``Text Editor``)."""
LABEL_IN_QUERY = 88
"""The label's words plus at most one more (``calculator app`` → ``Calculator``)."""
KEYWORD = 86
MATCH_MIN = 86
"""Lowest score that counts as a match; below it only suggestions are offered (typos score by similarity)."""
SUGGEST_MIN = 55
SUGGESTIONS = 5
DERIVED_PENALTY = 1
"""A derived form ranks just below the query as typed, so an exact hit on the original wins ties."""
_CASE_CHANGE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SEPARATORS = re.compile(r"[-_.\s]+")


def program_of(exec_line: str) -> str:
    """The program name of a desktop Exec line (``env X=1 /usr/bin/gedit %U`` → ``gedit``)."""
    parts = [p for p in exec_line.split() if "=" not in p and p != "env" and not p.startswith("%")]
    return parts[0].rsplit("/", 1)[-1].casefold() if parts else ""


def query_forms(query: str) -> list[tuple[str, int]]:
    """The query as typed, then derived forms with their penalty (``org.gnome.TextEditor`` → ``text editor``)."""
    raw = query.strip().removesuffix(".desktop")
    if not raw:
        return []
    tail = raw.rsplit("/", 1)[-1] if "/" in raw else raw if " " in raw else raw.rsplit(".", 1)[-1]
    forms = [(raw.casefold(), 0)]
    for form in (tail.casefold(), _SEPARATORS.sub(" ", _CASE_CHANGE.sub(" ", tail)).strip().casefold()):
        if form and all(form != f for f, _ in forms):
            forms.append((form, DERIVED_PENALTY))
    return forms


def entry_keys(app: InstalledApp) -> set[str]:
    """Identifiers a running process of the entry can carry: desktop id, its stem, program, window class."""
    stem = app.desktop_id.casefold().removesuffix(".desktop")
    return {k for k in (stem, stem.rsplit(".", 1)[-1], program_of(app.exec_line), app.wm_class.casefold()) if k}


def _label_score(form: str, label: str, exact: int) -> int:
    label = label.strip().casefold()
    if not label:
        return 0
    if label == form:
        return exact
    form_words, label_words = form.split(), label.split()
    if all(w in label_words for w in form_words):
        return ALL_WORDS
    if all(w in form_words for w in label_words) and len(form_words) - len(label_words) <= 1:
        return LABEL_IN_QUERY
    return round(SequenceMatcher(None, form, label).ratio() * 100)


def score(query: str, app: InstalledApp) -> int:
    """0–100: how well ``query`` names ``app``."""
    keys = entry_keys(app)
    keywords = {k.casefold() for k in app.keywords}
    best = 0
    for form, penalty in query_forms(query):
        value = max(
            EXACT if form in keys else 0,
            KEYWORD if form in keywords else 0,
            _label_score(form, app.name, EXACT),
            _label_score(form, app.generic_name, GENERIC_EXACT),
        )
        best = max(best, value - penalty if value else 0)
    return best


def rank(query: str, apps: Iterable[InstalledApp]) -> list[tuple[int, InstalledApp]]:
    """Entries scoring above zero, best first (ties by name, then desktop id)."""
    scored = [(score(query, a), a) for a in apps]
    return sorted((s for s in scored if s[0] > 0), key=lambda s: (-s[0], s[1].name.casefold(), s[1].desktop_id))


def best_matches(query: str, apps: Iterable[InstalledApp]) -> list[InstalledApp]:
    """The entries sharing the top score, when it counts as a match."""
    ranked = rank(query, apps)
    if not ranked or ranked[0][0] < MATCH_MIN:
        return []
    return [a for s, a in ranked if s == ranked[0][0]]


def search(query: str | None, apps: Sequence[InstalledApp]) -> list[InstalledApp]:
    """Entries for an ``installed`` listing: all without a query, else matches and near matches, best first."""
    if not (query and query.strip()):
        return list(apps)
    q = query.strip().casefold()
    ranked = rank(query, apps)
    return [a for s, a in ranked if s >= MATCH_MIN or q in f"{a.name} {a.desktop_id}".casefold()]


def describe(apps: Iterable[InstalledApp]) -> str:
    return ", ".join(f"{a.name} ({a.desktop_id})" for a in apps)


def resolve(query: str, apps: Sequence[InstalledApp]) -> InstalledApp:
    """The one installed entry ``query`` names, else ``APP_NOT_FOUND`` / ``INVALID_ARGUMENT`` with suggestions."""
    ranked = rank(query, apps)
    top = ranked[0][0] if ranked else 0
    if top < MATCH_MIN:
        close = [a for s, a in ranked if s >= SUGGEST_MIN][:SUGGESTIONS]
        raise LinuxPilotError(
            ErrorCode.APP_NOT_FOUND,
            f"No installed app matches {query!r}.",
            hint=f"Closest: {describe(close)}. Launch one by its exact name."
            if close
            else 'See linux_app(action="installed", app="<part of the name>").',
        )
    best = [a for s, a in ranked if s == top]
    if len(best) > 1 and top < EXACT:
        raise LinuxPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"{query!r} matches several apps: {describe(best[:8])}.",
            hint="Pass the exact name or desktop id.",
        )
    return best[0]


def _owned(name: str, bundle_id: str | None, keys: set[str]) -> bool:
    bundle = (bundle_id or "").casefold().removesuffix(".desktop")
    return name.casefold() in keys or bool(bundle and {bundle, bundle.rsplit(".", 1)[-1]} & keys)


def running_of(entries: Iterable[InstalledApp], apps: Sequence[AppInfo]) -> list[AppInfo]:
    """Running apps that belong to the entries (AT-SPI name or desktop id equal to one of their keys)."""
    keys: set[str] = set().union(*(entry_keys(e) for e in entries))
    return [a for a in apps if _owned(a.name, a.bundle_id, keys)] if keys else []


def windows_of(entry: InstalledApp, windows: Sequence[WindowInfo]) -> list[WindowInfo]:
    """Open windows of the entry's app, the focused one first."""
    keys = entry_keys(entry)
    own = [w for w in windows if _owned(w.app, w.bundle_id, keys)]
    return sorted(own, key=lambda w: not w.focused)
