# Lamzu Battery Tray

A small Windows tray app that shows the battery level of a **Lamzu Maya X** wireless mouse and warns you before it dies.

Lamzu's own software is a web page (WebHID), so the only way to check battery was to open a browser tab. Windows can't show it either, since the mouse uses a 2.4 GHz dongle, not Bluetooth. This app reads the battery straight from the dongle and keeps it in your taskbar.

<p align="center">
  <img src="docs/icons-dark.png" alt="Tray icon states on a dark taskbar">
  <br>
  <img src="docs/icons-light.png" alt="Tray icon states on a light taskbar">
</p>

## Features

- Battery % in the tray icon, colour-coded (green / amber at 25% / red at 10%)
- Windows notification at **20%, 10% and 5%**: one each, re-armed after charging
- Charging indicator when the mouse is on its cable
- Tells "mouse asleep/off" apart from "battery empty", and keeps showing the last known level
- Re-reads right after the PC wakes from sleep
- Follows light/dark taskbar theme
- Optional *Start with Windows*, single instance, small rotating log

## Install

**Option 1: run from source**

```bat
pip install -r requirements.txt
pythonw lamzu_tray.pyw
```

**Option 2: build an .exe**

```bat
pip install -r requirements.txt pyinstaller
build.bat
```

This produces `dist\LamzuBattery.exe`. Right-click the tray icon → *Start with Windows* to launch it at login.

Quick check without the tray: `python lamzu_tray.pyw --once`

> If reads fail, close the Lamzu web hub tab. While it's connected it can hold the HID interface.

## How it works: reverse-engineering the protocol

Lamzu doesn't document its HID protocol, so I worked it out from the official web driver:

1. **Capture.** The web hub talks to the dongle over WebHID, so I patched `HIDDevice.prototype.sendFeatureReport` / `receiveFeatureReport` in Chrome DevTools to log every report.
2. **Find the value.** The hub showed 98%. One response carried `0x62` (= 98) at a fixed offset, in reply to command `0x83`.
3. **Replay.** I sent the same feature report from Python with `hidapi`, then confirmed the value across wireless, cable/charging and mouse-off states.

| | |
|---|---|
| Vendor ID | `0x373E` |
| Product ID | `0x001E` = 8K dongle (wireless), `0x001C` = mouse on USB cable |
| Interface | usage page `0xFFFF`, usage `0x00` |
| Request | feature report 0 · `00 00 02 02 00 83` + zero padding to 64 bytes |
| Response | byte 0 status · byte 5 echoes `83` · **byte 6 = charging (0/1)** · **byte 7 = battery %** |
| Status | `A0` = pending (dongle still asking the mouse), `A1` = done |

Example response: `a1 00 02 02 00 83 00 61 …`, which means not charging, 97%.

Things I found along the way:

- The dongle relays the request over the air, so the first read is usually `A0`. You have to poll until it flips to `A1` (typically on the 2nd read).
- When the mouse is **off or asleep**, the dongle stays at `A0` forever. It never returns an error or 0%, so a timeout means "unreachable", not "empty".
- On the cable, the mouse enumerates as a **different product ID** (`0x001C`) and answers the same command directly.
- While charging, the reported % jumps up (97 → 100 right after plugging in), presumably because voltage reads higher under charge. The app shows it as approximate (`~`) and doesn't base alerts on it.

## Compatibility

Tested on a **Lamzu Maya X 8K** dongle, Windows 11. Other Lamzu mice that use the same web hub may use the same protocol with different product IDs. If you try one, open an issue with the output of:

```bat
python -c "import hid; [print(hex(d['product_id']), d['product_string'], hex(d['usage_page'])) for d in hid.enumerate(0x373e)]"
```

## Disclaimer

Unofficial project, not affiliated with or endorsed by Lamzu. It only *reads* the battery value and never writes settings to the mouse.

## License

MIT
