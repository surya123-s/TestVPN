# ===========================================
# Software Deployment: Brave, VLC, IDM + Extensions
# ===========================================

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
function Log($msg) { Write-Host "[DEPLOY $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg" }

# Ensure script runs as Administrator
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
# VLC (Winget)
# -------------------------------------------
Log "Installing VLC Media Player..."
Start-Process "winget" -ArgumentList "install --id=VideoLAN.VLC -e --accept-package-agreements --accept-source-agreements --silent" -Wait

# -------------------------------------------
# Internet Download Manager (Interactive Installer)
# -------------------------------------------
$IDMURL = "https://mirror2.internetdownloadmanager.com/idman642build42.exe"
$IDMInstaller = Join-Path $WorkRoot "IDM_Setup.exe"
Log "Downloading Internet Download Manager..."
Invoke-WebRequest -Uri $IDMURL -OutFile $IDMInstaller -UseBasicParsing
Log "Launching IDM installer (manual setup required)..."
Start-Process -FilePath $IDMInstaller

# -------------------------------------------
# Browser Extensions (Chrome + Brave)
# -------------------------------------------
Log "Configuring Browser Extensions..."

$updateUrl = "https://clients2.google.com/service/update2/crx"
$extensions = @(
    "epcnnfbjfcgphgdmggkamkmgojdagdnn", # uBlock Origin
    "mlomiejdfkolichcflejclcbmpeaniij", # Ghostery
    "bgnkhhnnamicmpeenaelnjfhikgbkllg", # Adguard Ad Blocker
    "lokpenepehfdekijkebhpnpcjjpngpnd"  # YouTube Ad Auto Skipper
)

$policyRoots = @(
    "HKLM:\SOFTWARE\Policies\Google\Chrome\ExtensionSettings",
    "HKLM:\SOFTWARE\Policies\BraveSoftware\Brave\ExtensionSettings"
)

foreach ($root in $policyRoots) {
    New-Item -Path $root -Force | Out-Null
    foreach ($id in $extensions) {
        $json = @{ installation_mode = "normal_installed"; update_url = $updateUrl } | ConvertTo-Json -Compress
        New-ItemProperty -Path $root -Name $id -Value $json -PropertyType String -Force | Out-Null
    }
    $defaultJson = @{ installation_mode = "allowed" } | ConvertTo-Json -Compress
    New-ItemProperty -Path $root -Name "*" -Value $defaultJson -PropertyType String -Force | Out-Null
}

Log "Extensions configured successfully."

# -------------------------------------------
# Cleanup
# -------------------------------------------
Remove-Item -Path $WorkRoot -Recurse -Force
Log "Deployment completed."
