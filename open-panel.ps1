param([ValidateSet('gui', 'dashboard')][string]$View = 'gui')
$ErrorActionPreference = 'Stop'
$pythonPath = (Get-Command python.exe -ErrorAction Stop).Source
$pythonwPath = Join-Path (Split-Path $pythonPath) 'pythonw.exe'
$watcherPath = Join-Path $PSScriptRoot 'plugins\codex-quota-watcher\scripts\watcher.py'
$panelArguments = '"' + $watcherPath + '" ' + $View
Start-Process -FilePath $pythonwPath -ArgumentList $panelArguments -WindowStyle Hidden
