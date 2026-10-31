# Builds Hatchling and its installer on Windows, installing anything that's missing.
# Run it with Build-Hatchling.cmd (double-click) in the main folder.
#
#  1. Python 3.12 and Inno Setup 6 - installed with winget if they aren't already there
#  2. A private Python environment in .build\venv (doesn't touch the rest of your PC)
#  3. Hatchling.exe with PyInstaller, then the installer with Inno Setup
#  4. Offers to run the installer

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

function Say($msg) { Write-Host ""; Write-Host "== $msg" -ForegroundColor Yellow }
function Ok($msg) { Write-Host "   $msg" -ForegroundColor Green }

function Run($exe, [string[]]$argList, $what) {
    & $exe @argList
    if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)." }
}

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machine;$user"
}

function Find-Python {
    foreach ($v in @('3.12', '3.13', '3.11')) {
        try {
            $out = & py "-$v" -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $out) { return ($out | Select-Object -First 1).Trim() }
        } catch { }
    }
    foreach ($c in @("$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
                     "$env:ProgramFiles\Python312\python.exe",
                     "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
                     "$env:ProgramFiles\Python313\python.exe")) {
        if (Test-Path $c) { return $c }
    }
    return $null
}

function Find-Iscc {
    $candidates = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
                    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
                    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe")
    foreach ($c in $candidates) { if ($c -and (Test-Path $c)) { return $c } }
    $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Winget-Install($id, $name, $page) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "$name isn't installed and winget isn't available to install it. Install $name from $page, then run this again."
    }
    Say "Installing $name - this can take a few minutes"
    & winget install --id $id -e --accept-package-agreements --accept-source-agreements --silent
    # -1978335189 = already installed / nothing to upgrade
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne -1978335189) {
        throw "Installing $name failed (winget exit code $LASTEXITCODE). Install it from $page, then run this again."
    }
    Refresh-Path
}

try {
    Write-Host "Hatchling - build and install" -ForegroundColor White

    # ---- version
    $m = Select-String -Path "core\version.py" -Pattern '^VERSION = "(.+)"'
    if (-not $m) { throw "Couldn't read the version from core\version.py." }
    $ver = $m.Matches[0].Groups[1].Value
    Ok "Version $ver"

    # ---- Python
    Say "Checking for Python"
    $py = Find-Python
    if (-not $py) {
        Winget-Install 'Python.Python.3.12' 'Python 3.12' 'https://www.python.org/downloads/'
        $py = Find-Python
        if (-not $py) { throw "Python was installed but can't be found yet. Close this window and run Build-Hatchling.cmd again." }
    }
    Ok "Using $py"

    # ---- Inno Setup
    Say "Checking for Inno Setup"
    $iscc = Find-Iscc
    if (-not $iscc) {
        Winget-Install 'JRSoftware.InnoSetup' 'Inno Setup 6' 'https://jrsoftware.org/isdl.php'
        $iscc = Find-Iscc
        if (-not $iscc) { throw "Inno Setup was installed but can't be found yet. Close this window and run Build-Hatchling.cmd again." }
    }
    Ok "Using $iscc"

    # ---- private Python environment with Hatchling's libraries
    Say "Preparing Hatchling's libraries (first run downloads about 250 MB)"
    $venv = Join-Path $root ".build\venv"
    $vpy = Join-Path $venv "Scripts\python.exe"
    if (-not (Test-Path $vpy)) { Run $py @('-m', 'venv', $venv) 'Creating the build environment' }
    Run $vpy @('-m', 'pip', 'install', '--disable-pip-version-check', '--upgrade', 'pip') 'Updating pip'
    Run $vpy @('-m', 'pip', 'install', '--disable-pip-version-check', '-r', 'requirements.txt', 'pyinstaller') 'Installing libraries'
    Ok "Libraries ready"

    # ---- app
    Say "Building Hatchling.exe"
    Run $vpy @('tools\library_tool.py', 'index') 'Refreshing the feature library'
    $data = @('assets', 'features', 'services', 'livedata', 'vehicles', 'library', 'data')
    $pyiArgs = @('-m', 'PyInstaller', '--noconfirm', '--clean', '--windowed', '--name', 'Hatchling',
                 '--icon', (Join-Path $root 'assets\hatchling.ico'),
                 '--distpath', (Join-Path $root 'dist'), '--workpath', (Join-Path $root '.build\work'),
                 '--specpath', (Join-Path $root '.build'))
    foreach ($d in $data) { $pyiArgs += @('--add-data', ((Join-Path $root $d) + ';' + $d)) }
    $pyiArgs += (Join-Path $root 'main.py')
    Run $vpy $pyiArgs 'Building Hatchling.exe'
    if (-not (Test-Path (Join-Path $root 'dist\Hatchling\Hatchling.exe'))) { throw "Hatchling.exe wasn't created." }
    Ok "dist\Hatchling\Hatchling.exe"

    # ---- installer
    Say "Building the installer"
    Run $iscc @("/DMyAppVersion=$ver", (Join-Path $root 'installer\hatchling.iss')) 'Building the installer'
    $setup = Join-Path $root "dist\Hatchling-Setup-$ver.exe"
    if (-not (Test-Path $setup)) { throw "The installer wasn't created." }
    Ok $setup

    Say "Done"
    Write-Host "   Your installer: $setup" -ForegroundColor White
    Write-Host "   Copy it anywhere or share it - it contains everything Hatchling needs."
    $answer = Read-Host "   Install Hatchling now? (Y/n)"
    if ($answer -notmatch '^(n|no)$') { Start-Process -FilePath $setup }
    else { Start-Process explorer.exe -ArgumentList "/select,`"$setup`"" }
    exit 0
}
catch {
    Write-Host ""
    Write-Host "Build stopped: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Nothing was installed for Hatchling itself. Fix the problem above and run Build-Hatchling.cmd again."
    exit 1
}
