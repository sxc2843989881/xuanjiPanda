# ============================================================
# MongoDB Cleanup Script (Run as Administrator)
# 1. Remove MongoDB 8.3 service and files
# 2. Register MongoDB 8.0.8 as Windows service (auto start)
# ============================================================

# ====== 1. Stop and delete MongoDB 8.3 service ======
Write-Host "[1/6] Stopping MongoDB 8.3 service..." -ForegroundColor Cyan
try {
    Stop-Service MongoDB -Force -ErrorAction Stop
    Write-Host "  Stopped" -ForegroundColor Green
} catch {
    Write-Host "  Stop failed, trying to kill process..." -ForegroundColor Yellow
    $procs = Get-Process mongod -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "*8.3*" }
    if ($procs) { $procs | Stop-Process -Force }
}
Start-Sleep -Seconds 3

Write-Host "[2/6] Deleting MongoDB 8.3 service..." -ForegroundColor Cyan
sc.exe delete MongoDB
Start-Sleep -Seconds 2

# ====== 2. Register MongoDB 8.0.8 as service ======
Write-Host "[3/6] Registering MongoDB 8.0.8 as Windows service..." -ForegroundColor Cyan
$mongodExe = "c:\XuanJLH\pandaai\mongodb\clean_data\bin\mongod.exe"
$cfgFile = "c:\XuanJLH\pandaai\mongodb\clean_data\mongod_pandaai.cfg"

# Kill current manual 8.0.8 process to free port 27018
$proc808 = Get-Process mongod -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "*clean_data*" }
if ($proc808) {
    Write-Host "  Killing current 8.0.8 process (PID: $($proc808.Id))..." -ForegroundColor Yellow
    $proc808 | Stop-Process -Force
    Start-Sleep -Seconds 3
}

# Register as service (port 27018, auto start)
sc.exe create PandaAI-MongoDB binPath= "`"$mongodExe`" --config `"$cfgFile`" --service" start= auto DisplayName= "PandaAI MongoDB 8.0.8"
Write-Host "  Service registered: PandaAI-MongoDB" -ForegroundColor Green

Write-Host "[4/6] Starting PandaAI-MongoDB service..." -ForegroundColor Cyan
Start-Service PandaAI-MongoDB
Start-Sleep -Seconds 5
$svc = Get-Service PandaAI-MongoDB
Write-Host "  Service status: $($svc.Status)" -ForegroundColor Green

# ====== 3. Verify ports ======
Write-Host "[5/6] Verifying ports..." -ForegroundColor Cyan
$port27017 = netstat -ano | findstr ":27017" | findstr "LISTENING"
$port27018 = netstat -ano | findstr ":27018" | findstr "LISTENING"
if ($port27017) {
    Write-Host "  27017: still in use -> $port27017" -ForegroundColor Red
} else {
    Write-Host "  27017: released (8.3 cleaned)" -ForegroundColor Green
}
if ($port27018) {
    Write-Host "  27018: listening (PandaAI MongoDB running)" -ForegroundColor Green
} else {
    Write-Host "  27018: NOT listening! Check log" -ForegroundColor Red
}

# ====== 4. Delete 8.3 files ======
Write-Host "[6/6] Deleting MongoDB 8.3 program files..." -ForegroundColor Cyan
$mongo83Path = "C:\Program Files\MongoDB\Server\8.3"
if (Test-Path $mongo83Path) {
    try {
        Remove-Item -Path $mongo83Path -Recurse -Force -ErrorAction Stop
        Write-Host "  Deleted: $mongo83Path" -ForegroundColor Green
    } catch {
        Write-Host "  Delete failed, try again after reboot: $_" -ForegroundColor Yellow
    }
}
$serverPath = "C:\Program Files\MongoDB\Server"
if (Test-Path $serverPath) {
    $remaining = Get-ChildItem $serverPath -ErrorAction SilentlyContinue
    if (-not $remaining) {
        Remove-Item $serverPath -Force -ErrorAction SilentlyContinue
    }
}

# ====== Final status ======
Write-Host ""
Write-Host "========== Done ==========" -ForegroundColor Cyan
Write-Host ""
Write-Host "Current MongoDB service:" -ForegroundColor White
Get-Service PandaAI-MongoDB | Select-Object Name, Status, StartType, DisplayName | Format-Table -AutoSize

Write-Host "Current mongod process:" -ForegroundColor White
Get-Process mongod -ErrorAction SilentlyContinue | Select-Object Id, @{N='Path';E={$_.Path}}, @{N='MemoryMB';E={[math]::Round($_.WorkingSet64/1MB)}} | Format-Table -AutoSize

Write-Host "PandaAI MongoDB will auto-start on reboot (port 27018)" -ForegroundColor Green
