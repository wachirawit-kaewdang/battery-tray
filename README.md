# Battery Tray

A small Windows tray app that shows the battery level of your wireless devices, one tray icon each, and warns you before they die. It started as a tray icon for a **Lamzu Maya X** mouse.

Lamzu's own software is a web page (WebHID), so the only way to check battery was to open a browser tab. Windows can't show it either, since the mouse uses a 2.4 GHz dongle, not Bluetooth. This app reads the battery straight from the dongle and keeps it in your taskbar.

Supported sources:

- **Lamzu mice** over the 2.4 GHz dongle or USB cable (vendor protocol, see below)
- **Bluetooth devices** that report battery to Windows: headsets, earbuds, mice, keyboards. These are the same values shown in *Settings → Bluetooth & devices*.
- **Xbox controllers** via XInput. Windows only gives four levels (empty / low / medium / full), so these icons show a bar with no number.
- **Logitech devices** on a Unifying, Lightspeed or Bolt receiver, or plugged in by USB (HID++ 2.0). ⚠️ **Untested:** written from the public protocol documentation without Logitech hardware. See [Compatibility](#compatibility).

An icon appears when a device connects and disappears when it disconnects. If nothing is found, a single `?` icon stays so you still have the menu.

<p align="center">
  <img src="docs/icons-dark.png" alt="Tray icon states on a dark taskbar">
  <br>
  <img src="docs/icons-light.png" alt="Tray icon states on a light taskbar">
</p>

## Features

- Battery % in the tray icon, colour-coded (green / amber at 25% / red at 10%)
- Windows notification at **20%, 10% and 5%**: one each, re-armed after charging
- One icon per device; hover for its name and level
- Charging indicator when the mouse is on its cable or a controller is wired
- Tells "mouse asleep/off" apart from "battery empty", and keeps showing the last known level
- Re-reads right after the PC wakes from sleep
- Follows light/dark taskbar theme
- Optional *Start with Windows*, single instance, small rotating log

## Install

Download from the [**latest release**](https://github.com/wachirawit-kaewdang/battery-tray/releases/latest):

- **`BatteryTray-x.y.z-setup.exe`**: recommended. Installs for your user only (no admin prompt), adds a Start menu entry, can start with Windows, and uninstalls cleanly from *Settings → Apps*.
- **`BatteryTray-x.y.z-portable.zip`**: unzip anywhere and run `BatteryTray.exe`. Nothing is installed.

Windows 10/11, 64-bit.

### First run: "Windows protected your PC"

The app isn't code-signed (signing certificates cost money every year), so Windows SmartScreen warns about it the first time. Click **More info → Run anyway**. You only have to do this once.

If you'd rather check the download first:

- Compare its hash with `SHA256SUMS.txt` from the same release: `Get-FileHash .\BatteryTray-x.y.z-setup.exe` in PowerShell.
- Or skip the binaries and run from source (below). The whole app is one Python file.

If your antivirus flags it, that's a known false positive with apps packaged by PyInstaller. Please open an issue saying which antivirus it was.

### Using it

Right-click any tray icon for *Refresh now*, *Start with Windows*, the log folder and *Quit*. Hover an icon to see the device name and level.

> If Lamzu reads fail, close the Lamzu web hub tab. While it's connected it can hold the HID interface.

### Run from source

```bat
pip install -r requirements.txt
pythonw battery_tray.pyw
```

Quick check without the tray: `python battery_tray.pyw --once` lists every device it can read.

### Build the release yourself

Needs [Inno Setup 6](https://jrsoftware.org/isinfo.php) (`winget install JRSoftware.InnoSetup`).

```bat
pip install -r requirements.txt pyinstaller
build.bat
```

This writes the installer, portable zip and `SHA256SUMS.txt` to `release\`. The version comes from `APP_VERSION` in `battery_tray.pyw`.

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

| Source | Status |
|---|---|
| Lamzu Maya X 8K (dongle + cable) | ✅ Tested |
| Bluetooth devices | ✅ Tested with EDIFIER X3 Lite earbuds |
| Xbox controllers | ⚠️ Untested: standard XInput API |
| Logitech (HID++ 2.0) | ⚠️ Untested: written from documentation, checked only against a simulator |

A Bluetooth device only shows up if it reports its battery to Windows. If *Settings → Bluetooth & devices* shows a %, it will. Other brands' 2.4 GHz mice (Razer, SteelSeries, etc.) each use their own protocol and would need their own reader like the Lamzu one.

### Logitech: help test it

The Logitech reader follows the HID++ 2.0 protocol as documented by Logitech and implemented by [Solaar](https://github.com/pwr-Solaar/Solaar). It reads battery features `0x1004` (unified battery), `0x1000` (battery status) or `0x1001` (voltage, shown as an approximate `~%`). `tests\hidpp_simulator.py` checks the logic against simulated receivers, but it has never run against a real Logitech device. Expected limits:

- Old (pre-2012) HID++ 1.0 devices aren't supported.
- Logitech devices connected by **Bluetooth** go through the Bluetooth source instead, and only show if Windows reports their battery.
- G HUB or Logi Options+ running at the same time should be fine, since replies are tagged, but this is untested.

If you have a Logitech mouse or keyboard, please [open an issue](https://github.com/wachirawit-kaewdang/battery-tray/issues) saying whether it worked. Include the output of `python battery_tray.pyw --once` and the `Logitech …` lines from the log (tray menu → *Open log folder*).

Other Lamzu mice that use the same web hub may use the same protocol with different product IDs. If you try one, [open an issue](https://github.com/wachirawit-kaewdang/battery-tray/issues) with the output of:

```bat
python -c "import hid; [print(hex(d['product_id']), d['product_string'], hex(d['usage_page'])) for d in hid.enumerate(0x373e)]"
```

## Disclaimer

Unofficial project, not affiliated with or endorsed by Lamzu. It only *reads* the battery value and never writes settings to the mouse.

## License

MIT
