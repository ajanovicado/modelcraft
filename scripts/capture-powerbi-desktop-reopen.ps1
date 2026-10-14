[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$DesktopExecutable,
    [Parameter(Mandatory = $true)]
    [string]$PbipPath,
    [Parameter(Mandatory = $true)]
    [string]$ScreenshotPath,
    [int]$WindowTimeoutSeconds = 45,
    [int]$RenderSettleSeconds = 8
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
    throw "Desktop reopen capture is Windows-only."
}
if (-not (Test-Path -LiteralPath $DesktopExecutable -PathType Leaf)) {
    throw "Power BI Desktop executable is missing."
}
if (-not (Test-Path -LiteralPath $PbipPath -PathType Leaf)) {
    throw "PBIP entry point is missing."
}

$ScreenshotDirectory = Split-Path -Parent $ScreenshotPath
New-Item -ItemType Directory -Force -Path $ScreenshotDirectory | Out-Null
Add-Type -AssemblyName UIAutomationClient
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class DesktopWindowCapture {
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
}
"@
if ($PbipPath.IndexOf('"') -ge 0 -or $PbipPath.IndexOf("`0") -ge 0) {
    throw "PBIP path contains a character that cannot be passed literally."
}
$QuotedPbipPath = '"' + $PbipPath + '"'
$Started = Start-Process -FilePath $DesktopExecutable -ArgumentList $QuotedPbipPath -PassThru
$WindowObserved = $false
$ObservedWindowHandle = [IntPtr]::Zero
$Deadline = [DateTime]::UtcNow.AddSeconds($WindowTimeoutSeconds)
try {
    while ([DateTime]::UtcNow -lt $Deadline) {
        Start-Sleep -Milliseconds 500
        try {
            $Started.Refresh()
            if ($Started.MainWindowHandle -ne 0) {
                $ObservedWindowHandle = $Started.MainWindowHandle
                $WindowObserved = $true
                break
            }
        }
        catch {
            break
        }
    }
    if (-not $WindowObserved) {
        throw "Power BI Desktop did not expose a main window before the timeout."
    }
    if (-not [DesktopWindowCapture]::SetForegroundWindow($ObservedWindowHandle)) {
        throw "Power BI Desktop could not be placed in the foreground."
    }
    $AutomationRoot = [System.Windows.Automation.AutomationElement]::FromHandle($ObservedWindowHandle)
    $TabCondition = [System.Windows.Automation.PropertyCondition]::new(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::TabItem
    )
    $Tabs = $AutomationRoot.FindAll([System.Windows.Automation.TreeScope]::Descendants, $TabCondition)
    $ExecutiveTab = $null
    foreach ($Tab in $Tabs) {
        if ($Tab.Current.Name -eq "Executive Pulse") { $ExecutiveTab = $Tab; break }
    }
    if ($null -eq $ExecutiveTab) {
        throw "The expected Executive Pulse report-page tab was not exposed to UI Automation."
    }
    $Selection = $ExecutiveTab.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern)
    $Selection.Select()
    if (-not $Selection.Current.IsSelected) {
        throw "Executive Pulse was not the selected report page."
    }
    Start-Sleep -Seconds $RenderSettleSeconds
    Add-Type -AssemblyName System.Drawing
    Add-Type -AssemblyName System.Windows.Forms
    $Rect = New-Object DesktopWindowCapture+RECT
    if (-not [DesktopWindowCapture]::GetWindowRect($ObservedWindowHandle, [ref]$Rect)) {
        throw "Power BI Desktop window bounds could not be read."
    }
    $Width = $Rect.Right - $Rect.Left
    $Height = $Rect.Bottom - $Rect.Top
    if ($Width -le 0 -or $Height -le 0) { throw "Power BI Desktop window bounds are invalid." }
    $Bitmap = New-Object System.Drawing.Bitmap $Width, $Height
    $Graphics = [System.Drawing.Graphics]::FromImage($Bitmap)
    try {
        $Graphics.CopyFromScreen($Rect.Left, $Rect.Top, 0, 0, $Bitmap.Size)
        $Bitmap.Save($ScreenshotPath, [System.Drawing.Imaging.ImageFormat]::Png)
    }
    finally {
        $Graphics.Dispose()
        $Bitmap.Dispose()
    }
}
finally {
    try {
        if (-not $Started.HasExited) {
            Stop-Process -Id $Started.Id -Force -ErrorAction Stop
            $Started.WaitForExit(10000) | Out-Null
        }
    }
    catch {
        # The Python caller independently checks that Desktop is closed before
        # undo. A failure here causes that fail-closed check to abort the run.
    }
}

[pscustomobject]@{
    process_started = $true
    process_id = $Started.Id
    window_observed = $WindowObserved
    foreground_verified = $true
    page_name = "Executive Pulse"
    page_selected = $true
    window_handle = [int64]$ObservedWindowHandle
    screenshot_path = [System.IO.Path]::GetFullPath($ScreenshotPath)
} | ConvertTo-Json -Depth 5 -Compress
