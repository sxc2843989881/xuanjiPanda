# ============================================================
# MongoDB 8.3 清理脚本（需以管理员身份运行）
# 用途：停止并删除 MongoDB 8.3 服务，删除程序文件，保留 8.0.8
# ============================================================

# 1. 停止 MongoDB 服务
Write-Host "[1/5] 停止 MongoDB 服务..." -ForegroundColor Cyan
try {
    Stop-Service MongoDB -Force -ErrorAction Stop
    Write-Host "  MongoDB 服务已停止" -ForegroundColor Green
} catch {
    Write-Host "  停止服务失败: $_" -ForegroundColor Yellow
    # 尝试强制结束进程
    $procs = Get-Process mongod -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "*8.3*" }
    if ($procs) {
        $procs | Stop-Process -Force
        Write-Host "  已强制结束 8.3 mongod 进程" -ForegroundColor Green
    }
}
Start-Sleep -Seconds 3

# 2. 禁用并删除 MongoDB 服务
Write-Host "[2/5] 删除 MongoDB 服务..." -ForegroundColor Cyan
try {
    sc.exe delete MongoDB
    Write-Host "  MongoDB 服务已删除" -ForegroundColor Green
} catch {
    Write-Host "  删除服务失败: $_" -ForegroundColor Yellow
}

# 3. 验证 27017 端口已释放
Start-Sleep -Seconds 2
Write-Host "[3/5] 验证 27017 端口..." -ForegroundColor Cyan
$port27017 = netstat -ano | findstr ":27017" | findstr "LISTENING"
if ($port27017) {
    Write-Host "  警告: 27017 端口仍被占用!" -ForegroundColor Red
    Write-Host "  $port27017"
} else {
    Write-Host "  27017 端口已释放" -ForegroundColor Green
}

# 4. 验证 27018（PandaAI）仍在运行
Write-Host "[4/5] 验证 27018 端口 (PandaAI)..." -ForegroundColor Cyan
$port27018 = netstat -ano | findstr ":27018" | findstr "LISTENING"
if ($port27018) {
    Write-Host "  27018 端口正常 (PandaAI MongoDB 8.0.8 运行中)" -ForegroundColor Green
} else {
    Write-Host "  警告: 27018 端口未监听! PandaAI MongoDB 可能未运行" -ForegroundColor Red
}

# 5. 删除 MongoDB 8.3 程序文件
Write-Host "[5/5] 删除 MongoDB 8.3 程序文件..." -ForegroundColor Cyan
$mongo83Path = "C:\Program Files\MongoDB\Server\8.3"
if (Test-Path $mongo83Path) {
    try {
        Remove-Item -Path $mongo83Path -Recurse -Force -ErrorAction Stop
        Write-Host "  已删除: $mongo83Path" -ForegroundColor Green
    } catch {
        Write-Host "  删除失败 (可能需要重启后再试): $_" -ForegroundColor Yellow
    }
} else {
    Write-Host "  路径不存在: $mongo83Path" -ForegroundColor Gray
}

# 检查 Server 目录是否为空，如果是则一并删除
$serverPath = "C:\Program Files\MongoDB\Server"
if (Test-Path $serverPath) {
    $remaining = Get-ChildItem $serverPath -ErrorAction SilentlyContinue
    if (-not $remaining) {
        Remove-Item $serverPath -Force -ErrorAction SilentlyContinue
        Write-Host "  已删除空目录: $serverPath" -ForegroundColor Green
    }
}

# 最终状态
Write-Host "`n=== 清理完成 ===" -ForegroundColor Cyan
Write-Host "剩余 mongod 进程:" -ForegroundColor White
Get-Process mongod -ErrorAction SilentlyContinue | Select-Object Id, Path, @{N='MemoryMB';E={[math]::Round($_.WorkingSet64/1MB)}} | Format-Table -AutoSize

Write-Host "端口监听状态:" -ForegroundColor White
netstat -ano | findstr ":27017 :27018" | findstr "LISTENING"

Write-Host "`n提示: PandaAI MongoDB 8.0.8 仍在端口 27018 运行" -ForegroundColor Green
Write-Host "配置文件: c:\XuanJLH\pandaai\mongodb\clean_data\mongod_pandaai.cfg" -ForegroundColor Gray
