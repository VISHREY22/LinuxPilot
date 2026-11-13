"""Spike S1: how fast can we read accessibility trees on this Linux desktop?

Compares, per running app:
  (a) org.a11y.atspi.Cache.GetItems over raw D-Bus with jeepney (one call per app), and
  (b) Component.GetExtents for the first 150 nodes (what a snapshot needs for kept elements).
Also prints toolkit names and window frames, which tell whether coordinates are trustworthy
(native Wayland apps report window-relative frames at 0,0).

Run:  uv run --with jeepney python spikes/atspi_dbus_spike.py | tee spikes/atspi_dbus_spike.out
Read-only: it never clicks, types or changes anything.
"""

from __future__ import annotations

import time

from jeepney import DBusAddress, new_method_call
from jeepney.io.blocking import open_dbus_connection

ROOT = "/org/a11y/atspi/accessible/root"
EXTENTS_SAMPLE = 150


def call(conn, bus: str, path: str, iface: str, method: str, sig: str | None = None, body=(), timeout=5.0):
    msg = new_method_call(DBusAddress(path, bus_name=bus, interface=iface), method, sig, body)
    return conn.send_and_get_reply(msg, timeout=timeout).body


def prop(conn, bus: str, path: str, iface: str, name: str):
    return call(conn, bus, path, "org.freedesktop.DBus.Properties", "Get", "ss", (iface, name))[0][1]


def main() -> None:
    session = open_dbus_connection(bus="SESSION")
    address = call(session, "org.a11y.Bus", "/org/a11y/bus", "org.a11y.Bus", "GetAddress")[0]
    enabled = prop(session, "org.a11y.Bus", "/org/a11y/bus", "org.a11y.Status", "IsEnabled")
    a11y = open_dbus_connection(bus=address)
    print(f"a11y bus {address.split(',')[0]}  IsEnabled={enabled}")

    t = time.perf_counter()
    apps = call(a11y, "org.a11y.atspi.Registry", ROOT, "org.a11y.atspi.Accessible", "GetChildren")[0]
    print(f"registry: {len(apps)} apps in {(time.perf_counter() - t) * 1000:.0f} ms\n")
    print(f"{'app':26} {'toolkit':10} {'nodes':>6} {'GetItems':>9} {'extents x150':>13}  windows")
    for bus, path in apps:
        try:
            name = prop(a11y, bus, path, "org.a11y.atspi.Accessible", "Name")
            toolkit = prop(a11y, bus, path, "org.a11y.atspi.Application", "ToolkitName")
            t = time.perf_counter()
            items = call(a11y, bus, "/org/a11y/atspi/cache", "org.a11y.atspi.Cache", "GetItems")[0]
            get_items_ms = (time.perf_counter() - t) * 1000
        except Exception as exc:
            print(f"{bus:26} error: {str(exc)[:80]}")
            continue
        t = time.perf_counter()
        for item in items[:EXTENTS_SAMPLE]:
            try:
                call(a11y, bus, item[0][1], "org.a11y.atspi.Component", "GetExtents", "u", (0,), timeout=2)
            except Exception:
                continue
        extents_ms = (time.perf_counter() - t) * 1000
        windows = []
        for wbus, wpath in call(a11y, bus, path, "org.a11y.atspi.Accessible", "GetChildren")[0][:3]:
            try:
                x, y, w, h = call(a11y, wbus, wpath, "org.a11y.atspi.Component", "GetExtents", "u", (0,))[0]
                windows.append(f"({x},{y} {w}x{h})")
            except Exception:
                continue
        print(
            f"{name[:26]:26} {toolkit[:10]:10} {len(items):6} {get_items_ms:7.0f}ms {extents_ms:11.0f}ms  "
            + " ".join(windows)
        )


if __name__ == "__main__":
    main()
