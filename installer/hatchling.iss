; Hatchling installer - Inno Setup 6.3 or newer (https://jrsoftware.org/isinfo.php)
; Build with Build-Hatchling.cmd, or let GitHub Actions build it (see .github/workflows/build.yml).

#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif
#define MyAppName "Hatchling"
#define MyAppPublisher "SecondWave IT Systems"
#define MyAppExe "Hatchling.exe"

; ---- USB adapter drivers ---------------------------------------------------------------
; Paste the direct download links for the driver installers here once you've copied them
; from the chip makers' own sites (FTDI: ftdichip.com/drivers/vcp-drivers  WCH: wch-ic.com).
; Left empty, the installer opens those official pages instead of downloading anything.
#define FtdiDriverUrl ""
#define Ch340DriverUrl ""

[Setup]
; AppId identifies Hatchling for upgrades and the Control Panel entry - never change it.
AppId={{94E82534-30A7-490B-B324-66BFC892EA22}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL=https://secondwaveitsystems.cloud
AppSupportURL=https://github.com/HackTheScript001/Hatchling/issues
AppUpdatesURL=https://github.com/HackTheScript001/Hatchling/releases
AppCopyright=(c) SecondWave IT Systems. All rights reserved.
AppContact=liam@secondwaveitsystems.cloud
LicenseFile=..\LICENSE
; Installs to C:\Program Files (x86)\Hatchling ({commonpf32} is the 32-bit Program Files folder on 64-bit Windows)
DefaultDirName={commonpf32}\{#MyAppName}
DefaultGroupName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExe}
UninstallDisplayName={#MyAppName} - Ford-family diagnostics
SetupIconFile=..\assets\hatchling.ico
WizardImageFile=..\assets\wizard.bmp
WizardSmallImageFile=..\assets\wizard-small.bmp
WizardStyle=modern
OutputDir=..\dist
OutputBaseFilename=Hatchling-Setup-{#MyAppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
CloseApplications=yes
VersionInfoVersion={#MyAppVersion}
VersionInfoProductName={#MyAppName}
VersionInfoCompany={#MyAppPublisher}
VersionInfoCopyright=(c) SecondWave IT Systems
VersionInfoDescription={#MyAppName} - a SecondWave IT Systems product

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
SetupWindowTitle=%1 Setup - SecondWave IT Systems
WelcomeLabel2=This will install [name/ver] on your computer.%n%nHatchling is a SecondWave IT Systems product - diagnostics and feature setup for Ford-family vehicles.%n%nIt is recommended that you close all other applications before continuing.
FinishedLabel=Setup has finished installing [name] on your computer.%n%nHatchling is a SecondWave IT Systems product. Thanks for using it.

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "drivers"; Description: "Set up &USB adapter drivers (FTDI and CH340 - only needed if Windows doesn't install them itself)"; GroupDescription: "Adapter:"; Flags: unchecked

[Files]
; The PyInstaller build: Python, Qt, pyserial and all data files are inside - nothing else to install.
Source: "..\dist\Hatchling\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"
Name: "{group}\Hatchling folder (backups, features, logs)"; Filename: "{userdocs}\Hatchling"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Tasks: desktopicon

[Dirs]
Name: "{userdocs}\Hatchling"; Flags: uninsneveruninstall

[Run]
Filename: "{app}\{#MyAppExe}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
; In-app updates run this installer silently with /RELAUNCH=1 so Hatchling reopens by itself afterwards
Filename: "{app}\{#MyAppExe}"; Flags: nowait runasoriginaluser; Check: RelaunchAfterUpdate

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
var
  DownloadPage: TDownloadWizardPage;

procedure InitializeWizard;
begin
  DownloadPage := CreateDownloadPage('Adapter drivers', 'Downloading USB adapter drivers...', nil);
end;

function RelaunchAfterUpdate: Boolean;
begin
  Result := WizardSilent and (ExpandConstant('{param:RELAUNCH|0}') = '1');
end;

function HaveUrls: Boolean;
begin
  Result := ('{#FtdiDriverUrl}' <> '') or ('{#Ch340DriverUrl}' <> '');
end;

procedure OpenDriverPages;
var
  Code: Integer;
begin
  MsgBox('Windows 10 and 11 usually install the adapter driver by themselves when you plug it in.' + #13#10#13#10 +
         'If your adapter doesn''t show up in Hatchling, the official driver pages will open now. ' +
         'Pick the one that matches your adapter (Device Manager shows FTDI or CH340/CH341).',
         mbInformation, MB_OK);
  ShellExec('open', 'https://ftdichip.com/drivers/vcp-drivers/', '', '', SW_SHOWNORMAL, ewNoWait, Code);
  ShellExec('open', 'http://www.wch-ic.com/downloads/CH341SER_ZIP.html', '', '', SW_SHOWNORMAL, ewNoWait, Code);
end;

procedure RunDriver(const FileName, Name: String);
var
  Code: Integer;
begin
  if FileExists(ExpandConstant('{tmp}\' + FileName)) then
    if not Exec(ExpandConstant('{tmp}\' + FileName), '', '', SW_SHOWNORMAL, ewWaitUntilTerminated, Code) then
      MsgBox(Name + ' driver installer could not start (' + SysErrorMessage(Code) + ').', mbError, MB_OK);
end;

procedure InstallDrivers;
begin
  if not HaveUrls then
  begin
    OpenDriverPages;
    Exit;
  end;
  DownloadPage.Clear;
  if '{#FtdiDriverUrl}' <> '' then DownloadPage.Add('{#FtdiDriverUrl}', 'ftdi-driver.exe', '');
  if '{#Ch340DriverUrl}' <> '' then DownloadPage.Add('{#Ch340DriverUrl}', 'ch340-driver.exe', '');
  DownloadPage.Show;
  try
    try
      DownloadPage.Download;
      RunDriver('ftdi-driver.exe', 'FTDI');
      RunDriver('ch340-driver.exe', 'CH340');
    except
      if MsgBox('Driver download failed: ' + GetExceptionMessage + #13#10#13#10 +
                'Open the official driver pages instead?', mbError, MB_YESNO) = IDYES then
        OpenDriverPages;
    end;
  finally
    DownloadPage.Hide;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('drivers') then
    InstallDrivers;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    MsgBox('Hatchling has been removed.' + #13#10#13#10 +
           'Your backups, history and own feature files are still in Documents\Hatchling - ' +
           'delete that folder if you don''t need them.', mbInformation, MB_OK);
end;
