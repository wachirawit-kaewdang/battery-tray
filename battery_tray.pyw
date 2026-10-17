"""Device batteries in the Windows tray: one icon per device.

Sources:
  - Lamzu mouse (dongle or cable), over vendor HID
  - Bluetooth devices that report battery to Windows (headsets, mice, keyboards...)
  - Xbox controllers, via XInput (only 4 coarse levels: empty/low/medium/full)

Setup:  pip install hidapi pystray Pillow
Run:    pythonw battery_tray.pyw          (no console window)
Test:   python battery_tray.pyw --once    (prints one reading per device and exits)
Build:  build.bat                          (makes dist\\BatteryTray.exe)

Close the Lamzu web hub tab if Lamzu reads keep failing; it can hold the HID interface.
Log file: %LOCALAPPDATA%\\BatteryTray\\battery.log
"""
import ctypes
import logging
import os
import sys
import threading
import time
import uuid
import winreg
from ctypes import wintypes
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler

import hid
import pystray
from PIL import Image, ImageDraw, ImageFont

POLL_SECONDS = 60
TICK_SECONDS = 10       # how often the loop wakes to check the clock
RESUME_GAP = 45         # a clock jump bigger than this means the PC was asleep
RESUME_DELAY = 4        # give USB/Bluetooth a moment to come back after resume
LOW_ALERTS = (20, 10, 5)  # notify once at each threshold until it charges again

APP_NAME = "BatteryTray"
OLD_APP_NAME = "LamzuBattery"  # autostart entry from the Lamzu-only version
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
THEME_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
LOG_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), APP_NAME)

log = logging.getLogger(APP_NAME)


@dataclass
class Reading:
    state: str              # "ok", "asleep", "error"
    percent: int | None = None
    charging: bool = False
    via: str = ""           # "wireless" / "cable" / "bluetooth" / "wired"
    detail: str = ""
    coarse: str = ""        # Xbox: "empty"/"low"/"medium"/"full" instead of a real %


@dataclass
class Found:
    key: str                # stable id, used to match the device across polls
    name: str
    reading: Reading


# ---------------------------------------------------------------- Lamzu (vendor HID)

LAMZU_VID = 0x373E
LAMZU_PIDS = ((0x001C, "cable"), (0x001E, "wireless"))  # cable first: authoritative when plugged in
LAMZU_USAGE_PAGE, LAMZU_USAGE = 0xFFFF, 0x00  # interface 2
REPORT_LEN = 64


def _lamzu_request():
    payload = [0x00, 0x00, 0x02, 0x02, 0x00, 0x83]
    payload += [0x00] * (REPORT_LEN - len(payload))
    return [0x00] + payload  # leading report ID 0 for hidapi


def _lamzu_query(path):
    """Returns (percent, charging) or None if the mouse never answered."""
    dev = hid.device()
    dev.open_path(path)
    try:
        dev.send_feature_report(_lamzu_request())
        # 0xA0 = still waiting on the mouse, 0xA1 = answered.
        for _ in range(50):
            resp = dev.get_feature_report(0, REPORT_LEN + 1)
            if len(resp) == REPORT_LEN + 1:  # Windows prepends the report ID
                resp = resp[1:]
            if resp and resp[0] != 0xA0:
                break
            time.sleep(0.01)
        if resp and resp[0] == 0xA1 and resp[5] == 0x83:
            return resp[7], bool(resp[6])
        return None
    finally:
        dev.close()


def find_lamzu():
    found_any = False
    last_error = ""
    name = "Lamzu mouse"
    for pid, via in LAMZU_PIDS:
        for info in hid.enumerate(LAMZU_VID, pid):
            if info["usage_page"] != LAMZU_USAGE_PAGE or info["usage"] != LAMZU_USAGE:
                continue
            found_any = True
            product = (info.get("product_string") or "").replace(" Dongle", "").strip()
            if product:
                name = product.title()  # "LAMZU Maya X 8K" -> "Lamzu Maya X 8K"
            try:
                result = _lamzu_query(info["path"])
            except OSError as e:
                last_error = str(e) or "HID access failed"
                continue
            if result:
                pct, charging = result
                r = Reading("ok", max(0, min(100, pct)), charging or via == "cable", via)
                return [Found("lamzu", name, r)]
    if not found_any:
        return []  # dongle and cable both unplugged: no icon
    if last_error:
        return [Found("lamzu", name, Reading("error", detail=last_error))]
    return [Found("lamzu", name, Reading("asleep"))]


# ---------------------------------------------------------------- Bluetooth (Windows device properties)

class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class _DEVPROPKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", wintypes.ULONG)]


class _SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("ClassGuid", _GUID),
                ("DevInst", wintypes.DWORD), ("Reserved", ctypes.c_void_p)]


def _propkey(fmtid, pid):
    return _DEVPROPKEY(_GUID.from_buffer_copy(uuid.UUID(fmtid).bytes_le), pid)


KEY_BATTERY = _propkey("104EA319-6EE2-4701-BD47-8DDBF425BBE5", 2)    # Bluetooth battery %
KEY_FRIENDLY = _propkey("A45C254E-DF1C-4EFD-8020-67D146A850E0", 14)  # DEVPKEY_Device_FriendlyName
KEY_DESC = _propkey("A45C254E-DF1C-4EFD-8020-67D146A850E0", 2)       # DEVPKEY_Device_DeviceDesc
KEY_CONTAINER = _propkey("8C7ED206-3F8A-4827-B3AB-AE9E1FAEFC6C", 2)  # DEVPKEY_Device_ContainerId
KEY_INSTANCE = _propkey("78C34FC8-104A-4ACA-9EA4-524D52996E57", 256) # DEVPKEY_Device_InstanceId
KEY_CONNECTED = _propkey("83DA6326-97A6-4088-9453-A1923F573B29", 15) # Bluetooth "connected" flag

DIGCF_PRESENT, DIGCF_ALLCLASSES = 0x2, 0x4
DEVPROP_TYPE_BYTE, DEVPROP_TYPE_BOOLEAN = 0x03, 0x11
DEVPROP_TYPE_GUID, DEVPROP_TYPE_STRING = 0x0D, 0x12

_setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
_setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
_setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
_setupapi.SetupDiEnumDeviceInfo.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_SP_DEVINFO_DATA)]
_setupapi.SetupDiEnumDeviceInfo.restype = wintypes.BOOL
_setupapi.SetupDiGetDevicePropertyW.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(_SP_DEVINFO_DATA), ctypes.POINTER(_DEVPROPKEY),
    ctypes.POINTER(wintypes.ULONG), ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
_setupapi.SetupDiGetDevicePropertyW.restype = wintypes.BOOL
_setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
_INVALID_HANDLE = ctypes.c_void_p(-1).value


def _devprop(hdev, data, key):
    ptype, size = wintypes.ULONG(), wintypes.DWORD()
    _setupapi.SetupDiGetDevicePropertyW(hdev, ctypes.byref(data), ctypes.byref(key), ctypes.byref(ptype),
                                        None, 0, ctypes.byref(size), 0)
    if not size.value:
        return None
    buf = ctypes.create_string_buffer(size.value)
    if not _setupapi.SetupDiGetDevicePropertyW(hdev, ctypes.byref(data), ctypes.byref(key), ctypes.byref(ptype),
                                               buf, size, None, 0):
        return None
    t = ptype.value
    if t == DEVPROP_TYPE_STRING:
        return buf.raw.decode("utf-16-le").split("\0", 1)[0]
    if t in (DEVPROP_TYPE_BYTE, DEVPROP_TYPE_BOOLEAN):
        return buf.raw[0]
    if t == DEVPROP_TYPE_GUID:
        return str(uuid.UUID(bytes_le=buf.raw[:16]))
    return None


def find_bluetooth():
    """Windows stores the battery % on one of the device's service nodes, and the name and
    connected flag on its main BTHENUM/BTHLE node, so group the nodes by container."""
    hdev = _setupapi.SetupDiGetClassDevsW(None, None, None, DIGCF_PRESENT | DIGCF_ALLCLASSES)
    if hdev in (None, _INVALID_HANDLE):
        raise OSError(ctypes.get_last_error(), "SetupDiGetClassDevs failed")
    containers = {}
    try:
        data = _SP_DEVINFO_DATA()
        data.cbSize = ctypes.sizeof(_SP_DEVINFO_DATA)
        i = 0
        while _setupapi.SetupDiEnumDeviceInfo(hdev, i, ctypes.byref(data)):
            i += 1
            inst = (_devprop(hdev, data, KEY_INSTANCE) or "").upper()
            battery = _devprop(hdev, data, KEY_BATTERY)
            is_main = inst.startswith(("BTHENUM\\DEV_", "BTHLE\\DEV_"))
            if battery is None and not is_main:
                continue
            cid = _devprop(hdev, data, KEY_CONTAINER) or inst
            c = containers.setdefault(cid, {})
            label = _devprop(hdev, data, KEY_FRIENDLY) or _devprop(hdev, data, KEY_DESC)
            if battery is not None:
                c.setdefault("battery", battery)
                c.setdefault("fallback_name", label)
            if is_main:
                c["name"] = label
                connected = _devprop(hdev, data, KEY_CONNECTED)
                if connected is not None:
                    c["connected"] = bool(connected)
    finally:
        _setupapi.SetupDiDestroyDeviceInfoList(hdev)

    out = []
    for cid, c in containers.items():
        if "battery" not in c or c.get("connected") is False:
            continue  # paired but switched off: Windows keeps a stale %, so hide it
        name = c.get("name") or c.get("fallback_name") or "Bluetooth device"
        if "xbox" in name.lower():
            continue  # XInput covers these, with charging info
        pct = max(0, min(100, c["battery"]))
        out.append(Found(f"bt:{cid}", name, Reading("ok", pct, via="bluetooth")))
    return out


# ---------------------------------------------------------------- Xbox controllers (XInput)

class _XINPUT_BATTERY(ctypes.Structure):
    _fields_ = [("BatteryType", ctypes.c_ubyte), ("BatteryLevel", ctypes.c_ubyte)]


try:
    _xinput = ctypes.WinDLL("xinput1_4")
    _xinput.XInputGetBatteryInformation.argtypes = [wintypes.DWORD, ctypes.c_ubyte, ctypes.POINTER(_XINPUT_BATTERY)]
    _xinput.XInputGetBatteryInformation.restype = wintypes.DWORD
except (OSError, AttributeError):
    _xinput = None

XINPUT_TYPE_DISCONNECTED, XINPUT_TYPE_WIRED = 0x00, 0x01
XINPUT_LEVELS = {0: ("empty", 5), 1: ("low", 20), 2: ("medium", 60), 3: ("full", 100)}


def find_xbox():
    if _xinput is None:
        return []
    out = []
    for user in range(4):
        info = _XINPUT_BATTERY()
        if _xinput.XInputGetBatteryInformation(user, 0, ctypes.byref(info)) != 0:
            continue  # ERROR_DEVICE_NOT_CONNECTED
        if info.BatteryType == XINPUT_TYPE_DISCONNECTED:
            continue
        name = f"Xbox controller {user + 1}"
        if info.BatteryType == XINPUT_TYPE_WIRED:
            r = Reading("ok", None, charging=True, via="wired")
        else:
            word, pct = XINPUT_LEVELS.get(info.BatteryLevel, ("unknown", None))
            r = Reading("ok", pct, via="wireless", coarse=word)
        out.append(Found(f"xinput:{user}", name, r))
    return out


PROVIDERS = (("Lamzu", find_lamzu), ("Bluetooth", find_bluetooth), ("Xbox", find_xbox))


def find_all():
    found = []
    for label, provider in PROVIDERS:
        try:
            found += provider()
        except Exception:  # one broken source shouldn't hide the others
            log.exception("%s scan failed", label)
    return found


# ---------------------------------------------------------------- icon

_FONT_CACHE = {}

GREY = (140, 140, 140)
GREEN = (70, 200, 100)
AMBER = (245, 170, 30)
RED = (235, 60, 60)
BLUE = (60, 150, 255)


def _font(size):
    if size not in _FONT_CACHE:
        for name in ("segoeuib.ttf", "arialbd.ttf"):
            try:
                _FONT_CACHE[size] = ImageFont.truetype(name, size)
                break
            except OSError:
                pass
        else:
            _FONT_CACHE[size] = ImageFont.load_default()
    return _FONT_CACHE[size]


def light_taskbar():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, THEME_KEY) as k:
            return winreg.QueryValueEx(k, "SystemUsesLightTheme")[0] == 1
    except OSError:
        return False


def _level_color(pct):
    if pct <= 10:
        return RED
    if pct <= 25:
        return AMBER
    return GREEN


def render_battery(pct, fill, *, text=None, bolt=False, dim=False, light=False):
    """64x64 horizontal battery: outline, level fill, then a number, bolt or symbol."""
    S = 64
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    ink = (30, 30, 30) if light else (240, 240, 240)
    halo = (255, 255, 255) if light else (0, 0, 0)
    outline = GREY if dim else ink
    if not dim and not bolt and pct is not None and pct <= 25:
        outline = fill  # low: the whole battery turns amber/red, the fill alone is too thin to see

    body = (1, 9, 55, 55)
    d.rounded_rectangle(body, radius=8, outline=outline, width=5)
    d.rounded_rectangle((56, 23, 63, 41), radius=3, fill=outline)

    if pct is not None and pct > 0:
        inner_l, inner_r = 8, 48
        w = max(4, round((inner_r - inner_l) * pct / 100))
        d.rounded_rectangle((inner_l, 16, inner_l + w, 48), radius=3, fill=fill)

    cx, cy = (body[0] + body[2]) / 2, (body[1] + body[3]) / 2
    if bolt:
        pts = [(cx + 4, cy - 22), (cx - 12, cy + 3), (cx - 1, cy + 3),
               (cx - 5, cy + 22), (cx + 12, cy - 4), (cx + 1, cy - 4)]
        d.polygon(pts, fill=ink, outline=halo, width=3)
    elif text:
        fs = 44
        while fs > 14:
            b = d.textbbox((0, 0), text, font=_font(fs), stroke_width=3)
            if b[2] - b[0] <= 50 and b[3] - b[1] <= 42:
                break
            fs -= 2
        font = _font(fs)
        b = d.textbbox((0, 0), text, font=font, stroke_width=3)
        x = cx - (b[2] - b[0]) / 2 - b[0]
        y = cy - (b[3] - b[1]) / 2 - b[1]
        d.text((x, y), text, font=font, fill=GREY if dim else ink,
               stroke_width=3, stroke_fill=halo)
    return img


def icon_for(r: Reading, last: Reading | None, light=False):
    if r.state == "ok":
        if r.charging:
            return render_battery(r.percent, BLUE, bolt=True, light=light)
        if r.percent is None:
            return render_battery(None, None, text="?", dim=True, light=light)
        # Coarse levels get a bar only: a number like "60" would claim precision we don't have.
        text = None if r.coarse else str(r.percent)
        return render_battery(r.percent, _level_color(r.percent), text=text, light=light)
    if r.state == "asleep" and last and last.percent is not None:
        return render_battery(last.percent, (110, 110, 110), text=str(last.percent), dim=True, light=light)
    if r.state == "error":
        return render_battery(None, None, text="!", dim=True, light=light)
    return render_battery(None, None, text="?", dim=True, light=light)


def tooltip_for(name, r: Reading, last: Reading | None, last_time):
    if r.state == "ok":
        if r.coarse:
            text = f"{name}: battery {r.coarse}"
        elif r.percent is None:
            text = f"{name}: {r.via}" + (" (no battery reading)" if r.via == "wired" else "")
        elif r.charging:
            text = f"{name}: ~{r.percent}% (charging, approximate)"
        else:
            text = f"{name}: {r.percent}% ({r.via})"
    elif r.state == "asleep":
        text = f"{name}: asleep or off"
        if last and last.percent is not None:
            text += f" (last {last.percent}% at {time.strftime('%H:%M', last_time)})"
    else:
        text = f"{name}: read failed ({r.detail})"
    return text[:127]


# ---------------------------------------------------------------- system

def _launch_command():
    if getattr(sys, "frozen", False):  # PyInstaller build
        return f'"{sys.executable}"'
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        pyw = exe[:-10] + "pythonw.exe"
        if os.path.exists(pyw):
            exe = pyw
    return f'"{exe}" "{os.path.abspath(__file__)}"'


def autostart_enabled(_=None):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def toggle_autostart(icon, _item):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if autostart_enabled():
            winreg.DeleteValue(k, APP_NAME)
            log.info("autostart off")
        else:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _launch_command())
            log.info("autostart on: %s", _launch_command())


def refresh_autostart_path():
    """Carry over the old LamzuBattery entry, and repoint autostart here if the app moved."""
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                        winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as k:
        try:
            winreg.QueryValueEx(k, OLD_APP_NAME)
            winreg.DeleteValue(k, OLD_APP_NAME)
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _launch_command())
            log.info("autostart migrated from %s", OLD_APP_NAME)
        except OSError:
            pass
        try:
            current = winreg.QueryValueEx(k, APP_NAME)[0]
        except OSError:
            return
        if current != _launch_command():
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _launch_command())
            log.info("autostart repointed: %s", _launch_command())


_mutex = None


def claim_single_instance():
    """True if we're the only copy. The mutex lives until the process exits."""
    global _mutex
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    _mutex = kernel32.CreateMutexW(None, False, "Local\\BatteryTray")
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    h = RotatingFileHandler(os.path.join(LOG_DIR, "battery.log"), maxBytes=256_000,
                            backupCount=1, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)
    sys.excepthook = lambda *exc: log.critical("crash", exc_info=exc)
    threading.excepthook = lambda a: log.critical("thread crash", exc_info=(a.exc_type, a.exc_value, a.exc_traceback))


# ---------------------------------------------------------------- app

class TrayIcon:
    """One tray icon. pystray on Windows pumps messages for the thread that runs the icon,
    so each icon gets its own thread and the poller just pushes images and text to it."""

    def __init__(self, app, key, name, image, title):
        self.app, self.key, self.name = app, key, name
        self.status = title
        self.last: Reading | None = None
        self.last_ok: Reading | None = None
        self.last_ok_time = None
        self.alerted = set()
        self._ready = threading.Event()
        self.icon = pystray.Icon(f"{APP_NAME}-{key}", image, title, menu=app.menu(self))
        threading.Thread(target=self.icon.run, kwargs={"setup": self._setup},
                         daemon=True, name=f"tray-{key}").start()
        self._ready.wait(5)

    def _setup(self, icon):
        icon.visible = True
        self._ready.set()

    def show(self, image, title):
        self.status = title
        self.icon.icon = image
        self.icon.title = title
        self.icon.update_menu()

    def update(self, name, r: Reading, light):
        self.name = name
        if r.state == "ok":
            self.last_ok, self.last_ok_time = r, time.localtime()
            self.check_alerts(r)
        if self.last is None or (r.state, r.percent, r.charging, r.detail) != (
                self.last.state, self.last.percent, self.last.charging, self.last.detail):
            log.info("%s", tooltip_for(name, r, self.last_ok, self.last_ok_time))
        self.last = r
        self.show(icon_for(r, self.last_ok, light), tooltip_for(name, r, self.last_ok, self.last_ok_time))

    def check_alerts(self, r: Reading):
        if r.percent is None:
            return
        # Re-arm once it's charging OR clearly back above the thresholds. The second
        # case covers charging from a wall brick, where the app never sees "charging".
        # The +5 margin stops a reading wobbling 20<->21 from re-alerting.
        if r.charging or r.percent > max(LOW_ALERTS) + 5:
            self.alerted.clear()
            return
        for level in sorted(LOW_ALERTS):
            if r.percent <= level and level not in self.alerted:
                self.alerted.update(l for l in LOW_ALERTS if l >= level)
                amount = f"battery {r.coarse}" if r.coarse else f"battery at {r.percent}%"
                try:
                    self.icon.notify(f"{self.name} {amount}. Time to charge.", "Battery low")
                except Exception:
                    log.exception("notify failed")
                break

    def close(self):
        try:
            self.icon.stop()
        except Exception:
            log.exception("closing icon %s failed", self.key)


class App:
    PLACEHOLDER = "__none__"

    def __init__(self):
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.icons: dict[str, TrayIcon] = {}

    def menu(self, tray: TrayIcon):
        return pystray.Menu(
            pystray.MenuItem(lambda _: tray.status, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Refresh now", lambda: self.wake.set(), default=True),
            pystray.MenuItem("Start with Windows", toggle_autostart, checked=autostart_enabled),
            pystray.MenuItem("Open log folder", lambda: os.startfile(LOG_DIR)),
            pystray.MenuItem("Quit", self.quit),
        )

    def quit(self):
        log.info("quit")
        self.stop.set()
        self.wake.set()

    def sync(self, found: list[Found]):
        light = light_taskbar()
        live = {f.key for f in found}
        for key in [k for k in self.icons if k not in live and k != self.PLACEHOLDER]:
            log.info("%s gone", self.icons[key].name)
            self.icons.pop(key).close()

        for f in found:
            if f.key not in self.icons:
                log.info("%s found", f.name)
                self.icons[f.key] = TrayIcon(self, f.key, f.name, icon_for(f.reading, None, light), f.name)
            self.icons[f.key].update(f.name, f.reading, light)

        # With nothing to show, keep one icon so there's still a menu (and a way to quit).
        if not found and self.PLACEHOLDER not in self.icons:
            self.icons[self.PLACEHOLDER] = TrayIcon(
                self, self.PLACEHOLDER, "Battery Tray",
                render_battery(None, None, text="?", dim=True, light=light), "No battery devices found")
        elif found and self.PLACEHOLDER in self.icons:
            self.icons.pop(self.PLACEHOLDER).close()

    def run(self):
        self.sync(find_all())
        last_poll = last_tick = time.time()
        while not self.stop.is_set():
            forced = self.wake.wait(TICK_SECONDS)
            self.wake.clear()
            if self.stop.is_set():
                break
            now = time.time()
            if now - last_tick > RESUME_GAP:  # wall clock jumped: PC was asleep
                log.info("resumed from sleep (%.0fs gap)", now - last_tick)
                time.sleep(RESUME_DELAY)
                forced = True
            last_tick = time.time()
            if forced or last_tick - last_poll >= POLL_SECONDS:
                self.sync(find_all())
                last_poll = time.time()
        for tray in self.icons.values():
            tray.close()


def main():
    if "--once" in sys.argv:
        logging.basicConfig()
        found = find_all()
        for f in found:
            print(f"{f.key:<45} {tooltip_for(f.name, f.reading, None, None)}")
        if not found:
            print("No battery devices found.")
        return
    if not claim_single_instance():
        return  # already running
    setup_logging()
    log.info("start (%s)", _launch_command())
    refresh_autostart_path()
    App().run()


if __name__ == "__main__":
    main()
