#define AppVersion "0.5.0"
#ifndef ComponentRoot
#define ComponentRoot "..\build\bundled-components"
#endif
[Setup]
AppId={{39EAF694-2BE4-45C5-B565-559B153C0EC7}
AppName=Prism
AppVersion={#AppVersion}
AppPublisher=Prism
DefaultDirName={localappdata}\Programs\Prism
DefaultGroupName=Prism
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\release
OutputBaseFilename=Prism-Setup-{#AppVersion}-windows-x64
SetupIconFile=..\prism\assets\logo.ico
UninstallDisplayIcon={app}\Prism.exe
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern
ChangesAssociations=yes
LicenseFile=..\LICENSE
CloseApplications=yes
[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked
[Files]
#ifndef AppBuildRoot
#define AppBuildRoot "..\dist\Prism"
#endif
Source: "{#AppBuildRoot}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\LICENSE"; DestDir: "{app}"
Source: "..\NOTICE"; DestDir: "{app}"
Source: "..\THIRD_PARTY.md"; DestDir: "{app}"
Source: "licenses\*"; DestDir: "{app}\licenses"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README.md"; DestDir: "{app}"
Source: "..\README.en.md"; DestDir: "{app}"
Source: "{#ComponentRoot}\*"; DestDir: "{localappdata}\Prism\components"; Excludes: "visual-search\index\openclip-2.26.1-vit-b32-openai-rgb-exif-v1\*,__pycache__\*,*.pyc"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{group}\Prism"; Filename: "{app}\Prism.exe"
Name: "{autodesktop}\Prism"; Filename: "{app}\Prism.exe"; Tasks: desktopicon
[Registry]
Root: HKCU; Subkey: "Software\Classes\.prism"; ValueType: string; ValueData: "Prism.File"; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\Prism.File"; ValueType: string; ValueData: "Prism Project"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\Prism.File\DefaultIcon"; ValueType: string; ValueData: """{app}\Prism.exe"",0"
Root: HKCU; Subkey: "Software\Classes\Prism.File\shell\open\command"; ValueType: string; ValueData: """{app}\Prism.exe"" ""%1"""
[Run]
Filename: "{app}\Prism.exe"; Description: "Launch Prism"; Flags: nowait postinstall skipifsilent

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
var
  Names: TArrayOfString;
  I: Integer;
  Root, ConfigPath, Config: String;
begin
  if CurStep = ssPostInstall then
  begin
    Root := ExpandConstant('{localappdata}\Prism\components');
    Names := ['capture', 'visual-search', 'video-download', 'video-f2', 'video-xhs', 'video-bili'];
    Config := 'home = ' + Root + '\python-base' + #13#10 +
      'include-system-site-packages = false' + #13#10 +
      'version = 3.12' + #13#10;
    for I := 0 to GetArrayLength(Names) - 1 do
    begin
      ConfigPath := Root + '\' + Names[I] + '\runtime\pyvenv.cfg';
      if not SaveStringToFile(ConfigPath, UTF8Encode(Config), False) then
        RaiseException('Cannot configure component runtime: ' + Names[I]);
    end;
  end;
end;
