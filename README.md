# Hatchling

You heard of Forscan now welcome to Hatchling

**[Download the latest version](https://github.com/HackTheScript001/Hatchling/releases/latest)** - on Windows run `Hatchling-Setup-<version>.exe`.

**Linux and Raspberry Pi OS (64-bit)** - open a terminal and run:

```
curl -fsSL https://raw.githubusercontent.com/HackTheScript001/Hatchling/main/install-linux.sh | bash
```

Add `-s -- --autostart` after `bash` to have it open full screen when the Pi starts. Remove it with
`~/.local/share/hatchling/current/install-linux.sh --uninstall`. USB adapters show as `/dev/ttyUSB0`;
for a Bluetooth adapter, pair it and run `sudo rfcomm bind 0 <adapter address>` once.

![Hatchling](assets/logo.png)

Your own Ford HS/MS-CAN diagnostic and configuration tool for a switched ELM327 USB adapter.
Built for the Fiesta Mk7, with vehicle profiles for Ford, Lincoln, Mercury and Mazda from
1996 onwards, plus "any car mode" (standard OBD-II: fault codes, live data, MOT check, misfires, trips) for every other make.

## What it does

| Page | What you get |
|---|---|
| Connect | Pick the COM port, or **Simulator** to try everything without a car |
| Modules | Scans HS-CAN and MS-CAN, reads VIN / part number / software from each module |
| Fault codes | Reads and clears DTCs across every module, freeze-frame and extended details, HTML report export |
| Live data | Engine OBD values plus any module value you define, trend lines, CSV recording |
| Service | Step-by-step procedures (resets, calibrations, relearns) and module restart |
| Configuration | As-Built read, hex + bit editor, backup / load / write with verify, As-Built import, **feature finder** |
| Features | Switch-style feature toggles per module, hardware checks, guided enabling |
| Dashboard | Four big dials for any live value, full-screen mode |
| History | Every connect, scan, code read, write and procedure per VIN, new/gone codes, notes |

## Install it (Windows)

Run `Hatchling-Setup-<version>.exe`. It installs to `C:\Program Files (x86)\Hatchling`, adds Start menu (and optional
desktop) shortcuts, and appears in **Settings > Apps / Control Panel > Programs** with an
uninstaller. Python, Qt and every library are built in - nothing else to install.

USB adapter drivers: Windows 10/11 normally installs FTDI and CH340 drivers by itself when you
plug the adapter in. The installer's optional **Set up USB adapter drivers** step downloads and
runs them if you've added the direct links at the top of `installer/hatchling.iss`, otherwise it
opens the official driver pages. Hatchling also names the chip next to each COM port and links to
the right driver when no adapter is found.

The installer isn't code-signed, so Windows SmartScreen will warn the first time - choose
**More info > Run anyway**. Uninstalling keeps your `Documents\Hatchling` folder.

## Build the installer (Windows)

1. Right-click the downloaded zip, choose **Properties**, tick **Unblock**, OK - then extract it.
2. Double-click **Build-Hatchling.cmd**.

That's it. It installs Python 3.12 and Inno Setup with Windows' built-in `winget` if you don't
have them, builds Hatchling in a private environment (`.build\`, nothing else on your PC is
touched), makes `dist\Hatchling-Setup-<version>.exe` and offers to install it. The first run
downloads about 250 MB and takes 5-10 minutes; later runs are quicker.

The finished `Hatchling-Setup-<version>.exe` is a single file containing everything - copy or
share it, no Python needed on the other PC.

If it stops, it says why in red (most often: no internet, or winget missing on an old Windows 10 -
then install Python and Inno Setup from python.org and jrsoftware.org and run it again).

**Or let GitHub build it:** push this folder to a GitHub repo; the workflow in
`.github/workflows` builds the installer on every push. Tag a release matching
`core/version.py` (e.g. `v1.0.1`) and set `UPDATE_REPO` in `core/version.py` - Hatchling then
offers updates automatically.

**Just run it from source:** `pip install -r requirements.txt` then `python main.py`. Pick a
Simulator port to try everything without a car.

## Your own files

Built-in definitions ship inside the app. Your own go in `Documents\Hatchling\features`,
`services`, `livedata` and `vehicles` (each has a `_TEMPLATE.json`). A file with the same name
as a built-in one replaces it. Backups, logs, reports and `history.db` live there too.

## Vehicle support

On connect, Hatchling reads the VIN (from the PCM over CAN, or generic OBD-II on older cars),
decodes the maker and - for North American VINs - the model year, then picks a profile.
You can override it on the Connect page.

| Profile | Roughly covers | Buses | What works |
|---|---|---|---|
| Ford HS + MS-CAN | 2004-2019: Fiesta Mk6/7, Focus Mk2/3, Kuga, Mondeo Mk4, Transit, F-150 09-14... | HS, MS | Everything |
| Ford second HS on 3/11 | 2015+: F-150, Mustang S550, Fiesta Mk8, Focus Mk4, Puma, Bronco... | HS, HS 3/11, MS | Everything; some 2020+ gateways limit writes |
| Early Ford CAN (KWP2000) | 2003-2010: early Focus Mk2, Fiesta Mk6, Transit Mk7... | HS, MS | Codes via KWP2000, live data, scans; config varies |
| Ford/Lincoln/Mercury pre-CAN | 1996-2007: Focus Mk1, Mondeo Mk1-3, Ka, Crown Vic... | OBD-II | Engine codes, clear, VIN, live data |
| Mazda HS + MS-CAN | 2004+: Mazda2/3/5/6, CX-5/7/9, MX-5, BT-50 | HS, MS | Everything; use Full scan |
| Generic OBD-II | Any make | OBD-II | Engine codes, clear, VIN, live data |

Profiles are JSON files in `vehicles/` - add your own (buses, year range, makes, priority).
Module addresses aren't fixed per model: Quick scan tries the common Ford addresses on each
bus, Full scan tries every address, so unusual layouts are still found.

Older body modules on pre-CAN Fords (Ford SCP) need a dedicated tool - the switched ELM327
only reaches the engine computer on those cars.

## Using it on the car

- Ignition ON, engine off, **battery charger connected** for any writes.
- When the app asks, flip the adapter switch. It only asks when the switch position actually changes (MS-CAN and HS-CAN on 3/11 share a position).
- Baud: leave it on Automatic - Hatchling finds the speed and remembers it. Wi-Fi adapters: join the adapter's Wi-Fi network, then pick "Wi-Fi adapter".
- Back up every module (Configuration → Read → Save backup) before changing anything. Backups go to `Documents\Hatchling\backups\<VIN>\`.

## Features page

Each feature has a main on/off switch, plus one switch per module it touches - for example
cruise control needs the PCM and the cluster. Flip the main switch to do every module, or
just the module rows you want, then **Apply changes**. Status shows Enabled, Disabled,
Partly on (some modules done) or Needs values.

**Automatic detection:** every module scan also reads the configuration bits each feature
uses (while it's already on that bus, so no extra switch flips) and works out whether the
feature is on, off or partly on. The result shows under the module list, on the Features
page and in History. A feature whose module isn't on the car shows "Module not on this car".

- **Hardware check** runs before anything is switched on. Manual checks ask you to confirm
  (e.g. cruise switches fitted); automatic ones confirm a module answers or a status bit is set.
  If any fail, enabling is blocked and the failures are listed.
- **Enable in other modules** appears when a feature is only partly on. It walks you through
  each remaining module one step at a time - enable it, skip it, or open that exact block in
  the Configuration editor.
- When every module is done you get a **"<feature> is now enabled"** confirmation.

## Writing a feature file

Copy `features/_TEMPLATE.json`, rename it (no leading underscore) and list each module (values below are from the simulator demo, not a real car):

```json
"modules": [
  { "module": "0x7E0", "bus": "HS", "label": "PCM - cruise function",
    "changes": [ { "did": "DE01", "byte": 2, "mask": "0x02", "on": "0x02", "off": "0x00" } ] },
  { "module": "0x720", "bus": "MS", "label": "IPC - cluster display",
    "changes": [ { "did": "DE01", "byte": 1, "mask": "0x10", "on": "0x10", "off": "0x00" } ] }
]
```

- `mask` = which bits this change owns; `on`/`off` = their value when enabled/disabled.
- A module can have several changes (different blocks or bytes).
- `hardware_checks` types: `manual` (user confirms), `module_present` (bus + module must answer),
  `did_value` (bus, module, did, byte, mask, value must match). See `_TEMPLATE.json`.
- `features/cruise-control.json` is ready with the PCM and cluster rows - fill in its `changes`.
- Leave `"verified": false` until you've proven it on your own car - the app makes you tick a confirmation for unverified features.

No real Fiesta feature values are shipped. Bit positions vary by model year and module
version, and a wrong value can disable things, so they need to come from your own work:

1. Read and back up the module.
2. Get the "after" state: a backup from a car that already has the feature, Ford's As-Built
   data for a VIN with the option (motorcraftservice.com), or a careful community source.
3. Configuration → Load backup on your module highlights every byte that differs.
4. Narrow it to the bits that matter, write those, test, then save them as a feature file.

## Getting your factory file

Modules > **Get my factory file** copies your VIN and opens Ford's As-Built page. Sign in with your own free
Ford account, paste the VIN and download the file - Hatchling watches your Downloads folder and imports it
automatically (it checks the VIN matches the connected car). If Ford moves the page, set `"factory_url"` in
`Documents\Hatchling\settings.json`.

## Community

The **Community** page lists the latest feedback, shared features and factory files from the repo (no account
needed to read). **Ask a question** and **Browse discussions** need Discussions turned on in the repo settings.

## Factory As-Built (do this first)

Before changing anything, get your car's factory configuration from Ford: on
motorcraftservice.com open the As-Built data lookup, enter your VIN and download the file.
In Hatchling, **Modules > Import Ford As-Built file** stores it under
`Documents\Hatchling\factory\<VIN>.json` (it checks the VIN matches the connected car).

Hatchling reads Ford's XML download (`.ab`), a saved web page, or text copied from the page -
anything with lines like `7E0-01-01 xxxx xxxx xxxx`. If Ford's file won't import, use the
copy-and-paste route and report the file so the importer can be fixed.

With it imported:
- **Modules > Changes since factory** reads every module in the file and lists each bit that
  differs from the factory. Tick the bits for one feature (e.g. cruise control in the PCM and
  cluster), name it, save - your current values become "on", the factory values "off". Works
  even if you enabled the feature with another tool before you had Hatchling.
- **Configuration > Compare with factory** shows one module's differences in the editor; Write
  changes would put it back to factory.

## Sharing features (the seamless route)

Once one person has learned a feature on their car, everyone with the same car can use it:

1. **On the car that has it:** Modules > Changes since factory, tick the feature's bits, save,
   enable-check it, **Mark as working on my car**, then Features > **Export**. The file carries
   the car model and the part numbers of the modules it was learned on - never the VIN.
2. **On another car:** Features > **Import feature**, run a module scan (it shows Off and
   whether their modules match: same / newer revision / different), fit the hardware, flip the
   switch. Hatchling runs the hardware check, backs up each module, writes and verifies.
3. Once it works, **Mark as working on my car**. Until then it's labelled "Shared from
   another car - not tested on yours yet".

**Compare with donor car** (Modules page) learns a feature without a car that has it: import
your own Ford As-Built file, then pick the Ford file of a car of the same model that came with
the feature. Hatchling lists every difference between the two factory builds - untick trim
or option differences (lights, seats, radio) and keep the feature's bits.

## Feature library (GitHub)

The shared library lives in this repo's `library/` folder. A copy ships inside Hatchling, so
the **Library** page works offline; when online it quietly downloads the latest index from
GitHub at startup (or press **Check for new features**).

**One-time setup on GitHub**
1. Push this folder to a public GitHub repo (e.g. `yourname/hatchling`).
2. Put `owner/name` in `UPDATE_REPO` in `core/version.py` - the library uses the same repo
   (or set `LIBRARY_REPO` in `core/library.py` to use a separate one).
3. Repo **Settings > Actions > General > Workflow permissions**: choose **Read and write**.
4. Create three labels under **Issues > Labels**: `feature-submission`, `confirmation`, `approved`.

**How features get in**
- In Hatchling: Features > **Share to library** (only for features marked working). GitHub opens
  with the "Share a feature" form filled in; the user picks the make and presses **Submit**.
  (Very large features are copied to the clipboard to paste instead.)
- You review the issue. Happy with it? Add the **approved** label. The *Feature library*
  Action checks it (no VINs, sane values, known fields only), saves it to
  `library/features/<make>/`, rebuilds `library/index.json`, replies and closes the issue.
  If it fails the checks, the Action says why and removes the label.
- Only people with write access to the repo can add labels, so only you can approve.

**Confirmations**: after someone marks a library feature as working, Hatchling offers to open a
pre-filled "Works on my car" issue. The Action counts one per GitHub user, grouped by module
part family, and the Library page shows the count.

Issue text is treated as untrusted data: the Action passes it to `tools/library_tool.py` via a
file and never into a shell command. Run `python tools/library_tool.py check` any time to
re-validate every library file.

## Learn a feature from your car

Features page > **Learn a feature from my car**:
1. Name it and take a **before** snapshot (every module, backed up).
2. Change just that one setting with the tool you normally use or at a garage. Using the same adapter?
   Press **Free the adapter** first, and close the other program afterwards.
3. Cycle the ignition and take the **after** snapshot. Hatchling lists exactly what changed - save it as a
   feature and it can switch it on and off from then on.

## Sharing your factory file

Modules > **Share my factory file** sends your car's factory As-Built (VIN removed) plus the options it
came with. Once there are at least two cars with and two without an option, the repo's comparison tool
finds the bits that option uses and proposes a feature in `factory-data/<make>/<model>/proposed/` -
without anyone writing to their car. Maintainers review proposals before they go into the library.

## Feature finder

In Configuration, read a module, then **Load backup** from a car that has the feature (or
import its As-Built, or flip bits yourself). **Make feature from edits** lists every changed
bit - untick anything unrelated (serials, mileage) and save it as a feature: the module's
current values become "off", the highlighted ones "on". Features usually span modules, so
run it on each module and choose **Add to** the same feature. **Compare backups** does the
same from two saved backups (before and after).

## Service procedures

Each procedure is a JSON file in `services/` listing every step, so you can read it before
running it. Step types: `prompt`, `session`, `security`, `routine` (start/stop), `results`
(poll until done), `write`, `reset`, `wait`. See `services/_TEMPLATE.json`. Two simulator demos
are included; real Ford routine IDs need to come from your own research, same as feature bits.
**Restart a module** is built in and works on any module.

## Module live values

Add values to read from any module in `livedata/*.json`:

```json
{ "module": "0x726", "bus": "MS", "did": "0x4028", "name": "Battery charge", "unit": "%", "formula": "A" }
```

Formula letters A, B, C... are the reply bytes; `signed8(A)`, `signed16(A, B)` and `bit(A, n)`
are available. Formulas are checked so only maths can run. Recordings go to `Documents\Hatchling\logs\`.

## Replacing a module

Read the new module, then either **Load backup** (a backup from the old module) or
**Import As-Built** (paste lines like `726-01-01 2A80 40A6 CB` from a factory As-Built record),
check the highlighted differences, and **Write changes**.

## Known limits

- **Not yet tested on a real car.** Everything was tested against the built-in simulator. Start with Modules + Fault codes (read-only), then a Configuration read + backup, before any write.
- Module names from a scan are best guesses based on common Ford addresses; the part numbers it reads back are the real identity.
- Some modules need security access before writes. The hook is in `core/uds.py` (`UDS.security_hook`); no Ford key algorithms are included. If a write is refused with "Security access denied", that module needs one.
- No PATS/key programming or module flashing - those need Ford security access and a proper adapter.
- Hatchling is independently written. It doesn't contain or use any FORScan code or data.
- If writes fail on your adapter's multi-frame send, a better adapter (vLinker FS / OBDLink EX) will be more reliable.

## Code layout

```
core/buses.py     bus definitions (HS, MS, HS on 3/11, generic OBD)
core/elm.py       ELM327 serial, bus switching, ISO-TP parsing and multi-frame send
core/obd.py       generic OBD-II over J1850 / ISO 9141 / KWP / CAN
core/vin.py       VIN decoding (maker, country, model year, check digit)
core/vehicles.py  vehicle profiles and VIN matching
core/uds.py       UDS services + KWP2000 fault-code fallback, security access hook
core/modules.py   module scan + identification
core/asbuilt.py   read, backup, diff, safe write with verify
core/features.py  feature profile loading, status, apply
core/services.py  service procedure loader and step runner
core/livedata.py  module live values, safe formulas, CSV recorder
core/report.py    HTML diagnostic report
core/finder.py    feature finder (bit compare, save as feature)
core/factory.py   Ford factory As-Built import, storage per VIN, compare
core/library.py   shared feature library (bundled copy + GitHub)
tools/library_tool.py  validates and adds library features (used by the GitHub Action)
library/          the shared feature library and its index
core/history.py   car history (SQLite)
core/updates.py   update check against GitHub releases
core/paths.py     built-in vs Documents\\Hatchling files
core/sim.py       fake adapter + car for testing
ui/splash.py      animated startup splash (hatchback traces itself in, wheels pop, pulse sweeps)
ui/logo_shape.py  logo geometry shared by the icon and the splash
installer/        Inno Setup script
tools/build-windows.ps1  one-step Windows build (used by Build-Hatchling.cmd)
ui/app.py         PySide6 desktop app
```
