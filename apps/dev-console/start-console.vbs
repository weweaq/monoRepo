' Dev Console launcher - double-click this file; no console window ever.
' (start.bat flashes a cmd window when double-clicked; .vbs runs via wscript, zero windows)
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "D:\AAAmyPrj\github\myrepos\mono\apps\dev-console"
sh.Run """D:\AAAmyPrj\github\myrepos\mono\.venv\Scripts\pythonw.exe"" server.py", 0, False
