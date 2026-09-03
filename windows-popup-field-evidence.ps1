# Run this only in an interactive Windows customer or acceptance session.
# It performs read-only evidence capture for exactly three historical tasks.
# Do not redirect this script's output to a public or shared location.

[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$legacyNames = @(
    'Jax-Watchdog-AtStartup',
    'Jax-Watchdog-Every5Min',
    'jax-watchdog'
)

$records = foreach ($name in $legacyNames) {
    try {
        $task = Get-ScheduledTask -TaskName $name -TaskPath '\' -ErrorAction Stop
        $info = Get-ScheduledTaskInfo -TaskName $name -TaskPath '\' -ErrorAction Stop
        [pscustomobject]@{
            task_name = $task.TaskName
            task_path = $task.TaskPath
            status = 'found'
            state = [string]$task.State
            principal_logon_type = [string]$task.Principal.LogonType
            action_execute = @($task.Actions | ForEach-Object { $_.Execute }) -join ';'
            action_arguments_present = [bool](@($task.Actions | Where-Object { -not [string]::IsNullOrWhiteSpace($_.Arguments) }).Count -gt 0)
            last_task_result = $info.LastTaskResult
            last_run_time_utc = if ($info.LastRunTime) { $info.LastRunTime.ToUniversalTime().ToString('o') } else { $null }
            next_run_time_utc = if ($info.NextRunTime) { $info.NextRunTime.ToUniversalTime().ToString('o') } else { $null }
        }
    } catch [Microsoft.Management.Infrastructure.CimException] {
        [pscustomobject]@{
            task_name = $name
            task_path = '\'
            status = 'not_found'
            state = $null
            principal_logon_type = $null
            action_execute = $null
            action_arguments_present = $false
            last_task_result = $null
            last_run_time_utc = $null
            next_run_time_utc = $null
        }
    } catch {
        [pscustomobject]@{
            task_name = $name
            task_path = '\'
            status = 'query_error'
            state = $null
            principal_logon_type = $_.Exception.GetType().FullName
            action_execute = $null
            action_arguments_present = $false
            last_task_result = $null
            last_run_time_utc = $null
            next_run_time_utc = $null
        }
    }
}

$records | ConvertTo-Json -Depth 3
