<#
.SYNOPSIS
    Запускает API, воркер и панель.

.PARAMETER Prod
    Собрать панель и отдавать её тем же сервером на порту 8000,
    вместо dev-сервера Vite на 5173.

.PARAMETER NoWorker
    Не запускать воркер (например, если он уже крутится в другом окне).

.PARAMETER NoBot
    Не запускать телеграм-бота, даже если токен задан.

.PARAMETER NoPot
    Не запускать поставщик proof-of-origin токенов. Без него YouTube отдаёт
    ссылки на медиа, но данные по ним не выдаёт — загрузка падает с 403.
#>
[CmdletBinding()]
param(
    [switch]$Prod,
    [switch]$NoWorker,
    [switch]$NoBot,
    [switch]$NoPot
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repo ".venv\Scripts\python.exe"
$serverDir = Join-Path $repo "server"

if (-not (Test-Path $venvPython)) {
    throw "Окружение не развёрнуто. Запусти сначала: .\scripts\bootstrap.ps1"
}

$processes = @()

function Start-Component($title, $exe, $argumentList, $workingDirectory) {
    Write-Host "запускаю $title…" -ForegroundColor Cyan
    $process = Start-Process -FilePath $exe -ArgumentList $argumentList `
        -WorkingDirectory $workingDirectory -PassThru
    # Имя нужно, чтобы при падении сказать, что именно упало, а не «код 1».
    $process | Add-Member -NotePropertyName Title -NotePropertyValue $title -Force
    return $process
}

function Assert-PortFree($port, $what) {
    <#
        Забытый с прошлого раза процесс — самая частая причина «ничего не
        запускается». Порт занят, компонент падает на старте, а скрипт глушит
        и остальные, так что до вывода ошибки дело не доходит.
    #>
    $busy = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if (-not $busy) { return }

    $ownerPid = $busy[0].OwningProcess
    $owner = (Get-Process -Id $ownerPid -ErrorAction SilentlyContinue).ProcessName
    throw ("Порт {0} ({1}) уже занят процессом {2} (PID {3}). " -f $port, $what, $owner, $ownerPid) +
          "Скорее всего, он остался с прошлого запуска. Закрой его командой " +
          ("Stop-Process -Id {0}  — и запусти снова." -f $ownerPid)
}

function Test-Ollama {
    <#
        Ollama нужна, только если она выбрана провайдером в .env. Она не
        стартует сама вместе с проектом, и без неё поиск фрагментов падает
        уже после загрузки и распознавания — то есть через много минут работы.
        Дешевле предупредить сразу.
    #>
    $envFile = Join-Path $repo ".env"
    if (-not (Test-Path $envFile)) { return }

    $provider = Select-String -Path $envFile -Pattern '^\s*SHORTS_LLM_PROVIDER\s*=\s*(.+)$' |
                Select-Object -First 1
    if (-not $provider) { return }
    if ($provider.Matches[0].Groups[1].Value.Trim() -ne "ollama") { return }

    $url = "http://localhost:11434"
    $line = Select-String -Path $envFile -Pattern '^\s*OLLAMA_BASE_URL\s*=\s*(.+)$' | Select-Object -First 1
    if ($line) { $url = $line.Matches[0].Groups[1].Value.Trim().TrimEnd('/') }

    try {
        $null = Invoke-WebRequest "$url/api/tags" -UseBasicParsing -TimeoutSec 4
        Write-Host "Ollama отвечает на $url" -ForegroundColor DarkGray
    } catch {
        Write-Host "Ollama не отвечает на $url, а в .env она выбрана провайдером." -ForegroundColor Yellow
        Write-Host "Загрузка и распознавание пройдут, а поиск фрагментов упадёт." -ForegroundColor Yellow
        Write-Host "Запусти её: ollama serve  (или открой приложение Ollama)" -ForegroundColor Yellow
    }
}

function Start-PotProvider {
    <#
        Поставщик proof-of-origin токенов для YouTube. Без него список качеств
        читается, а сама загрузка падает с 403: ссылки выдаются, данные по ним
        нет. yt-dlp ходит к нему на 4416 через плагин bgutil-ytdlp-pot-provider.
    #>
    $busy = Get-NetTCPConnection -LocalPort 4416 -State Listen -ErrorAction SilentlyContinue
    if ($busy) {
        # Служба общая и переживает перезапуск проекта — второй экземпляр не нужен.
        Write-Host "поставщик токенов уже слушает 4416 — переиспользую" -ForegroundColor DarkGray
        return $null
    }

    $potDir = Join-Path $repo "tools\pot-provider"
    $entry = Join-Path $potDir "build\main.js"
    if (-not (Test-Path $entry)) {
        Write-Host "поставщик токенов не собран — YouTube будет отдавать 403 на загрузку." -ForegroundColor Yellow
        Write-Host "Собрать: cd tools\pot-provider  затем  npm ci  и  npx tsc" -ForegroundColor Yellow
        return $null
    }

    $node = (Get-Command node -ErrorAction SilentlyContinue).Source
    if (-not $node) {
        Write-Host "node не найден — поставщик токенов не запустится, ждите 403 от YouTube." -ForegroundColor Yellow
        return $null
    }

    return Start-Component "поставщик токенов" $node @("build\main.js") $potDir
}

try {
    Assert-PortFree 8000 "API"
    Test-Ollama
    if (-not $NoPot) {
        # Пустой результат означает «не запускали»: в $processes его класть
        # нельзя, иначе следящий цикл споткнётся о null вместо процесса.
        $pot = Start-PotProvider
        if ($pot) { $processes += $pot }
    }
    if (-not $NoWorker) {
        $processes += Start-Component "воркер" $venvPython @("-m", "app.queue.worker") $serverDir
        Start-Sleep -Milliseconds 800
    }

    # Бот стартует, только если задан токен: без него процесс всё равно
    # сразу завершится, а скрипт решит, что упал компонент, и погасит остальные.
    if (-not $NoBot) {
        $envFile = Join-Path $repo ".env"
        $hasToken = $false
        if (Test-Path $envFile) {
            $line = Select-String -Path $envFile -Pattern '^\s*SHORTS_TELEGRAM_BOT_TOKEN\s*=\s*(.+)$' |
                    Select-Object -First 1
            if ($line) { $hasToken = -not [string]::IsNullOrWhiteSpace($line.Matches[0].Groups[1].Value) }
        }
        if ($hasToken) {
            $processes += Start-Component "телеграм-бот" $venvPython @("-m", "app.bot") $serverDir
        } else {
            Write-Host "телеграм-бот пропущен: SHORTS_TELEGRAM_BOT_TOKEN не задан" -ForegroundColor DarkGray
        }
    }

    $processes += Start-Component "API" $venvPython `
        @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000") $serverDir

    if ($Prod) {
        Push-Location (Join-Path $repo "web")
        try {
            Write-Host "собираю панель…" -ForegroundColor Cyan
            & npm run build
            if ($LASTEXITCODE -ne 0) { throw "сборка панели не удалась" }
        } finally {
            Pop-Location
        }
        Start-Sleep -Seconds 2
        Write-Host "`nПанель: http://localhost:8000" -ForegroundColor Green
        Start-Process "http://localhost:8000"
    } else {
        # Занятый 5173 — отдельная ловушка: Vite не падает, а молча уходит на
        # 5174, и браузер открывает старый адрес с прошлой сборкой.
        Assert-PortFree 5173 "панель"

        # Get-Command npm отдаёт npm.ps1 — скрипт PowerShell, а не программу.
        # Start-Process запустить его не может и молча ничего не делает: Vite
        # не поднимается, ошибки при этом никакой. Нужен именно npm.cmd.
        $npmCmd = (Get-Command npm.cmd -ErrorAction SilentlyContinue).Source
        if (-not $npmCmd) {
            $fallback = Join-Path (Split-Path (Get-Command npm).Source) "npm.cmd"
            if (Test-Path $fallback) { $npmCmd = $fallback }
        }
        if (-not $npmCmd) { throw "не нашёл npm.cmd — установлен ли Node.js?" }
        $processes += Start-Component "панель (Vite)" $npmCmd @("run", "dev") (Join-Path $repo "web")
        Start-Sleep -Seconds 3
        Write-Host "`nПанель: http://localhost:5173" -ForegroundColor Green
        Write-Host "API:    http://localhost:8000/docs" -ForegroundColor Green
        Write-Host "Не открывай http://localhost:8000 — там лежит собранная копия," -ForegroundColor DarkYellow
        Write-Host "которая обновляется только запуском с ключом -Prod" -ForegroundColor DarkYellow
        Start-Process "http://localhost:5173"
    }

    Write-Host "`nCtrl+C — остановить всё.`n" -ForegroundColor Yellow
    while ($true) {
        Start-Sleep -Seconds 2
        $dead = $processes | Where-Object { $_.HasExited }
        if ($dead) {
            Write-Host ("$($dead[0].Title) завершился с кодом $($dead[0].ExitCode) — останавливаю остальные") -ForegroundColor Red
            break
        }
    }
} finally {
    foreach ($process in $processes) {
        if (-not $process.HasExited) {
            try { $process.Kill($true) } catch { }
        }
    }
    Write-Host "остановлено" -ForegroundColor Yellow
}
