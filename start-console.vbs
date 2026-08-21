' Dev Console launcher - double-click this file; no console window ever.
' (start.bat flashes a cmd window when double-clicked; .vbs runs via wscript, zero windows)
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "D:\AAAmyPrj\github\myrepos\dev-console"
sh.Run """D:\softwares\miniconda\envs\py12\pythonw.exe"" server.py", 0, False
