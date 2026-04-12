Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
repoRoot = fso.GetAbsolutePathName(fso.BuildPath(scriptDir, ".."))
launcherPath = fso.BuildPath(repoRoot, "host_app\launchers\windows\Launch Drosophila GUI (Windows).vbs")

shell.CurrentDirectory = repoRoot
shell.Run Chr(34) & launcherPath & Chr(34), 0, False
