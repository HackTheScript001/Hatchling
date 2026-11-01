What's new in 1.7.0
- Simpler Features page: each feature has one switch. Flip it and Hatchling does the rest - checks the parts are fitted, checks the battery, backs up, writes, and checks it took. The technical details are still there behind "Show the details".
- Will this work on my car? Before anything is written, Hatchling compares the feature with your car: the same module part numbers, the same settings layout, and that your car's current setting is one the feature knows. You get a clear answer: Proven on your modules, Very likely, Untested on your modules (only written if you tick that you accept the risk), or Won't work on your car (never written).
- Create features from my car: a guided way to turn everything that's been changed on your car since the factory into named features - tick the changes that belong together, call it "Cruise control", repeat. Then check each one works, mark it as working and share it.
- New features remember how your modules' settings are laid out, so other owners' cars are checked against it.

What's new in 1.6.2
- Module settings now sits right under Modules in the sidebar.

What's new in 1.6.1
- Garage: the simulator's cars can be added too (marked "simulator"), and the empty garage page no longer has big gaps.

What's new in 1.6.0
- Linux and Raspberry Pi: Hatchling now runs on Linux (Ubuntu, Debian, Mint and others) and on Raspberry Pi OS 64-bit - so a Pi with a small screen makes a portable diagnostic tool. Install with one line (see How to install below). It updates itself like the Windows version.
- Small screens: on a small display (like the Pi's 7 inch screen) Hatchling uses a compact layout and opens full screen. F11 switches full screen on and off; both can be changed in Settings.
- Garage: now starts empty. When you connect to your car, Hatchling asks whether to add it, or press Add this car to my garage. Removing a car can keep or delete its history.

What's new in 1.5.2
- Updates download more reliably: if the connection drops part way, Hatchling carries on from where it stopped and retries by itself, instead of failing the checksum check.

What's new in 1.5.1
- The update window now shows just what's new in the version you're getting, neatly formatted, instead of the raw release text cut off part way.

What's new in 1.5.0

- **Any car mode** - plug Hatchling into any petrol car from 2001 or diesel from 2004 (Vauxhall, VW, Toyota and the rest). It reads the VIN, names the make, and gives you the standard tools every car has: engine fault codes with their freeze frame, live data, gauges, MOT check, misfire counts, trips and the performance timer. The Ford-only pages (features, module settings, service jobs) step out of the way.
- **Mazda** - Mazdas get the full treatment like Fords: module scan with Mazda's own module names, fault codes from every module, module settings, backups and learning features. Mazda doesn't publish factory settings by VIN, so there's no factory file - Hatchling's automatic full backup before your first change is the safety net instead.
- Two more simulators to try it out: a 2012 Mazda3 and a 2016 Vauxhall.

What's new in 1.4.0

New to Hatchling
- **Setup guide** - on first start Hatchling walks you through finding the adapter, connecting, scanning the car, getting the factory file and making the first backup. Run it again from Settings.
- **Settings page** - library, updates, Wi-Fi address, adapter speeds and your files in one place. **Night mode now switches straight away.**
- **Report a problem** (bottom bar or Settings) - sends the activity log, version and adapter to GitHub. Your VIN, Windows user name and email addresses are removed, and you see exactly what's sent first.

In the workshop
- **Misfire counts** (Fault codes) - the engine's own misfire count for each cylinder, so you can spot a weak coil or plug before a fault code appears (CAN cars, about 2008 on).
- **Trips & timing** - live mpg, trip average, distance and fuel used, a history of your trips, and a 0-60 mph / 30-70 mph / 0-100 km/h performance timer (track or private road only).
- **Garage** - every car you've connected to, with a name you choose, its history, backups, factory file and trips.

Behind the scenes
- Every release is now tested first: GitHub opens Hatchling on a Windows machine, clicks through every screen against the simulator and stops the release if anything crashes.
- The build can sign Hatchling once a code-signing certificate is added, which removes the "Windows protected your PC" warning.

What was new in 1.3.1
- **Updating Hatchling window** - click **Update to …** in the bottom bar and Hatchling shows the download progress, then the installer's progress, then reopens on the new version. Nothing opens GitHub. (The download already starts in the background as soon as an update is found, so it's usually ready straight away.)

New in 1.3.0

Updates
- **Updates install themselves** - Hatchling downloads a new version in the background and checks it against the checksum published with the release. If you don't update straight away, it installs when you close Hatchling.

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

How to install
Hatchling updates itself: click Update in the bottom bar.
Windows: download Hatchling-Setup-1.7.0.exe below and run it.
Linux / Raspberry Pi OS (64-bit): open a terminal and run
curl -fsSL https://raw.githubusercontent.com/HackTheScript001/Hatchling/main/install-linux.sh | bash

Windows may show "Windows protected your PC" because the installer isn't code-signed yet - click More info, then Run anyway.

**Use at your own risk.** Hatchling writes to your car's modules. Keep a battery charger connected when making changes, and import your factory file before changing anything.

Feedback welcome: https://github.com/HackTheScript001/Hatchling/issues/new/choose

The source code is attached below as "Source code" (GPL-3.0).
