Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
batchPath = fso.BuildPath(scriptDir, "launch_gui.bat")

shell.CurrentDirectory = scriptDir
shell.Run Chr(34) & batchPath & Chr(34), 0, False
