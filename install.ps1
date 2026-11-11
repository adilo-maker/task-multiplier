# -*- coding: utf-8 -*-
# Установка плагина "Task Multiplier — Множитель задач" для OrcaSlicer.
#
# Запуск из PowerShell:
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
#
# Скрипт копирует плагин в папку плагинов OrcaSlicer и создаёт служебный
# .install_state.json. Пользовательский config.json не изменяется.

param(
    [string]$OrcaPluginsDir = (Join-Path $env:APPDATA 'OrcaSlicer\orca_plugins')
)

$ErrorActionPreference = 'Stop'

$source = Join-Path $PSScriptRoot 'task_multiplier.py'
if (-not (Test-Path -LiteralPath $source)) {
    throw "Не найден файл плагина: $source"
}

$targetDir = Join-Path $OrcaPluginsDir 'task_multiplier.py'
New-Item -ItemType Directory -Force -Path $targetDir | Out-Null

# OrcaSlicer отказывается загружать папку, где лежит несколько .py файлов.
$extra = Get-ChildItem -LiteralPath $targetDir -Filter '*.py' -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -ne 'task_multiplier.py' }
if ($extra) {
    Write-Warning ("В папке плагина найдены другие .py файлы: {0}. " +
        "Удалите их, иначе OrcaSlicer не сможет выбрать точку входа." -f ($extra.Name -join ', '))
}

Copy-Item -LiteralPath $source -Destination (Join-Path $targetDir 'task_multiplier.py') -Force

$state = [ordered]@{
    capabilities      = @(
        [ordered]@{ 'Task Multiplier' = $true }
    )
    enabled           = $true
    installed_from    = 'local'
    installed_version = '1.0.0'
    permissions       = [ordered]@{
        events         = @()
        fs_read        = @()
        fs_readwrite   = @()
        network_http   = @()
        network_socket = @()
        process        = @()
        threading      = @()
    }
    # Имя информационное; ASCII, чтобы JSON не зависел от кодировки консоли.
    plugin_name       = 'Task Multiplier'
}

$json = $state | ConvertTo-Json -Depth 8
$statePath = Join-Path $targetDir '.install_state.json'
$utf8 = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($statePath, $json + [Environment]::NewLine, $utf8)

Write-Host ''
Write-Host 'Плагин установлен:' -ForegroundColor Green
Write-Host "  $targetDir"
Write-Host ''
Write-Host 'Дальше:'
Write-Host '  1. Перезапустите OrcaSlicer.'
Write-Host '  2. Откройте диалог «Плагины» и включите Task Multiplier.'
Write-Host '  3. Настройте количество деталей и режим сброса/цикла.'
Write-Host ''
Write-Host 'Если OrcaSlicer уже запущен, закройте его перед следующим запуском.'
