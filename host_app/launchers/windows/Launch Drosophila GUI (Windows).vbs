Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
batchPath = fso.BuildPath(scriptDir, "launch_gui.bat")
repoRoot = fso.GetAbsolutePathName(fso.BuildPath(scriptDir, "..\..\.."))
shell.CurrentDirectory = repoRoot
shell.Run Chr(34) & batchPath & Chr(34), 0, False
