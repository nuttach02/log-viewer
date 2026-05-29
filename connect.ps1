Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

# ==================================================
# CONFIGURATION
# ==================================================
$NET_IP     = "10.93.40.11"
$SHARE_NAME = "store"
$NET_USER   = "test"
$NET_PASS   = "qcitest"
$DRIVE      = "X:"

# ==================================================
# HELPER: LOG
# ==================================================
function Log {
    param([string]$msg, [string]$color = "White")
    $timestamp = Get-Date -Format "HH:mm:ss"
    $txtLog.SelectionColor = switch ($color) {
        "Green"  { [System.Drawing.Color]::LightGreen }
        "Red"    { [System.Drawing.Color]::Tomato }
        "Yellow" { [System.Drawing.Color]::Gold }
        "Cyan"   { [System.Drawing.Color]::Cyan }
        default  { [System.Drawing.Color]::WhiteSmoke }
    }
    $txtLog.AppendText("[$timestamp] $msg`r`n")
    $txtLog.ScrollToCaret()
    $form.Refresh()
}

# ==================================================
# CONNECT
# ==================================================
function Connect-Server {
    Log "=== Connecting to Server ===" "Cyan"

    Log "[1/5] Removing mapped drive $DRIVE..." "Yellow"
    & net use $DRIVE /delete /y 2>&1 | Out-Null

    Log "[2/5] Removing all SMB sessions to $NET_IP..." "Yellow"
    & net use "\\$NET_IP\*" /delete /y 2>&1 | Out-Null

    Log "[3/5] Clearing cached credentials..." "Yellow"
    & cmdkey /delete:$NET_IP 2>&1 | Out-Null

    Log "[4/5] Injecting credentials..." "Yellow"
    & cmdkey /add:$NET_IP /user:$NET_USER /pass:$NET_PASS 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Log "[ERROR] Failed to add credentials." "Red"
        Set-StatusDisconnected
        return
    }

    Log "[5/5] Mapping $DRIVE -> \\$NET_IP\$SHARE_NAME..." "Yellow"
    & net use $DRIVE "\\$NET_IP\$SHARE_NAME" /persistent:no 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Log "[ERROR] Failed to map drive. Check share name / permissions." "Red"
        Set-StatusDisconnected
        return
    }

    if (-not (Test-Path "$DRIVE\")) {
        Log "[ERROR] Drive $DRIVE not accessible after mapping." "Red"
        Set-StatusDisconnected
        return
    }

    Log "[OK] Connected successfully -> $DRIVE" "Green"
    Set-StatusConnected
}

# ==================================================
# DISCONNECT
# ==================================================
function Disconnect-Server {
    Log "=== Disconnecting ===" "Cyan"
    & net use $DRIVE /delete /y 2>&1 | Out-Null
    & cmdkey /delete:$NET_IP 2>&1 | Out-Null
    Log "[OK] Drive $DRIVE disconnected." "Green"
    Set-StatusDisconnected
}

# ==================================================
# STATUS HELPERS
# ==================================================
function Set-StatusConnected {
    $lblStatus.Text      = "● CONNECTED"
    $lblStatus.ForeColor = [System.Drawing.Color]::LightGreen
    $btnConnect.Enabled    = $false
    $btnDisconnect.Enabled = $true
    $statusLabel.Text    = "Connected  |  $DRIVE -> \\$NET_IP\$SHARE_NAME"
}

function Set-StatusDisconnected {
    $lblStatus.Text      = "● DISCONNECTED"
    $lblStatus.ForeColor = [System.Drawing.Color]::Tomato
    $btnConnect.Enabled    = $true
    $btnDisconnect.Enabled = $false
    $statusLabel.Text    = "Disconnected  |  $NET_IP\$SHARE_NAME"
}

# ==================================================
# BUILD GUI
# ==================================================
$form               = New-Object System.Windows.Forms.Form
$form.Text          = "Server Connection  |  $NET_IP"
$form.Size          = New-Object System.Drawing.Size(560, 420)
$form.StartPosition = "CenterScreen"
$form.BackColor     = [System.Drawing.Color]::FromArgb(18, 22, 30)
$form.ForeColor     = [System.Drawing.Color]::WhiteSmoke
$form.Font          = New-Object System.Drawing.Font("Consolas", 10)
$form.FormBorderStyle = "FixedSingle"
$form.MaximizeBox   = $false

# Title
$pnlTitle           = New-Object System.Windows.Forms.Panel
$pnlTitle.Dock      = "Top"
$pnlTitle.Height    = 48
$pnlTitle.BackColor = [System.Drawing.Color]::FromArgb(0, 122, 204)
$form.Controls.Add($pnlTitle)

$lblTitle           = New-Object System.Windows.Forms.Label
$lblTitle.Text      = "  Server Connection Module"
$lblTitle.Font      = New-Object System.Drawing.Font("Consolas", 13, [System.Drawing.FontStyle]::Bold)
$lblTitle.ForeColor = [System.Drawing.Color]::White
$lblTitle.Dock      = "Fill"
$lblTitle.TextAlign = "MiddleLeft"
$pnlTitle.Controls.Add($lblTitle)

# Info Labels
function Add-InfoRow {
    param($label, $value, $y)
    $l = New-Object System.Windows.Forms.Label
    $l.Text      = $label
    $l.Location  = New-Object System.Drawing.Point(20, $y)
    $l.Size      = New-Object System.Drawing.Size(90, 20)
    $l.ForeColor = [System.Drawing.Color]::FromArgb(120, 160, 200)
    $l.Font      = New-Object System.Drawing.Font("Consolas", 9)
    $form.Controls.Add($l)

    $v = New-Object System.Windows.Forms.Label
    $v.Text      = $value
    $v.Location  = New-Object System.Drawing.Point(115, $y)
    $v.Size      = New-Object System.Drawing.Size(280, 20)
    $v.ForeColor = [System.Drawing.Color]::WhiteSmoke
    $v.Font      = New-Object System.Drawing.Font("Consolas", 9)
    $form.Controls.Add($v)
}

Add-InfoRow "Server"  $NET_IP     62
Add-InfoRow "Share"   $SHARE_NAME 84
Add-InfoRow "User"    $NET_USER   106
Add-InfoRow "Drive"   $DRIVE      128

# Status Indicator
$lblStatus          = New-Object System.Windows.Forms.Label
$lblStatus.Text     = "● DISCONNECTED"
$lblStatus.Location = New-Object System.Drawing.Point(20, 158)
$lblStatus.Size     = New-Object System.Drawing.Size(300, 24)
$lblStatus.Font     = New-Object System.Drawing.Font("Consolas", 11, [System.Drawing.FontStyle]::Bold)
$lblStatus.ForeColor = [System.Drawing.Color]::Tomato
$form.Controls.Add($lblStatus)

# Buttons
function Make-Button {
    param($text, $x, $bg)
    $btn = New-Object System.Windows.Forms.Button
    $btn.Text      = $text
    $btn.Location  = New-Object System.Drawing.Point($x, 192)
    $btn.Size      = New-Object System.Drawing.Size(180, 38)
    $btn.BackColor = $bg
    $btn.ForeColor = [System.Drawing.Color]::White
    $btn.FlatStyle = "Flat"
    $btn.Font      = New-Object System.Drawing.Font("Consolas", 10, [System.Drawing.FontStyle]::Bold)
    $btn.FlatAppearance.BorderSize = 0
    $btn.Cursor    = "Hand"
    $form.Controls.Add($btn)
    return $btn
}

$btnConnect    = Make-Button "[ CONNECT ]"    20  ([System.Drawing.Color]::FromArgb(0, 122, 204))
$btnDisconnect = Make-Button "[ DISCONNECT ]" 214 ([System.Drawing.Color]::FromArgb(160, 60, 60))
$btnDisconnect.Enabled = $false

# Log Box
$lblLog             = New-Object System.Windows.Forms.Label
$lblLog.Text        = "  OUTPUT LOG"
$lblLog.Location    = New-Object System.Drawing.Point(20, 244)
$lblLog.Size        = New-Object System.Drawing.Size(510, 22)
$lblLog.BackColor   = [System.Drawing.Color]::FromArgb(0, 80, 140)
$lblLog.ForeColor   = [System.Drawing.Color]::White
$lblLog.Font        = New-Object System.Drawing.Font("Consolas", 8, [System.Drawing.FontStyle]::Bold)
$lblLog.TextAlign   = "MiddleLeft"
$form.Controls.Add($lblLog)

$txtLog             = New-Object System.Windows.Forms.RichTextBox
$txtLog.Location    = New-Object System.Drawing.Point(20, 266)
$txtLog.Size        = New-Object System.Drawing.Size(510, 100)
$txtLog.BackColor   = [System.Drawing.Color]::FromArgb(12, 16, 22)
$txtLog.ForeColor   = [System.Drawing.Color]::WhiteSmoke
$txtLog.Font        = New-Object System.Drawing.Font("Consolas", 9)
$txtLog.ReadOnly    = $true
$txtLog.BorderStyle = "None"
$txtLog.ScrollBars  = "Vertical"
$form.Controls.Add($txtLog)

# Status Bar
$statusBar          = New-Object System.Windows.Forms.StatusStrip
$statusBar.BackColor = [System.Drawing.Color]::FromArgb(0, 80, 140)
$statusLabel        = New-Object System.Windows.Forms.ToolStripStatusLabel
$statusLabel.Text   = "Disconnected  |  $NET_IP\$SHARE_NAME"
$statusLabel.ForeColor = [System.Drawing.Color]::White
$statusBar.Items.Add($statusLabel) | Out-Null
$form.Controls.Add($statusBar)

# ==================================================
# BUTTON EVENTS
# ==================================================
$btnConnect.Add_Click({ Connect-Server })
$btnDisconnect.Add_Click({ Disconnect-Server })
$form.Add_FormClosing({ if ($btnDisconnect.Enabled) { Disconnect-Server } })

# ==================================================
# STARTUP
# ==================================================
Log "Server Connection Module ready." "Cyan"
Log "Server : \\$NET_IP\$SHARE_NAME  ->  $DRIVE"
Log "Click [CONNECT] to begin." "Yellow"

[System.Windows.Forms.Application]::Run($form)