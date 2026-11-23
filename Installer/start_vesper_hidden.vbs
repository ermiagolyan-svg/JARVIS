Set WshShell = CreateObject("WScript.Shell")
WshShell.Run ".venv\Scripts\pythonw.exe Core\vesper_watchdog.py", 0, False
Set WshShell = Nothing
