# 一键注册开机自启任务（需要管理员权限运行一次）
# 创建两个计划任务：
#   NympheaBot   -> D:\Developed\Nymphea-bot\bot_daemon.py     （bot 守护）
#   NapCatDaemon -> D:\Developed\Nymphea-bot\napcat_daemon.py  （NapCat 守护）
# 均为：开机启动 + 崩溃由守护脚本自拉起 + 隐藏窗口
$ErrorActionPreference = 'Stop'

$py = 'D:\Developed\Nymphea-bot\.venv\Scripts\pythonw.exe'

function Register-BotTask($name, $script) {
    $action = New-ScheduledTaskAction -Execute $py -Argument "`"$script`""
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -RestartCount 999 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
        -Settings $settings -RunLevel Highest -Force | Out-Null
    Write-Host "registered: $name"
}

Register-BotTask 'NympheaBot' 'D:\Developed\Nymphea-bot\bot_daemon.py'
Register-BotTask 'NapCatDaemon' 'D:\Developed\Nymphea-bot\napcat_daemon.py'

Write-Host 'done. tasks will start at next logon (or start manually):'
Write-Host '  Start-ScheduledTask NympheaBot'
Write-Host '  Start-ScheduledTask NapCatDaemon'
