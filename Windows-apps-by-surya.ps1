# ===========================================
# Software Deployment: Brave, Vivaldi, VLC + Download Managers + Utilities + Extensions
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
# Vivaldi (Winget)
# -------------------------------------------
Log "Installing Vivaldi Browser..."
Start-Process "winget" -ArgumentList "install --id=VivaldiTechnologies.Vivaldi -e --accept-package-agreements --accept-source-agreements --silent" -Wait

# -------------------------------------------
# VLC (Winget)
# -------------------------------------------
Log "Installing VLC Media Player..."
Start-Process "winget" -ArgumentList "install --id=VideoLAN.VLC -e --accept-package-agreements --accept-source-agreements --silent" -Wait

# -------------------------------------------
# Download Managers & Utilities
# -------------------------------------------
$apps = @(
    @{ Name="XDM"; URL="https://sourceforge.net/projects/xdman/files/latest/download"; Installer="XDMSetup.exe"; Args="/S" },
    @{ Name="FDM"; URL="https://cdn.freedownloadmanager.org/6/latest/fdm.exe"; Installer="FDMSetup.exe"; Args="/S" },
    @{ Name="IDM"; URL="https://download.internetdownloadmanager.com/idman630build12.exe"; Installer="IDMSetup.exe"; Args="/S" },
    @{ Name="WinRAR"; URL="https://www.rarlab.com/rar/winrar-x64-602.exe"; Installer="WinRARSetup.exe"; Args="/S" },
    @{ Name="7-Zip"; URL="https://www.7-zip.org/a/7z2301-x64.exe"; Installer="7zipSetup.exe"; Args="/S" }
)

foreach ($app in $apps) {
    $installerPath = Join-Path $WorkRoot $app.Installer
    Log "Downloading $($app.Name)..."
    Invoke-WebRequest -Uri $app.URL -OutFile $installerPath -UseBasicParsing
    Log "Installing $($app.Name)..."
    Start-Process -FilePath $installerPath -ArgumentList $app.Args -Wait
}

# -------------------------------------------
# Browser Extensions (Chrome + Brave + Vivaldi)
# -------------------------------------------
Log "Configuring Browser Extensions..."

$updateUrl = "https://clients2.google.com/service/update2/crx"
$extensions = @(
    "epcnnfbjfcgphgdmggkamkmgojdagdnn", # uBlock Origin
    "bgnkhhnnamicmpeenaelnjfhikgbkllg", # Adguard Ad Blocker
    "ngpampappnmepgilojfohadhhmbhlaek"  # IDM Integration Module
    "bbobopahenonfdgjgaleledndnnfhooj"  # AB Download Manager
    "ahmpjcflkgiildlgicmcieglgoilbfdp"  # Free Download Manager
)

$policyRoots = @(
    "HKLM:\SOFTWARE\Policies\Google\Chrome\ExtensionSettings",
    "HKLM:\SOFTWARE\Policies\BraveSoftware\Brave\ExtensionSettings",
    "HKLM:\SOFTWARE\Policies\Vivaldi\ExtensionSettings"
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
