<#
.SYNOPSIS
    Разворачивает окружение: ffmpeg, виртуальное окружение Python, зависимости панели.

.DESCRIPTION
    Скрипт идемпотентен — повторный запуск ничего не ломает и доустанавливает
    только недостающее.

.PARAMETER SkipGpu
    Не ставить faster-whisper (локальную транскрипцию). Полезно, если решено
    работать только через облачный STT.

.PARAMETER SkipWeb
    Не трогать npm-зависимости панели.
#>
[CmdletBinding()]
param(
    [switch]$SkipGpu,
    [switch]$SkipWeb
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot

function Write-Step($text) { Write-Host "`n=== $text" -ForegroundColor Cyan }
function Write-Ok($text) { Write-Host "  [ok] $text" -ForegroundColor Green }
function Write-Warn2($text) { Write-Host "  [!]  $text" -ForegroundColor Yellow }

# ---------------------------------------------------------------- Python

Write-Step "Ищу подходящий Python"

# ctranslate2 (движок faster-whisper) собирается только под 3.9-3.12,
# поэтому для локальной транскрипции нужен именно такой интерпретатор.
$preferred = @("3.12", "3.11", "3.10")
$pythonExe = $null
$pythonVersion = $null

foreach ($version in $preferred) {
    $candidate = & py -$version -c "import sys; print(sys.executable)" 2>$null
    if ($LASTEXITCODE -eq 0 -and $candidate) {
        $pythonExe = $candidate.Trim()
        $pythonVersion = $version
        break
    }
}

if (-not $pythonExe) {
    $fallback = Get-Command python -ErrorAction SilentlyContinue
    if (-not $fallback) {
        throw "Python не найден. Установи Python 3.12 с python.org и запусти скрипт заново."
    }
    $pythonExe = $fallback.Source
    $pythonVersion = (& $pythonExe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')").Trim()
    Write-Warn2 "Не нашёл Python 3.10-3.12, беру $pythonVersion. Локальная транскрипция, скорее всего, не установится."
    Write-Warn2 "Поставь Python 3.12 рядом — bootstrap подхватит его автоматически."
}
Write-Ok "Python $pythonVersion — $pythonExe"

# ---------------------------------------------------------------- venv

Write-Step "Виртуальное окружение"
$venv = Join-Path $repo ".venv"
$venvPython = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    & $pythonExe -m venv $venv
    Write-Ok "создано в .venv"
} else {
    Write-Ok "уже существует"
}

& $venvPython -m pip install --upgrade pip --quiet
& $venvPython -m pip install -r (Join-Path $repo "server\requirements.txt") --quiet
if ($LASTEXITCODE -ne 0) { throw "не удалось установить зависимости сервера" }
Write-Ok "зависимости сервера установлены"

if (-not $SkipGpu) {
    Write-Step "Локальная транскрипция (faster-whisper)"
    $minor = [int]($pythonVersion.Split(".")[1])
    if ($minor -ge 13) {
        Write-Warn2 "Python $pythonVersion не поддерживается ctranslate2 — пропускаю."
        Write-Warn2 "Транскрипция будет работать через облако (SHORTS_STT_FALLBACK)."
    } else {
        & $venvPython -m pip install -r (Join-Path $repo "server\requirements-gpu.txt") --quiet
        if ($LASTEXITCODE -eq 0) {
            Write-Ok "faster-whisper установлен"
            Write-Warn2 "Для работы на GPU нужны CUDA 12 и cuDNN 9. На NVIDIA-драйвере свежее 550 они обычно уже есть."
        } else {
            Write-Warn2 "Установка не удалась — останется облачный STT."
        }
    }
}

# ---------------------------------------------------------------- ffmpeg

Write-Step "ffmpeg"
$ffmpegDir = Join-Path $repo "tools\ffmpeg"
$ffmpegBin = Join-Path $ffmpegDir "bin\ffmpeg.exe"
$systemFfmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue

if ($systemFfmpeg) {
    Write-Ok "найден в PATH: $($systemFfmpeg.Source)"
} elseif (Test-Path $ffmpegBin) {
    Write-Ok "уже скачан в tools\ffmpeg"
} else {
    $url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
    $zip = Join-Path $env:TEMP "ffmpeg-shorts.zip"
    Write-Host "  скачиваю сборку ffmpeg (~90 МБ)…"
    try {
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
        $extractTo = Join-Path $env:TEMP "ffmpeg-shorts-extract"
        if (Test-Path $extractTo) { Remove-Item $extractTo -Recurse -Force }
        Expand-Archive -Path $zip -DestinationPath $extractTo -Force

        $inner = Get-ChildItem $extractTo -Directory | Select-Object -First 1
        New-Item -ItemType Directory -Force $ffmpegDir | Out-Null
        Copy-Item (Join-Path $inner.FullName "bin") $ffmpegDir -Recurse -Force

        Remove-Item $zip -Force
        Remove-Item $extractTo -Recurse -Force
        Write-Ok "ffmpeg установлен в tools\ffmpeg\bin"
    } catch {
        Write-Warn2 "не удалось скачать ffmpeg: $_"
        Write-Warn2 "Поставь его вручную и пропиши SHORTS_FFMPEG_PATH в .env"
    }
}

# ---------------------------------------------------------------- .env

Write-Step "Файл .env"
$envPath = Join-Path $repo ".env"
$examplePath = Join-Path $repo ".env.example"

if (-not (Test-Path $envPath)) {
    $secret = (& $venvPython -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())").Trim()
    $content = Get-Content $examplePath -Raw -Encoding UTF8
    $content = $content -replace "SHORTS_SECRET_KEY=", "SHORTS_SECRET_KEY=$secret"
    [System.IO.File]::WriteAllText($envPath, $content, (New-Object System.Text.UTF8Encoding $false))
    Write-Ok "создан, ключ шифрования сгенерирован"
    Write-Warn2 "Впиши в .env ключи API: ANTHROPIC_API_KEY, YOUTUBE_CLIENT_ID/SECRET и другие по необходимости."
} else {
    Write-Ok "уже существует — не трогаю"
}

# ---------------------------------------------------------------- панель

if (-not $SkipWeb) {
    Write-Step "Панель"
    $npm = Get-Command npm -ErrorAction SilentlyContinue
    if (-not $npm) {
        Write-Warn2 "npm не найден — панель собрать не получится. Установи Node.js 20+."
    } else {
        Push-Location (Join-Path $repo "web")
        try {
            & npm install --no-audit --no-fund --silent
            if ($LASTEXITCODE -eq 0) { Write-Ok "зависимости панели установлены" }
            else { Write-Warn2 "npm install завершился с ошибкой" }
        } finally {
            Pop-Location
        }
    }
}

# ------------------------------------------- поставщик токенов для YouTube

if (-not $SkipWeb) {
    Write-Step "Поставщик токенов YouTube"
    # Без него YouTube выдаёт ссылки на медиа, но данные по ним не отдаёт:
    # список качеств читается, а загрузка падает с 403. Ставится один раз,
    # дальше run.ps1 поднимает службу сам.
    $potDir = Join-Path $repo "tools\pot-provider"
    if (-not (Test-Path (Join-Path $potDir "package.json"))) {
        Write-Warn2 "нет tools\pot-provider — скачай его из релиза bgutil-ytdlp-pot-provider"
    } elseif (-not (Get-Command node -ErrorAction SilentlyContinue)) {
        Write-Warn2 "node не найден — без него YouTube будет отдавать 403 на загрузку"
    } else {
        Push-Location $potDir
        try {
            & npm ci --no-audit --no-fund --silent
            if ($LASTEXITCODE -ne 0) {
                # Сборка тянет node.lib с nodejs.org, и на нестабильной связи
                # первая попытка часто рвётся на TLS. Вторая обычно проходит.
                Write-Warn2 "первая попытка не удалась, повторяю"
                & npm ci --no-audit --no-fund --silent
            }
            if ($LASTEXITCODE -eq 0) {
                & npx tsc
                if ($LASTEXITCODE -eq 0) { Write-Ok "собран" }
                else { Write-Warn2 "npx tsc завершился с ошибкой" }
            } else {
                Write-Warn2 "npm ci завершился с ошибкой"
            }
        } finally {
            Pop-Location
        }
    }
}

Write-Step "Готово"
Write-Host @"
Дальше:
  1. Открой .env и впиши ключи API.
  2. Запусти всё разом:  .\scripts\run.ps1
     (или по отдельности — сервер, воркер и панель, см. README).
  3. Панель откроется на http://localhost:5173
"@ -ForegroundColor Green
