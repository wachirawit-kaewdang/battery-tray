"""Runs the Logitech HID++ reader against simulated receivers and devices.

This checks the reader's own logic (request framing, reply matching, error handling,
the three battery features, asleep/dedupe behaviour). The simulated devices answer
the way the HID++ 2.0 docs describe, so it can't prove real hardware agrees.

Run: python tests\\hidpp_simulator.py
"""
import importlib.machinery
import importlib.util
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
loader = importlib.machinery.SourceFileLoader("battery_tray", os.path.join(HERE, "..", "battery_tray.pyw"))
bt = importlib.util.module_from_spec(importlib.util.spec_from_loader("battery_tray", loader))
loader.exec_module(bt)


# ---------------------------------------------------------------- simulated devices

class SimDevice:
    """A HID++ 2.0 device: root at index 0, name at 1, battery feature at 2."""

    def __init__(self, name, feature, protocol=4, **battery):
        self.name, self.feature, self.protocol = name.encode(), feature, protocol
        self.battery = battery
        self.online = True
        self.features = {bt.FEAT_NAME: 1, feature: 2}

    def handle(self, idx, func, p):
        if idx == 0 and func == 0:  # root.getFeature
            return [self.features.get((p[0] << 8) | p[1], 0), 0, 0]
        if idx == 0 and func == 1:  # root.ping
            return [self.protocol, 0, p[2]]
        if idx == 1 and func == 0:
            return [len(self.name)]
        if idx == 1 and func == 1:
            return list(self.name[p[0]:p[0] + 16])
        b = self.battery
        if idx == 2 and b.get("error"):
            return ("err", 0x05)  # e.g. hardware error from the battery feature
        if idx == 2 and self.feature == bt.FEAT_UNIFIED_BATTERY:
            if func == 0:
                return [0x0F, 0x03 if b.get("soc", True) else 0x01]
            return [b.get("pct", 0), b.get("levels", 0), b.get("charge", 0), 0]
        if idx == 2 and self.feature == bt.FEAT_BATTERY_STATUS:
            return [b["pct"], 0, b["status"]]
        if idx == 2 and self.feature == bt.FEAT_BATTERY_VOLTAGE:
            return [b["mv"] >> 8, b["mv"] & 0xFF, b.get("flags", 0)]
        return ("err", 0x02)  # invalid function


class SimInterface:
    def __init__(self, pid, devices, has_short=True):
        self.pid, self.devices, self.has_short = pid, devices, has_short
        self.handles = []
        self.base = f"\\\\?\\HID#VID_046D&PID_{pid:04X}&MI_02&Col0{{}}#7&{pid:x}&0&000{{}}#{{{{4d1e55b2}}}}"

    def push(self, kind, report):
        for h in self.handles:
            if h.kind == kind:
                h.queue.append(report)

    def process(self, data):
        dev_idx, idx, tag = data[1], data[2], data[3]
        func, params = tag >> 4, data[4:]
        self.push("long", [0x11, dev_idx, 0x04, 0x00] + [0] * 16)  # unrelated notification first
        dev = self.devices.get(dev_idx)
        if dev is None or not dev.online:
            self.push("short", [0x10, dev_idx, 0x8F, idx, tag, 0x09, 0])  # resource error
            return
        if dev.protocol < 2:
            self.push("short", [0x10, dev_idx, 0x8F, idx, tag, 0x01, 0])  # invalid sub-id
            return
        result = dev.handle(idx, func, params)
        if isinstance(result, tuple):
            self.push("long", [0x11, dev_idx, 0xFF, idx, tag, result[1]] + [0] * 14)
        else:
            self.push("long", ([0x11, dev_idx, idx, tag] + result + [0] * 16)[:20])

    def infos(self):
        out = [{"path": self.base.format(2, 2).encode(), "product_id": self.pid,
                "usage_page": 0xFF00, "usage": 0x0002}]
        if self.has_short:
            out.append({"path": self.base.format(1, 1).encode(), "product_id": self.pid,
                        "usage_page": 0xFF00, "usage": 0x0001})
        return out


INTERFACES: list[SimInterface] = []
OPENED: list[str] = []


class FakeHandle:
    def open_path(self, path):
        text = path.decode()
        OPENED.append(text)
        self.iface = next((i for i in INTERFACES if f"PID_{i.pid:04X}" in text), None)
        if self.iface is None:
            raise OSError("no such device")
        self.kind = "long" if "Col02" in text else "short"
        self.queue = []
        self.iface.handles.append(self)

    def set_nonblocking(self, _):
        pass

    def write(self, data):
        assert len(data) == 20 and data[0] == 0x11, f"bad request framing: {data}"
        self.iface.process(data)

    def read(self, _n, _timeout=0):
        return self.queue.pop(0) if self.queue else []

    def close(self):
        self.iface.handles.remove(self)


def fake_enumerate(vid=0, _pid=0):
    if vid != bt.LOGI_VID:
        return []
    out = [{"path": b"\\\\?\\HID#{00001124-0000-1000-8000-00805f9b34fb}_VID&0002046d_PID&b023",
            "product_id": 0xB023, "usage_page": 0xFF43, "usage": 0x0202}]  # Bluetooth: must be ignored
    for iface in INTERFACES:
        out += iface.infos()
    return out


bt.hid.device = FakeHandle
bt.hid.enumerate = fake_enumerate


# ---------------------------------------------------------------- scenarios

failures = 0


def check(label, cond, detail=""):
    global failures
    print(("PASS " if cond else "FAIL ") + label + (f"  ({detail})" if detail and not cond else ""))
    failures += not cond


def by_name(found):
    return {f.name: f for f in found}


# Real collection paths from one USB interface (a Lamzu dongle on Windows 11): each
# collection has its own path, differing in &ColNN, the instance suffix, and sometimes \KBD.
real = [r"\\?\HID#VID_373E&PID_001E&MI_01&Col01#8&737e579&0&0000#{4d1e55b2-f16f-11cf-88cb-001111000030}",
        r"\\?\HID#VID_373E&PID_001E&MI_01&Col03#8&737e579&0&0002#{4d1e55b2-f16f-11cf-88cb-001111000030}\KBD",
        r"\\?\HID#VID_373E&PID_001E&MI_01&Col05#8&737e579&0&0004#{4d1e55b2-f16f-11cf-88cb-001111000030}"]
other = r"\\?\HID#VID_373E&PID_001E&MI_02#8&2b0f233b&0&0000#{4d1e55b2-f16f-11cf-88cb-001111000030}"
check("collections of one interface group together, other interfaces don't",
      len({bt._hid_interface_id(p) for p in real}) == 1
      and bt._hid_interface_id(other) != bt._hid_interface_id(real[0]))

pro = SimDevice("PRO X Wireless", bt.FEAT_UNIFIED_BATTERY, pct=73)
mx = SimDevice("MX Master 3S", bt.FEAT_BATTERY_STATUS, pct=40, status=1)
k = SimDevice("Logitech K400", bt.FEAT_UNIFIED_BATTERY, soc=False, levels=0x02)
old = SimDevice("M705", bt.FEAT_BATTERY_STATUS, protocol=1, pct=0, status=0)
g502 = SimDevice("G502 X", bt.FEAT_BATTERY_VOLTAGE, mv=3900, flags=0x80)
INTERFACES[:] = [
    SimInterface(0xC539, {1: pro}),                    # Lightspeed, one mouse
    SimInterface(0xC52B, {1: mx, 2: k, 3: old}),       # Unifying, three devices
    SimInterface(0xC08B, {0xFF: g502}),                # mouse on USB cable
]

found = by_name(bt.find_logitech())
check("finds 4 devices with batteries", len(found) == 4, sorted(found))
r = found.get("Logitech PRO X Wireless")
check("unified battery: 73%, not charging, via receiver",
      r and (r.reading.percent, r.reading.charging, r.reading.via) == (73, False, "receiver"), r)
r = found.get("Logitech MX Master 3S")
check("battery status: 40% charging", r and (r.reading.percent, r.reading.charging) == (40, True), r)
r = found.get("Logitech K400")
check("name already starting with Logitech isn't doubled", r is not None, sorted(found))
check("unified without %: coarse 'low'", r and (r.reading.coarse, r.reading.percent) == ("low", 20), r)
r = found.get("Logitech G502 X")
check("voltage 3900 mV: approx 60-70%, charging, via USB",
      r and 60 <= r.reading.percent <= 70 and r.reading.approx and r.reading.charging
      and r.reading.via == "USB", r)
check("HID++ 1.0 device skipped", "Logitech M705" not in found)
check("Bluetooth HID path never opened", not any("00001124" in p for p in OPENED), OPENED)
check("tooltip marks voltage % as approximate",
      bt.tooltip_for("G", bt.Reading("ok", 67, via="USB", approx=True), None, None) == "G: ~67% (USB)")

pro.online = False
found = by_name(bt.find_logitech())
r = found.get("Logitech PRO X Wireless")
check("known device going offline shows as asleep", r and r.reading.state == "asleep", r)

INTERFACES.append(SimInterface(0xC095, {0xFF: SimDevice("PRO X Wireless", bt.FEAT_UNIFIED_BATTERY,
                                                         pct=55, charge=1)}))
found = bt.find_logitech()
pros = [f for f in found if f.name == "Logitech PRO X Wireless"]
check("charging on cable replaces its asleep receiver entry",
      len(pros) == 1 and pros[0].reading.state == "ok" and pros[0].reading.charging, pros)

INTERFACES[:] = [SimInterface(0xC548, {1: SimDevice("MX Keys", bt.FEAT_BATTERY_VOLTAGE, error=True)})]
t = time.monotonic()
found = bt.find_logitech()
elapsed = time.monotonic() - t
check("HID++ 2.0 error reply is recognised at once, not waited out",
      found == [] and elapsed < 0.3, f"{found}, {elapsed:.2f}s")

INTERFACES[:] = [SimInterface(0xC547, {}, has_short=False)]
t = time.monotonic()
found = bt.find_logitech()
check("receiver without short collection: empty slots time out cleanly",
      found == [], f"{found}, {time.monotonic() - t:.1f}s")

print()
print("all passed" if not failures else f"{failures} FAILED")
sys.exit(1 if failures else 0)
