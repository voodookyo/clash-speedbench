# Clash SpeedBench Windows 托盘图标
# 由 SpeedBenchTray.vbs 以无窗口方式拉起（SpeedBench.bat 启动面板后顺带启动）。
# 左键点图标 = 打开面板；右键菜单 = 打开面板 / 退出 SpeedBench。
# 退出走面板的 /api/quit（令牌读 %APPDATA%\ClashSpeedBench\web-token，与
# SwiftBar 插件同一机制）；看门狗每 3 秒探测面板端口，面板消失则自动收图标，
# 不留僵尸托盘图标。
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$PanelUrl  = 'http://127.0.0.1:8950'
$TokenFile = Join-Path $env:APPDATA 'ClashSpeedBench\web-token'
$IconFile  = Join-Path $PSScriptRoot 'speedbench.ico'

# 自定义图标加载失败时退回系统默认图标，保证托盘一定有东西可点
$icon = $null
try { $icon = New-Object System.Drawing.Icon($IconFile) } catch {}
if ($null -eq $icon) { $icon = [System.Drawing.SystemIcons]::Application }

$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Icon = $icon
$notify.Text = 'Clash SpeedBench'
$notify.Visible = $true

$menu = New-Object System.Windows.Forms.ContextMenuStrip
$openItem = $menu.Items.Add('打开面板')
$sepItem  = New-Object System.Windows.Forms.ToolStripSeparator
$null = $menu.Items.Add($sepItem)
$quitItem = $menu.Items.Add('退出 SpeedBench')
$notify.ContextMenuStrip = $menu

function Open-Panel { Start-Process $PanelUrl }

$openItem.add_Click({ Open-Panel })
# NotifyIcon 的 Click 事件不带按钮信息，要用 MouseClick 才能区分左右键
$notify.add_MouseClick({ if ($_.Button -eq 'Left') { Open-Panel } })

$quitItem.add_Click({
    try {
        $token = (Get-Content $TokenFile -Raw).Trim()
        Invoke-RestMethod -Method Post -Uri "$PanelUrl/api/quit" `
            -Headers @{ 'X-SpeedBench-Token' = $token } -TimeoutSec 5 | Out-Null
    } catch {}
    $notify.Visible = $false
    [System.Windows.Forms.Application]::Exit()
})

# 看门狗：连续 2 次探测不到面板（HTTP 层无任何响应）就认为面板已退出
$script:failCount = 0
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 3000
$timer.add_Tick({
    $alive = $false
    try {
        $null = Invoke-WebRequest -UseBasicParsing -Uri $PanelUrl -TimeoutSec 2
        $alive = $true
    } catch {
        # 拿到 HTTP 响应（哪怕 4xx/5xx）也说明面板进程还活着
        $alive = ($null -ne $_.Exception.Response)
    }
    if ($alive) {
        $script:failCount = 0
    } else {
        $script:failCount += 1
        if ($script:failCount -ge 2) {
            $notify.Visible = $false
            [System.Windows.Forms.Application]::Exit()
        }
    }
})
$timer.Start()

# WinForms 消息循环：托盘图标与定时器都靠它驱动，Application.Exit() 时返回
[System.Windows.Forms.Application]::Run()
$notify.Dispose()
$timer.Dispose()
