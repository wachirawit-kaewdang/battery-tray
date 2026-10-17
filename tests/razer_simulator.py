"""Runs the Razer reader against simulated Razer devices.

This checks the reader's own logic (packet layout and checksum, reply matching, busy and
timeout replies, transaction-id probing, collection selection, cable/dongle dedupe). The
simulated devices answer the way OpenRazer documents, so it can't prove real hardware agrees.

Run: python tests\\razer_simulator.py
"""
import importlib.machinery
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
loader = importlib.machinery.SourceFileLoader("battery_tray", os.path.join(HERE, "..", "battery_tray.pyw"))
bt = importlib.util.module_from_spec(importlib.util.spec_from_loader("battery_tray", loader))
loader.exec_module(bt)
bt.time.sleep = lambda s: None  # the reader's reply polling sleeps; the simulator answers at once


# ---------------------------------------------------------------- simulated devices

class SimRazer:
    def __init__(self, pid, product, tid, level=0, charging=False, *, online=True,
                 battery=True, busy_reads=0, control_iface=0, ifaces=(0, 1)):
        self.pid, self.product, self.tid = pid, product, tid
        self.level, self.charging, self.online, self.battery = level, charging, online, battery
        self.busy_reads, self.control_iface, self.ifaces = busy_reads, control_iface, ifaces
        self.reply = [0] * 90
        self.busy_left = 0
        self.sent = []  # (tid, class, id) of every accepted request

    def path(self, iface):
        return f"\\\\?\\HID#VID_1532&PID_{self.pid:04X}&MI_0{iface}#8&{self.pid:x}{iface}&0&0000#{{4d1e55b2}}"

    def infos(self):
        return [{"path": self.path(i).encode(), "product_id": self.pid, "product_string": self.product,
                 "interface_number": i, "usage_page": 0x01, "usage": 0x02} for i in self.ifaces]

    def send(self, iface, data):
        if iface != self.control_iface:
            raise OSError("HidD_SetFeature: incorrect function")  # collection has no such report
        assert len(data) == 91 and data[0] == 0, f"bad report framing ({len(data)} bytes)"
        p = data[1:]
        crc = 0
        for b in p[2:88]:
            crc ^= b
        assert p[88] == crc, "bad checksum"
        assert p[5] == 0x02, "bad data size"
        tid, cls, cmd = p[1], p[6], p[7]
        self.sent.append((tid, cls, cmd))
        if tid != self.tid:
            return  # wrong transaction id: the device ignores it, the old reply stays
        r = list(p)
        if not self.battery:
            r[0] = 0x05  # not supported
        elif not self.online:
            r[0] = 0x04  # dongle timed out reaching the mouse
        else:
            r[0] = 0x02
            r[9] = self.level if (cls, cmd) == bt.RAZER_CMD_BATTERY else int(self.charging)
        self.reply = r
        self.busy_left = self.busy_reads

    def get(self, iface):
        if iface != self.control_iface:
            raise OSError("HidD_GetFeature: incorrect function")
        if self.busy_left:
            self.busy_left -= 1
            return [0, 0x01] + self.reply[1:]  # status busy
        return [0] + self.reply


DEVICES: list[SimRazer] = []
OPENED: list[str] = []


class FakeHandle:
    def open_path(self, path):
        text = path.decode()
        OPENED.append(text)
        for d in DEVICES:
            for i in d.ifaces:
                if d.path(i) == text:
                    self.dev, self.iface = d, i
                    return
        raise OSError("no such device")

    def send_feature_report(self, data):
        self.dev.send(self.iface, data)
        return len(data)

    def get_feature_report(self, _report_id, _n):
        return self.dev.get(self.iface)

    def close(self):
        pass


def fake_enumerate(vid=0, _pid=0):
    if vid != bt.RAZER_VID:
        return []
    out = [{"path": b"\\\\?\\HID#{00001124-0000-1000-8000-00805f9b34fb}_VID&00021532_PID&008a",
            "product_id": 0x008A, "product_string": "Razer Bluetooth Mouse", "interface_number": -1,
            "usage_page": 0x01, "usage": 0x02}]  # Bluetooth: must be ignored
    for d in DEVICES:
        out += d.infos()
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


check("packet checksum is XOR of bytes 2-87",
      bt._razer_packet(0x1F, 0x07, 0x80)[1 + 88] == 0x02 ^ 0x07 ^ 0x80)

viper = SimRazer(0x007B, "Razer Viper Ultimate Dongle", 0x3F, level=128, control_iface=1, busy_reads=2)
dav3 = SimRazer(0x00B6, "Razer DeathAdder V3 Pro (Wired)", 0x1F, level=255, charging=True)
essential = SimRazer(0x0071, "Razer DeathAdder Essential", 0xFF, battery=False)
keyboard = SimRazer(0x0241, "Razer BlackWidow", 0x1F, control_iface=9)  # rejects every report
DEVICES[:] = [viper, dav3, essential, keyboard]

found = by_name(bt.find_razer())
check("finds the 2 battery devices", sorted(found) == ["Razer DeathAdder V3 Pro", "Razer Viper Ultimate"],
      sorted(found))
r = found.get("Razer Viper Ultimate")
check("dongle: level 128 -> 50%, not charging, via dongle, despite 2 busy replies",
      r and (r.reading.state, r.reading.percent, r.reading.charging, r.reading.via) == ("ok", 50, False, "dongle"), r)
r = found.get("Razer DeathAdder V3 Pro")
check("cable: 100% charging via cable, '(Wired)' dropped from the name",
      r and (r.reading.percent, r.reading.charging, r.reading.via) == (100, True, "cable"), r)
check("Bluetooth HID path never opened", not any("00001124" in p for p in OPENED), OPENED)

for d in DEVICES:
    d.sent.clear()
found = by_name(bt.find_razer())
check("second poll reuses the working transaction id only",
      {t for t, _, _ in viper.sent} == {0x3F} and {t for t, _, _ in dav3.sent} == {0x1F},
      (viper.sent, dav3.sent))
check("device with no battery isn't asked again", essential.sent == [], essential.sent)

viper.online = False
r = by_name(bt.find_razer()).get("Razer Viper Ultimate")
check("mouse out of range (status 0x04) shows as asleep", r and r.reading.state == "asleep", r)

DEVICES.append(SimRazer(0x007A, "Razer Viper Ultimate (Wired)", 0x3F, level=200, charging=True))
vipers = [f for f in bt.find_razer() if f.name == "Razer Viper Ultimate"]
check("charging on cable replaces the asleep dongle entry",
      len(vipers) == 1 and vipers[0].reading.via == "cable" and vipers[0].reading.charging
      and vipers[0].reading.percent == 78, vipers)

viper.online = True  # dongle answers too now: both entries are "ok"
vipers = [f for f in bt.find_razer() if f.name == "Razer Viper Ultimate"]
check("when cable and dongle both answer, the cable entry (with charging) wins",
      len(vipers) == 1 and vipers[0].reading.via == "cable" and vipers[0].reading.charging, vipers)

DEVICES[:] = [SimRazer(0x00A5, "Razer Viper V2 Pro Dongle", 0x1F, online=False)]
r = by_name(bt.find_razer()).get("Razer Viper V2 Pro")
check("dongle whose mouse is off at first scan still gets an asleep icon",
      r and r.reading.state == "asleep", r)

print()
print("all passed" if not failures else f"{failures} FAILED")
sys.exit(1 if failures else 0)
