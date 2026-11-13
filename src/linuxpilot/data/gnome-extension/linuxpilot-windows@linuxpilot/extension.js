/* LinuxPilot window helper on the session bus (org.gnome.Shell, /org/gnome/Shell/Extensions/LinuxPilot):
 * Windows() -> JSON geometry; Activate(pid, title) brings a window to the front; Close(pid, title) asks it to close
 * like its title-bar button does (the app still asks about unsaved work). */
const { Gio } = imports.gi;

const IFACE = `<node>
  <interface name="org.gnome.Shell.Extensions.LinuxPilot">
    <method name="Windows"><arg type="s" direction="out" name="json"/></method>
    <method name="Activate">
      <arg type="u" direction="in" name="pid"/><arg type="s" direction="in" name="title"/>
      <arg type="b" direction="out" name="done"/>
    </method>
    <method name="Close">
      <arg type="u" direction="in" name="pid"/><arg type="s" direction="in" name="title"/>
      <arg type="b" direction="out" name="done"/>
    </method>
  </interface>
</node>`;

function windowsOf(pid) {
    return global.get_window_actors().map(actor => actor.meta_window).filter(w => w.get_pid() === pid);
}

function find(pid, title) {
    const own = windowsOf(pid);
    const named = own.filter(w => (w.get_title() || '') === title);
    if (named.length > 0)
        return named[0];
    return own.length === 1 ? own[0] : null;
}

class Extension {
    enable() {
        this._dbus = Gio.DBusExportedObject.wrapJSObject(IFACE, this);
        this._dbus.export(Gio.DBus.session, '/org/gnome/Shell/Extensions/LinuxPilot');
    }

    disable() {
        if (this._dbus) {
            this._dbus.flush();
            this._dbus.unexport();
            this._dbus = null;
        }
    }

    Windows() {
        const rect = r => [r.x, r.y, r.width, r.height];
        return JSON.stringify(global.get_window_actors().map(actor => actor.meta_window).map(w => ({
            pid: w.get_pid(),
            title: w.get_title() || '',
            focus: w.has_focus(),
            minimized: w.minimized,
            frame: rect(w.get_frame_rect()),
            buffer: rect(w.get_buffer_rect()),
        })));
    }

    Activate(pid, title) {
        const w = find(pid, title);
        if (!w)
            return false;
        w.activate(global.get_current_time());
        return true;
    }

    Close(pid, title) {
        const w = find(pid, title);
        if (!w || !w.can_close())
            return false;
        w.delete(global.get_current_time());
        return true;
    }
}

function init() {
    return new Extension();
}
