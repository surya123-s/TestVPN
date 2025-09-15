# ===========================================
# Software Deployment: Brave, Vivaldi, VLC, 7-Zip, WinRAR, Notepad++ + Extensions
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
# 7-Zip
# -------------------------------------------
$SevenZipURL = "https://www.7-zip.org/a/7z2408-x64.exe"
$SevenZipInstaller = Join-Path $WorkRoot "7zip.exe"
Log "Downloading 7-Zip..."
Invoke-WebRequest -Uri $SevenZipURL -OutFile $SevenZipInstaller -UseBasicParsing
Log "Installing 7-Zip..."
Start-Process -FilePath $SevenZipInstaller -ArgumentList "/S" -Wait

# -------------------------------------------
# WinRAR
# -------------------------------------------
$WinRARURL = "https://www.rarlab.com/rar/winrar-x64-624.exe"
$WinRARInstaller = Join-Path $WorkRoot "winrar.exe"
Log "Downloading WinRAR..."
Invoke-WebRequest -Uri $WinRARURL -OutFile $WinRARInstaller -UseBasicParsing
Log "Installing WinRAR..."
Start-Process -FilePath $WinRARInstaller -ArgumentList "/S" -Wait

# -------------------------------------------
# Notepad++
# -------------------------------------------
$NotepadURL = "https://github.com/notepad-plus-plus/notepad-plus-plus/releases/download/v8.7.6/npp.8.7.6.Installer.x64.exe"
$NotepadInstaller = Join-Path $WorkRoot "notepadpp.exe"
Log "Downloading Notepad++..."
Invoke-WebRequest -Uri $NotepadURL -OutFile $NotepadInstaller -UseBasicParsing
Log "Installing Notepad++..."
Start-Process -FilePath $NotepadInstaller -ArgumentList "/S" -Wait

# -------------------------------------------
# Browser Extensions (Chrome + Brave + Vivaldi)
# -------------------------------------------
Log "Configuring Browser Extensions..."

$updateUrl = "https://clients2.google.com/service/update2/crx"
$extensions = @(
    "epcnnfbjfcgphgdmggkamkmgojdagdnn", # uBlock Origin
    "bgnkhhnnamicmpeenaelnjfhikgbkllg", # Adguard Ad Blocker
    "hlkenndednhfkekhgcdicdfddnkalmdm", #Cookie Editor
    #"ngpampappnmepgilojfohadhhmbhlaek",  # IDM Integration Module
    #"bbobopahenonfdgjgaleledndnnfhooj", # AB Download Manager
    "ahmpjcflkgiildlgicmcieglgoilbfdp"  # Free Download Manager
)

$policyRoots = @(
    "HKLM:\SOFTWARE\Policies\Google\Chrome\ExtensionSettings",
    "HKLM:\SOFTWARE\Policies\BraveSoftware\Brave\ExtensionSettings"
    #"HKLM:\SOFTWARE\Policies\Vivaldi\ExtensionSettings"
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
