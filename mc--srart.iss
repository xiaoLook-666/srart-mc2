; mc--srart 安装包脚本（Inno Setup 6）
#define MyAppName "mc--srart"
#define MyAppVersion "1.2.0"
#define MyAppPublisher "mc--srart"
#define MyAppExeName "mc--srart.exe"

[Setup]
AppId={{7F3C2A91-58D4-4E6B-9A1F-3C7B2D8E5F10}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\mc--srart
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=installer
OutputBaseFilename=mc--srart-setup
SetupIconFile=icon.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesAllowed=x86 x64
ArchitecturesInstallIn64BitMode=x64

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: checkedonce

[Files]
Source: "mc--srart.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "tools\terracotta\*"; DestDir: "{app}\tools\terracotta"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\mc--srart"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\mc--srart"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "运行 mc--srart"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\__pycache__"
