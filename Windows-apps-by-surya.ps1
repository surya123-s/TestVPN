# ===========================================
# Software Deployment: Brave, VLC, Telegram, IDM, AB Download Manager
# ===========================================

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
function Timestamp { (Get-Date).ToString("yyyy-MM-dd HH:mm:ss") }
function Log($msg) { Write-Host "[DEPLOY $(Timestamp)] $msg" }

# Admin check
if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Log "Restarting script with Administrator privileges..."
    Start-Process powershell -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`"" -Verb RunAs
    exit
}

# Workspace
$WorkRoot = "$env:TEMP\InstallerWork"
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
# Internet Download Manager (IDM – Launcher)
# -------------------------------------------
$IDMURL = "https://mirror2.internetdownloadmanager.com/idman642build42.exe"
$IDMInstaller = Join-Path $WorkRoot "IDM_Setup.exe"

Log "Downloading Internet Download Manager..."
Invoke-WebRequest -Uri $IDMURL -OutFile $IDMInstaller -UseBasicParsing

Log "Launching IDM installer (manual setup required)..."
Start-Process -FilePath $IDMInstaller

# -------------------------------------------
# AB Downloader
# -------------------------------------------
$ABURL = "https://github.com/erickutcher/httpdownloader/releases/download/1.0.6.9/httpdownloader-1.0.6.9-x64-setup.exe"
$ABInstaller = Join-Path $WorkRoot "ABDownloader.exe"
Log "Downloading AB Download Manager..."
Invoke-WebRequest -Uri $ABURL -OutFile $ABInstaller -UseBasicParsing
Log "Installing AB Download Manager..."
Start-Process -FilePath $ABInstaller -ArgumentList "/silent" -Wait

# -------------------------------------------
# Cleanup (except IDM, keep until user finishes install)
# -------------------------------------------
Log "Cleaning up temporary workspace (except IDM installer)..."
Get-ChildItem $WorkRoot | Where-Object { $_.Name -ne "IDM_Setup.exe" } | Remove-Item -Force -Recurse

Log "Deployment completed."
