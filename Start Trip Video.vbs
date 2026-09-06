' Start Trip Video with NO console window - it runs in the system tray instead.
' Double-click this. A small icon appears near the clock (Open / View log / Quit)
' and the app opens in your browser. For a debug run WITH a console, use
' "Start Trip Video.bat" instead.
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
shell.CurrentDirectory = fso.GetParentFolderName(WScript.ScriptFullName)
On Error Resume Next
' pyw.exe is the windowless Python launcher (matches the .bat's "py").
shell.Run "pyw.exe manage.py tray", 0, False
If Err.Number <> 0 Then
  Err.Clear
  shell.Run "pythonw.exe manage.py tray", 0, False
End If
