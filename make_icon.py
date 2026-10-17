"""Writes app.ico (the .exe's icon) using the same drawing code as the tray."""
import importlib.machinery
import importlib.util
import os

here = os.path.dirname(os.path.abspath(__file__))
loader = importlib.machinery.SourceFileLoader("lamzu_tray", os.path.join(here, "lamzu_tray.pyw"))
tray = importlib.util.module_from_spec(importlib.util.spec_from_loader("lamzu_tray", loader))
loader.exec_module(tray)

img = tray.render_battery(80, tray.GREEN, bolt=True)
img.resize((256, 256)).save(os.path.join(here, "app.ico"),
                            sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])
print("wrote app.ico")
