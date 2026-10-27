# Agenda a coleta diaria de ticks (08:50, dias uteis). Ajuste o caminho do projeto.
$acao = New-ScheduledTaskAction -Execute "uv" -Argument "run robowin coletar" -WorkingDirectory (Get-Location)
$gatilho = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 08:50
Register-ScheduledTask -TaskName "robowin-coleta" -Action $acao -Trigger $gatilho
