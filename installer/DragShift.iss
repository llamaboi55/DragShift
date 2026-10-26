; Inno Setup script — per-user install, no admin rights needed.
#define AppVersion GetEnv("DS_VERSION")
#if AppVersion == ""
  #define AppVersion "1.1.0+files.9"
#endif
#define AppCompression GetEnv("DS_COMPRESSION")
#if AppCompression == ""
  #define AppCompression "lzma2/ultra64"
#endif

[Setup]
AppId={{6C1E2B0A-7D5F-4E8B-9F3A-D7A5F1C2B3E4}
AppName=DragShift
AppVersion={#AppVersion}
AppPublisher=DragShift
DefaultDirName={localappdata}\Programs\DragShift
DefaultGroupName=DragShift
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=DragShift-Setup
SetupIconFile=..\dragshift\assets\dragshift.ico
UninstallDisplayIcon={app}\DragShift.exe
Compression={#AppCompression}
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
ShowLanguageDialog=yes
UsePreviousLanguage=no

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"

[CustomMessages]
en.AutoStart=Start DragShift when I sign in to Windows
ru.AutoStart=Запускать DragShift при входе в Windows
en.ContextMenu=Add DragShift to the right-click menu for files
ru.ContextMenu=Добавить DragShift в контекстное меню файлов
en.DesktopIcon=Create a desktop shortcut
ru.DesktopIcon=Создать ярлык на рабочем столе
en.LaunchNow=Launch DragShift now
ru.LaunchNow=Запустить DragShift

[Tasks]
Name: "autostart"; Description: "{cm:AutoStart}"; Flags: checkedonce
Name: "contextmenu"; Description: "{cm:ContextMenu}"; Flags: checkedonce
Name: "desktopicon"; Description: "{cm:DesktopIcon}"; Flags: unchecked

[Files]
Source: "..\dist\DragShift\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autoprograms}\DragShift"; Filename: "{app}\DragShift.exe"
Name: "{autodesktop}\DragShift"; Filename: "{app}\DragShift.exe"; Tasks: desktopicon
Name: "{usersendto}\DragShift"; Filename: "{app}\DragShift.exe"

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "DragShift"; ValueData: """{app}\DragShift.exe"" --quiet"; Tasks: autostart; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\*\shell\DragShift"; ValueType: string; ValueName: "MUIVerb"; ValueData: "DragShift…"; Tasks: contextmenu; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\*\shell\DragShift"; ValueType: string; ValueName: "Icon"; ValueData: "{app}\DragShift.exe"; Tasks: contextmenu
Root: HKCU; Subkey: "Software\Classes\*\shell\DragShift"; ValueType: string; ValueName: "MultiSelectModel"; ValueData: "Player"; Tasks: contextmenu
Root: HKCU; Subkey: "Software\Classes\*\shell\DragShift\command"; ValueType: string; ValueName: ""; ValueData: """{app}\DragShift.exe"" ""%1"""; Tasks: contextmenu
Root: HKCU; Subkey: "Software\DragShift"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\DragShift\DragShift"; ValueType: string; ValueName: "language"; ValueData: "{language}"

[Run]
Filename: "{app}\DragShift.exe"; Description: "{cm:LaunchNow}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{cmd}"; Parameters: "/C taskkill /F /IM DragShift.exe"; Flags: runhidden; RunOnceId: "KillDragShift"
