# Cria um atalho para iniciar o ClaudeUsageTray.exe automaticamente no login.
# Uso:
#   powershell -ExecutionPolicy Bypass -File .\install-startup.ps1
# Para desinstalar, apague o atalho da pasta shell:startup (Win+R -> shell:startup).

$ErrorActionPreference = "Stop"

$exe = Join-Path $PSScriptRoot "ClaudeUsageTray.exe"
if (-not (Test-Path $exe)) {
    Write-Error "ClaudeUsageTray.exe nao encontrado. Construa o .exe primeiro (veja o README)."
    exit 1
}

$startup = [Environment]::GetFolderPath("Startup")
$lnkPath = Join-Path $startup "ClaudeUsageTray.lnk"

$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($lnkPath)
$lnk.TargetPath = $exe
$lnk.WorkingDirectory = $PSScriptRoot
$lnk.Description = "Claude Usage Tray widget"
$lnk.Save()

Write-Host "Atalho de inicializacao criado em:"
Write-Host "  $lnkPath"
Write-Host ""
Write-Host "O widget iniciara automaticamente no proximo login."
Write-Host "Para remover, apague esse atalho (Win+R -> shell:startup)."
