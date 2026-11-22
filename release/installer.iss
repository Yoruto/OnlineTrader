; ISCC /DAppVersion=... /DBundleDir=... /DGitLabProjectId=... release\installer.iss
#ifndef AppVersion
  #error AppVersion is required
#endif
#ifndef BundleDir
  #error BundleDir is required
#endif
#ifndef GitLabProjectId
  #define GitLabProjectId 89047383
#endif
#if FileExists(AddBackslash(BundleDir) + "OnlineTrader-defaults.json") || FileExists(AddBackslash(BundleDir) + "runtime\private\OnlineTrader-defaults.json")
  #define SetupSuffix "-Configured"
#else
  #define SetupSuffix ""
#endif
[Setup]
AppId=OnlineTrader.Managed
AppName=OnlineTrader
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\OnlineTrader
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputBaseFilename=OnlineTrader-{#AppVersion}-Setup{#SetupSuffix}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\bootstrap\OnlineTrader.exe
SetupMutex=OnlineTrader.Managed.Setup

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}\versions\{#AppVersion}"; Excludes: "runtime\*"; Flags: recursesubdirs createallsubdirs onlyifdoesntexist
#if DirExists(AddBackslash(BundleDir) + "runtime")
Source: "{#BundleDir}\runtime\*"; DestDir: "{app}\runtime"; Excludes: "OnlineTrader-parameters.json"; Flags: recursesubdirs createallsubdirs onlyifdoesntexist uninsneveruninstall
; Version defaults advance with the product. OnlineTrader-overrides.json stays
; in the stable runtime directory and is never overwritten by installation.
Source: "{#BundleDir}\runtime\OnlineTrader-parameters.json"; DestDir: "{app}\runtime"; Flags: ignoreversion uninsneveruninstall
#endif
Source: "{#BundleDir}\OnlineTraderCLI.exe"; DestDir: "{app}\bootstrap"; Flags: onlyifdoesntexist
Source: "{#BundleDir}\OnlineTrader.exe"; DestDir: "{app}\bootstrap"; Flags: onlyifdoesntexist
Source: "{#BundleDir}\_internal\*"; DestDir: "{app}\bootstrap\_internal"; Flags: recursesubdirs createallsubdirs onlyifdoesntexist
Source: "{#BundleDir}\OnlineTrader.ini"; DestDir: "{app}"; Flags: onlyifdoesntexist skipifsourcedoesntexist uninsneveruninstall

[Icons]
Name: "{autoprograms}\OnlineTrader"; Filename: "{app}\bootstrap\OnlineTraderCLI.exe"; Parameters: "--managed-launch"; WorkingDir: "{app}"; IconFilename: "{app}\bootstrap\OnlineTrader.exe"; Flags: runminimized

[Code]
procedure InitializeFile(const Name, Contents: String);
var
  Target: String;
begin
  Target := ExpandConstant('{app}\') + Name;
  if not FileExists(Target) then
    if not SaveStringToFile(Target, Contents, False) then
      RaiseException('Cannot initialize ' + Name);
end;

procedure CurStepChanged(Step: TSetupStep);
begin
  if Step = ssPostInstall then
  begin
    InitializeFile('runtime.lock', 'OnlineTrader runtime lease');
    InitializeFile('active.json', '{"version":"{#AppVersion}"}');
    InitializeFile('update-source.json', '{"project_id":{#GitLabProjectId},"channel":"preview"}');
  end;
end;
