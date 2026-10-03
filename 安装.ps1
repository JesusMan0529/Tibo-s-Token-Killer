param()
$ErrorActionPreference = 'Stop'
$taskName = 'CodexQuotaWatcher-LiHaoDong'
$watcherPath = Join-Path $PSScriptRoot 'plugins\codex-quota-watcher\scripts\watcher.py'
$pythonPath = (Get-Command python.exe -ErrorAction Stop).Source
$pythonwPath = Join-Path (Split-Path $pythonPath) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonwPath)) { throw '找不到 pythonw.exe，请安装包含 Tkinter 的 Python 3.10 或更高版本。' }
$codexPath = (Get-Command codex.exe -ErrorAction Stop).Source

# Install the plugin with Codex's supported marketplace commands.
& $codexPath plugin marketplace add $PSScriptRoot --json
if ($LASTEXITCODE -ne 0) { throw '本地插件源注册失败。' }
& $codexPath plugin add 'codex-quota-watcher@lihaodong-local' --json
if ($LASTEXITCODE -ne 0) { throw '插件安装失败。' }
& $pythonPath $watcherPath init --codex-path $codexPath
if ($LASTEXITCODE -ne 0) { throw '控制器初始化失败。' }

# InteractiveToken avoids administrator privileges and password storage.
$taskUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$taskAction = New-ScheduledTaskAction -Execute $pythonwPath -Argument ('"' + $watcherPath + '" tick') -WorkingDirectory $PSScriptRoot
$taskTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(15) -RepetitionInterval (New-TimeSpan -Minutes 15)
$taskPrincipal = New-ScheduledTaskPrincipal -UserId $taskUser -LogonType Interactive -RunLevel Limited
$taskSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 24)
Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $taskTrigger -Principal $taskPrincipal -Settings $taskSettings -Description '每 15 分钟检查 Codex 额度并执行人工设定的目标；关闭时不查询、不执行。' -Force | Select-Object TaskName, State
Write-Output '安装完成。默认关闭；请通过面板设置目标任务。'
Write-Output '口令首次生效前，请重启 Codex，并在 Codex CLI 的 /hooks 中检查并信任本插件的 UserPromptSubmit 钩子。'
