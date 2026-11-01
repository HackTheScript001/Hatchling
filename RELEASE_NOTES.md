Beta testing release - thanks for trying Hatchling.

**Download `Hatchling-Setup-1.3.0.exe` below** and run it (it updates your current version in place). Windows may show "Windows protected your PC" because the installer isn't code-signed yet - click **More info**, then **Run anyway**. From 1.3.0 on, Hatchling updates itself.

What's new

Updates
- **Updates install themselves** - Hatchling downloads a new version in the background, checks it against the checksum published with the release, and shows **Restart to update**. One click (and Windows' permission prompt) and it reopens on the new version. If you don't click it, the update installs when you close Hatchling.

Safety before changing anything
- **Battery check** - before any change Hatchling reads the battery. Below 12.2 V it stops and asks you to connect a charger; between 12.2 V and 12.4 V it warns you first.
- **Automatic full backup** - before your first change of each visit, every module's settings are saved to one backup file.
- **Whole-car restore** (Module settings) - back up every module, restore a full backup, or put every module back to your factory file in one go.

Diagnostics
- **MOT check** (Fault codes) - shows the engine's emissions self-tests, the warning light and what still needs finishing before an MOT.
- **Fault code details** - plain meanings for over 200 common codes, the freeze frame (engine conditions when it happened, now on older OBD-only cars too) and what fixed it for other owners. **Share what fixed it** adds your fix for the next person.
- **Replay recordings** (Live data) - open a recording as a graph and read every value at any moment; handy for faults that come and go.
- **Car health report** - a PDF with fault codes, MOT readiness, features and modules.

Community
- **Wish list** (Feature library) - features owners want found, starting with the Fiesta Mk7. Pick one, learn it on your car, share it.
- **Requests and votes** (Community) - request a feature and vote with a 👍; filter for the most wanted, fault code fixes and features being looked for.
- **Fiesta service jobs** - battery reset, tyre sensor relearn, steering angle calibration, service reminder reset and DPF regeneration are listed and can be requested. They can't run yet: Hatchling needs the commands confirmed on a real car first.

Adapters and fixes
- **Wi-Fi adapters** are supported, Bluetooth adapters are labelled, and OBDLink adapters are recognised.
- Modules shows a module on both buses once (e.g. the body module "HS + MS").
- If you have a factory file for a different VIN, Modules says which car it belongs to.
- The Simulator has its own factory file, so you can try **Changes since factory** without a car.

**Use at your own risk.** Hatchling writes to your car's modules. Keep a battery charger connected when making changes, and import your factory file before changing anything.

Feedback welcome: https://github.com/HackTheScript001/Hatchling/issues/new/choose

The source code is attached below as "Source code" (GPL-3.0).
