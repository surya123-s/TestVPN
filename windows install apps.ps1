# ===========================================
# Software Deployment: Brave, VLC, Telegram, IDM+, AB Download Manager
# ===========================================

# Enforce TLS 1.2 for secure downloads
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Timestamp { (Get-Date).ToString("yyyy-MM-dd HH:mm:ss") }
function Log($msg) { Write-Host "[SOFT-DEPLOY $(Timestamp)] $msg" }

# Ensure script runs as Administrator
if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Log "Restarting script with Administrator privileges..."
    Start-Process powershell -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`"" -Verb RunAs
    exit
}

# Workspace for temporary downloads
$WorkRoot = "$env:TEMP\SoftwareInstaller"
New-Item -ItemType Directory -Force -Path $WorkRoot | Out-Null

# -------------------------------------------
# Brave Browser
# -------------------------------------------
$BraveURL = "https://laptop-updates.brave.com/download/BRV010"
$BraveInstaller = Join-Path $WorkRoot "BraveBrowserSetup.exe"
Log "Downloading Brave Browser..."
Invoke-WebRequest -Uri $BraveURL -OutFile $BraveInstaller -UseBasicParsing
Log "Installing Brave Browser..."
Start-Process -FilePath $BraveInstaller -ArgumentList "/silent /install" -Wait

# -------------------------------------------
# VLC Media Player
# -------------------------------------------
$VLCURL = "https://get.videolan.org/vlc/3.0.18/win64/vlc-3.0.18-win64.exe"
$VLCInstaller = Join-Path $WorkRoot "VLCSetup.exe"
Log "Downloading VLC Media Player..."
Invoke-WebRequest -Uri $VLCURL -OutFile $VLCInstaller -UseBasicParsing
Log "Installing VLC Media Player..."
Start-Process -FilePath $VLCInstaller -ArgumentList "/S" -Wait

# -------------------------------------------
# Telegram Desktop
# -------------------------------------------
$TelegramURL = "https://telegram.org/dl/desktop/win64"
$TelegramInstaller = Join-Path $WorkRoot "TelegramSetup.exe"
Log "Downloading Telegram Desktop..."
Invoke-WebRequest -Uri $TelegramURL -OutFile $TelegramInstaller -UseBasicParsing
Log "Installing Telegram Desktop..."
Start-Process -FilePath $TelegramInstaller -ArgumentList "/S" -Wait

# -------------------------------------------
# Internet Download Manager (IDM+)
# -------------------------------------------
$IDMURL = "https://mirror2.internetdownloadmanager.com/idman642build11.exe"
$IDMInstaller = Join-Path $WorkRoot "IDMSetup.exe"
Log "Downloading Internet Download Manager..."
Invoke-WebRequest -Uri $IDMURL -OutFile $IDMInstaller -UseBasicParsing
Log "Installing Internet Download Manager..."
Start-Process -FilePath $IDMInstaller -ArgumentList "/silent" -Wait

# Add IDM Chrome/Brave Extension
$IDMExtPath = "$env:ProgramFiles (x86)\Internet Download Manager\IDMGCExt.crx"
if (Test-Path $IDMExtPath) {
    Log "Installing IDM extension to Brave..."
    $BraveExtDir = "$env:LOCALAPPDATA\BraveSoftware\Brave-Browser\User Data\Default\Extensions"
    New-Item -ItemType Directory -Force -Path $BraveExtDir | Out-Null
    Copy-Item $IDMExtPath -Destination $BraveExtDir
}

# -------------------------------------------
# AB Download Manager
# -------------------------------------------
$ABURL = "https://github.com/erickutcher/httpdownloader/releases/download/1.0.6.9/httpdownloader-1.0.6.9-x64-setup.exe"
$ABInstaller = Join-Path $WorkRoot "ABDownloader.exe"
Log "Downloading AB Download Manager..."
Invoke-WebRequest -Uri $ABURL -OutFile $ABInstaller -UseBasicParsing
Log "Installing AB Download Manager..."
Start-Process -FilePath $ABInstaller -ArgumentList "/silent" -Wait

# Add AB Downloader Extension (if available)
# NOTE: AB Downloader has a Chrome extension "Chrono Download Manager" style fork
# You can force install via Chrome Web Store ID
$ABExtID = "lmhkpmbekcpmknklioeibfkpmmfibljd"  # Example placeholder ID
$PolicyKey = "HKLM:\SOFTWARE\Policies\BraveSoftware\Brave\ExtensionInstallForcelist"
New-Item -Path $PolicyKey -Force | Out-Null
Set-ItemProperty -Path $PolicyKey -Name "1" -Value "$ABExtID;https://clients2.google.com/service/update2/crx"

# -------------------------------------------
# Cleanup
# -------------------------------------------
Log "Cleaning up temporary workspace..."
Remove-Item -Path $WorkRoot -Recurse -Force

Log "All software installations completed successfully."
