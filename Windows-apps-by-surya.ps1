# ===========================================
# Software Deployment: Brave, VLC, Telegram, IDM+, AB Download Manager
# ===========================================

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
function Timestamp { (Get-Date).ToString("yyyy-MM-dd HH:mm:ss") }
function Log($msg) { Write-Host "[SOFT-DEPLOY $(Timestamp)] $msg" }

# Admin check
if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Log "Restarting script with Administrator privileges..."
    Start-Process powershell -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`"" -Verb RunAs
    exit
}

# Workspace
$WorkRoot = "$env:TEMP\SoftwareInstaller"
New-Item -ItemType Directory -Force -Path $WorkRoot | Out-Null

# -------------------------------------------
# Brave
# -------------------------------------------
$BraveURL = "https://laptop-updates.brave.com/download/BRV010"
$BraveInstaller = Join-Path $WorkRoot "BraveBrowserSetup.exe"
Log "Downloading Brave Browser..."
Invoke-WebRequest -Uri $BraveURL -OutFile $BraveInstaller -UseBasicParsing
Log "Installing Brave Browser..."
Start-Process -FilePath $BraveInstaller -ArgumentList "/silent /install" -Wait

# -------------------------------------------
# VLC via Winget
# -------------------------------------------
Log "Installing VLC Media Player via Winget..."
Start-Process "winget" -ArgumentList "install --id=VideoLAN.VLC -e --accept-package-agreements --accept-source-agreements --silent" -Wait

# -------------------------------------------
# Telegram via Winget
# -------------------------------------------
Log "Installing Telegram Desktop via Winget..."
Start-Process "winget" -ArgumentList "install --id=Telegram.TelegramDesktop -e --accept-package-agreements --accept-source-agreements --silent" -Wait

# -------------------------------------------
# Internet Download Manager (IDM+)
# -------------------------------------------
Log "Installing Internet Download Manager via Winget..."
Start-Process "winget" -ArgumentList "install --id=Tonec.InternetDownloadManager -e --accept-package-agreements --accept-source-agreements --silent" -Wait

# -------------------------------------------
# AB Downloader (HTTP Downloader)
# -------------------------------------------
$ABURL = "https://github.com/erickutcher/httpdownloader/releases/download/1.0.6.9/httpdownloader-1.0.6.9-x64-setup.exe"
$ABInstaller = Join-Path $WorkRoot "ABDownloader.exe"
Log "Downloading AB Download Manager..."
Invoke-WebRequest -Uri $ABURL -OutFile $ABInstaller -UseBasicParsing
Log "Installing AB Download Manager..."
Start-Process -FilePath $ABInstaller -ArgumentList "/silent" -Wait

# -------------------------------------------
# Cleanup
# -------------------------------------------
Log "Cleaning up temporary workspace..."
Remove-Item -Path $WorkRoot -Recurse -Force

Log "All software installations completed successfully."
