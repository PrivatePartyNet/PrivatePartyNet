; PrivatePartyNet installer. Built by tools\build.py with Inno Setup 6.
; Per-user install: no administrator rights for PrivatePartyNet itself. Works fully offline.
; Every prerequisite is packaged here. Each one is checked on the PC first and installed only when missing.
; Installing over an existing PrivatePartyNet is an upgrade: same AppId, same folder, settings and certificate kept.

#ifndef AppVersion
  #define AppVersion "3.0.0"
#endif
#ifndef PrereqDir
  #define PrereqDir "..\installer\prereq"
#endif
#define AppName "PrivatePartyNet"
#define AppExe "PrivatePartyNet.exe"

[Setup]
AppId={{02FC5BBE-A733-4ACF-AE03-9CDDF2637230}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppName}
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
AppMutex=Local\PrivatePartyNet.HostWindow
CloseApplications=yes
RestartApplications=no
OutputDir=..\dist
OutputBaseFilename={#AppName}Setup-{#AppVersion}
SetupIconFile=..\assets\privatepartynet.ico
UninstallDisplayIcon={app}\privatepartynet.ico
; Tells Explorer that icons may have changed, so a copy of PrivatePartyNet that was already installed (or pinned) does not
; keep showing the old icon from Windows' icon cache.
ChangesAssociations=yes
UninstallDisplayName={#AppName} {#AppVersion}
VersionInfoVersion={#AppVersion}.0
VersionInfoProductName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startupicon"; Description: "Start {#AppName} when I sign in to Windows"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "..\dist\PrivatePartyNet\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\assets\privatepartynet.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PrereqDir}\*"; DestDir: "{tmp}\PrivatePartyNetPrereq"; Flags: ignoreversion deleteafterinstall; Check: AnyPrereqNeeded

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; IconFilename: "{app}\privatepartynet.ico"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; IconFilename: "{app}\privatepartynet.ico"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; IconFilename: "{app}\privatepartynet.ico"; Tasks: startupicon

[Run]
; Rebuilds Explorer's icon cache without restarting Explorer, so the new icon shows on shortcuts and the taskbar now.
Filename: "{sys}\ie4uinit.exe"; Parameters: "-show"; Flags: runhidden skipifdoesntexist; StatusMsg: "Refreshing icons..."
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent

; The app's own data (settings, certificate, log, and chat history when it exists) is not in an UninstallDelete
; rule. Uninstalling asks whether to remove it, and a silent uninstall always keeps it. See CurUninstallStepChanged.

[Code]
const
  WebView2Exe = 'MicrosoftEdgeWebView2RuntimeInstallerX64.exe';
  VcExe = 'vc_redist.x64.exe';
  NetExe = 'Microsoft_NET_Framework_4.8_offline.exe';
  WebView2Key = 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  VcKey = 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64';
  NetKey = 'SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full';
  UninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{02FC5BBE-A733-4ACF-AE03-9CDDF2637230}_is1';

// The WebView2 Runtime registers its version under the 32-bit EdgeUpdate key (machine), or the per-user key.
function WebView2Version(): String;
var
  Version: String;
begin
  Version := '';
  if not RegQueryStringValue(HKLM32, WebView2Key, 'pv', Version) or (Version = '') or (Version = '0.0.0.0') then
    if not RegQueryStringValue(HKCU, WebView2Key, 'pv', Version) then
      Version := '';
  if Version = '0.0.0.0' then
    Version := '';
  Result := Version;
end;

function NeedsWebView2(): Boolean;
begin
  Result := WebView2Version() = '';
end;

// The Visual C++ 2015-2022 x64 runtime sets Installed to 1.
function NeedsVc(): Boolean;
var
  Installed: Cardinal;
begin
  if not RegQueryDWordValue(HKLM, VcKey, 'Installed', Installed) then
    Installed := 0;
  Result := Installed <> 1;
end;

// .NET Framework 4.8 is release 528040 or higher in the Full key.
function NeedsNet(): Boolean;
var
  Release: Cardinal;
begin
  if not RegQueryDWordValue(HKLM, NetKey, 'Release', Release) then
    Release := 0;
  Result := Release < 528040;
end;

function AnyPrereqNeeded(): Boolean;
begin
  Result := NeedsWebView2() or NeedsVc() or NeedsNet();
end;

// --- the wizard ------------------------------------------------------------------------------------------------

function InitializeSetup(): Boolean;
var
  Installed: String;
begin
  Result := True;
  Installed := '';
  if RegQueryStringValue(HKCU, UninstallKey, 'DisplayVersion', Installed) and (Installed <> '') then
  begin
    if not WizardSilent then
      MsgBox('{#AppName} ' + Installed + ' is already installed on this PC.' + #13#10 + #13#10 +
             'Setup will update it to version {#AppVersion}. Your settings, your certificate and your log are kept.' + #13#10 +
             'If {#AppName} is open, Setup asks you to close it first.', mbInformation, MB_OK);
  end;
end;

// Run one prerequisite and go on either way. A failure is reported, not hidden, and the app still installs.
procedure RunPrereq(Path, Arguments, Message: String);
var
  ResultCode: Integer;
begin
  if WizardSilent then
    Log(Message)
  else
    WizardForm.StatusLabel.Caption := Message;
  if not Exec(Path, Arguments, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    MsgBox('Setup could not start a prerequisite: ' + Path, mbError, MB_OK);
    Exit;
  end;
  // 0 is success. 3010 and 1641 mean success and a restart is wanted before some features work.
  if (ResultCode = 3010) or (ResultCode = 1641) then
    Log('A restart is wanted after: ' + Path)
  else if ResultCode <> 0 then
    MsgBox('A prerequisite did not install cleanly (exit code ' + IntToStr(ResultCode) + '): ' + Path + #13#10 +
           '{#AppName} is installed. Run the file from the {#AppName} prerequisites folder if it still does not start.',
           mbError, MB_OK);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Dir: String;
begin
  if CurStep = ssPostInstall then
  begin
    Dir := ExpandConstant('{tmp}\PrivatePartyNetPrereq\');
    if NeedsVc() then
      RunPrereq(Dir + VcExe, '/install /quiet /norestart',
        'Installing the Visual C++ runtime (Windows may ask for permission once)...');
    if NeedsNet() then
      RunPrereq(Dir + NetExe, '/q /norestart',
        'Installing .NET Framework 4.8 (Windows may ask for permission once; this can take several minutes)...');
    if NeedsWebView2() then
      RunPrereq(Dir + WebView2Exe, '/silent /install',
        'Installing the Microsoft Edge WebView2 runtime...');
  end;
end;

// Uninstall keeps the data unless asked. A silent uninstall never deletes it.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and not UninstallSilent then
    if MsgBox('Also delete your {#AppName} data (settings, certificate, log and saved chats)?' + #13#10 + #13#10 +
              'Choose No to keep it. It is picked up again if you install {#AppName} later.',
              mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      DelTree(ExpandConstant('{localappdata}\{#AppName}'), True, True, True);
end;
