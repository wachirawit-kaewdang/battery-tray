"""Lamzu mouse battery in the Windows tray.

Setup:  pip install hidapi pystray Pillow
Run:    pythonw lamzu_tray.pyw          (no console window)
Test:   python lamzu_tray.pyw --once    (prints one reading and exits)
Build:  build.bat                        (makes dist\\LamzuBattery.exe)

Close the Lamzu web hub tab if reads keep failing; it can hold the HID interface.
Log file: %LOCALAPPDATA%\\LamzuBattery\\lamzu.log
"""
import ctypes
import logging
import os
import sys
import threading
import time
import winreg
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler

import hid
import pystray
from PIL import Image, ImageDraw, ImageFont

VID = 0x373E
PID_DONGLE = 0x001E  # wireless
PID_CABLE = 0x001C   # mouse on cable
USAGE_PAGE, USAGE = 0xFFFF, 0x00  # interface 2
REPORT_LEN = 64

POLL_SECONDS = 60
TICK_SECONDS = 10       # how often the loop wakes to check the clock
RESUME_GAP = 45         # a clock jump bigger than this means the PC was asleep
RESUME_DELAY = 4        # give USB a moment to come back after resume
LOW_ALERTS = (20, 10, 5)  # notify once at each threshold until it charges again

APP_NAME = "LamzuBattery"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
THEME_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
LOG_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), APP_NAME)

log = logging.getLogger(APP_NAME)


# ---------------------------------------------------------------- HID

@dataclass
class Reading:
    state: str              # "ok", "asleep", "missing", "error"
    percent: int | None = None
    charging: bool = False
    via: str = ""           # "wireless" / "cable"
    detail: str = ""


def _request():
    payload = [0x00, 0x00, 0x02, 0x02, 0x00, 0x83]
    payload += [0x00] * (REPORT_LEN - len(payload))
    return [0x00] + payload  # leading report ID 0 for hidapi


def _query(path):
    """Returns (percent, charging) or None if the mouse never answered."""
    dev = hid.device()
    dev.open_path(path)
    try:
        dev.send_feature_report(_request())
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


def read_battery() -> Reading:
    # Cable first: if the mouse is plugged in, that's the authoritative path.
    found_any = False
    last_error = ""
    for pid, via in ((PID_CABLE, "cable"), (PID_DONGLE, "wireless")):
        for info in hid.enumerate(VID, pid):
            if info["usage_page"] != USAGE_PAGE or info["usage"] != USAGE:
                continue
            found_any = True
            try:
                result = _query(info["path"])
            except OSError as e:
                last_error = str(e) or "HID access failed"
                continue
            if result:
                pct, charging = result
                return Reading("ok", max(0, min(100, pct)), charging or via == "cable", via)
    if not found_any:
        return Reading("missing")
    if last_error:
        return Reading("error", detail=last_error)
    return Reading("asleep")


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
        return render_battery(r.percent, _level_color(r.percent), text=str(r.percent), light=light)
    if r.state == "asleep" and last and last.percent is not None:
        return render_battery(last.percent, (110, 110, 110), text=str(last.percent), dim=True, light=light)
    if r.state == "error":
        return render_battery(None, None, text="!", dim=True, light=light)
    return render_battery(None, None, text="?", dim=True, light=light)


def tooltip_for(r: Reading, last: Reading | None, last_time):
    if r.state == "ok":
        if r.charging:
            return f"Lamzu: ~{r.percent}% (charging, approximate)"
        return f"Lamzu: {r.percent}% ({r.via})"
    if r.state == "asleep":
        if last and last.percent is not None:
            return f"Lamzu: asleep or off (last {last.percent}% at {time.strftime('%H:%M', last_time)})"
        return "Lamzu: asleep or off"
    if r.state == "missing":
        return "Lamzu: dongle/cable not found"
    return f"Lamzu: read failed ({r.detail})"[:127]


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
    """If autostart is on but points somewhere else (old copy, moved file), repoint it here."""
    if not autostart_enabled():
        return
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                        winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as k:
        if winreg.QueryValueEx(k, APP_NAME)[0] != _launch_command():
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _launch_command())
            log.info("autostart repointed: %s", _launch_command())


_mutex = None


def claim_single_instance():
    """True if we're the only copy. The mutex lives until the process exits."""
    global _mutex
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    _mutex = kernel32.CreateMutexW(None, False, "Local\\LamzuBatteryTray")
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    h = RotatingFileHandler(os.path.join(LOG_DIR, "lamzu.log"), maxBytes=256_000,
                            backupCount=1, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)
    sys.excepthook = lambda *exc: log.critical("crash", exc_info=exc)
    threading.excepthook = lambda a: log.critical("thread crash", exc_info=(a.exc_type, a.exc_value, a.exc_traceback))


# ---------------------------------------------------------------- app

class App:
    def __init__(self):
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.last: Reading | None = None
        self.last_ok: Reading | None = None
        self.last_ok_time = None
        self.alerted = set()
        self.status = "Starting..."
        self.light = light_taskbar()
        self.icon = pystray.Icon(
            APP_NAME,
            render_battery(None, None, text="..", dim=True, light=self.light),
            "Lamzu: reading...",
            menu=pystray.Menu(
                pystray.MenuItem(lambda _: self.status, None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Refresh now", lambda: self.wake.set(), default=True),
                pystray.MenuItem("Start with Windows", toggle_autostart, checked=autostart_enabled),
                pystray.MenuItem("Open log folder", lambda: os.startfile(LOG_DIR)),
                pystray.MenuItem("Quit", self.quit),
            ),
        )

    def quit(self):
        log.info("quit")
        self.stop.set()
        self.wake.set()
        self.icon.stop()

    def update(self, r: Reading):
        if r.state == "ok":
            self.last_ok, self.last_ok_time = r, time.localtime()
            self.check_alerts(r)
        if self.last is None or (r.state, r.percent, r.charging, r.detail) != (
                self.last.state, self.last.percent, self.last.charging, self.last.detail):
            log.info("%s", tooltip_for(r, self.last_ok, self.last_ok_time))
        self.last = r
        self.light = light_taskbar()
        self.icon.icon = icon_for(r, self.last_ok, self.light)
        self.status = tooltip_for(r, self.last_ok, self.last_ok_time)
        self.icon.title = self.status
        self.icon.update_menu()

    def check_alerts(self, r: Reading):
        # Re-arm once it's charging OR clearly back above the thresholds. The second
        # case covers charging from a wall brick, where the app never sees "charging".
        # The +5 margin stops a reading wobbling 20<->21 from re-alerting.
        if r.charging or r.percent > max(LOW_ALERTS) + 5:
            self.alerted.clear()
            return
        for level in sorted(LOW_ALERTS):
            if r.percent <= level and level not in self.alerted:
                self.alerted.update(l for l in LOW_ALERTS if l >= level)
                try:
                    self.icon.notify(f"Lamzu battery at {r.percent}%. Time to charge.", "Mouse battery low")
                except Exception:
                    log.exception("notify failed")
                break

    def poll_once(self):
        try:
            r = read_battery()
        except Exception as e:  # never let the poller die
            log.exception("read failed")
            r = Reading("error", detail=str(e))
        self.update(r)

    def poll_loop(self):
        self.icon.visible = True
        self.poll_once()
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
                self.poll_once()
                last_poll = time.time()

    def run(self):
        self.icon.run(setup=lambda _icon: threading.Thread(target=self.poll_loop, daemon=True).start())


def main():
    if "--once" in sys.argv:
        r = read_battery()
        print(r)
        print(tooltip_for(r, None, None))
        return
    if not claim_single_instance():
        return  # already running
    setup_logging()
    log.info("start (%s)", _launch_command())
    refresh_autostart_path()
    App().run()


if __name__ == "__main__":
    main()
