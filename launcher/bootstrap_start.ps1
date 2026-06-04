param(
    [ValidateSet("auto", "web")]
    [string]$Mode = "web",
    [switch]$CheckOnly,
    [switch]$SkipInstall,
    [switch]$SkipNode,
    [switch]$RunTests,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$LaunchArgs = @()
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$script:Root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$script:LogDir = Join-Path $script:Root "temp\logs"
$script:LogPath = Join-Path $script:LogDir ("bootstrap-{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
$script:VenvDir = Join-Path $script:Root ".venv"
$script:VenvPython = Join-Path $script:VenvDir "Scripts\python.exe"

function Initialize-Logging {
    New-Item -ItemType Directory -Force -Path $script:LogDir | Out-Null
    "wlwl-ass bootstrap log" | Set-Content -LiteralPath $script:LogPath -Encoding UTF8
    Write-Log "Project root: $script:Root"
    Write-Log "Log file: $script:LogPath"
}

function Write-Log {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Message,
        [string]$Level = "INFO"
    )
    $line = "[{0}] [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Level, $Message
    Write-Host $line
    Add-Content -LiteralPath $script:LogPath -Encoding UTF8 -Value $line
}

function Quote-Arg {
    param([AllowNull()][string]$Value)
    if ($null -eq $Value) {
        return '""'
    }
    if ($Value -match '[\s"]') {
        return '"' + ($Value -replace '"', '\"') + '"'
    }
    return $Value
}

function Format-CommandLine {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @()
    )
    $parts = @((Quote-Arg $FilePath))
    foreach ($arg in $Arguments) {
        $parts += (Quote-Arg $arg)
    }
    return ($parts -join " ")
}

function Invoke-CaptureCommand {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @(),
        [string]$WorkingDirectory = $script:Root
    )
    Push-Location -LiteralPath $WorkingDirectory
    try {
        $output = & $FilePath @Arguments 2>&1
        $exitCode = $LASTEXITCODE
        if ($null -eq $exitCode) {
            $exitCode = 0
        }
        return [pscustomobject]@{
            ExitCode = [int]$exitCode
            Output = @($output)
        }
    } catch {
        return [pscustomobject]@{
            ExitCode = 127
            Output = @($_.Exception.Message)
        }
    } finally {
        Pop-Location
    }
}

function Invoke-LoggedCommand {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @(),
        [string]$WorkingDirectory = $script:Root,
        [switch]$AllowFailure
    )
    if (-not (Test-Path -LiteralPath $WorkingDirectory)) {
        throw "Working directory does not exist: $WorkingDirectory"
    }

    $commandLine = Format-CommandLine -FilePath $FilePath -Arguments $Arguments
    Write-Log "RUN ($WorkingDirectory): $commandLine"
    Push-Location -LiteralPath $WorkingDirectory
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = & $FilePath @Arguments 2>&1
        $exitCode = $LASTEXITCODE
        if ($null -eq $exitCode) {
            $exitCode = 0
        }
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
        Pop-Location
    }

    foreach ($line in @($output)) {
        if ($line -is [System.Management.Automation.ErrorRecord]) {
            Write-Log ($line.Exception.Message) "CMD"
        } else {
            Write-Log ($line.ToString()) "CMD"
        }
    }

    Write-Log "EXIT ${exitCode}: $commandLine"
    if (($exitCode -ne 0) -and (-not $AllowFailure)) {
        throw "Command failed with exit code ${exitCode}: $commandLine"
    }
    return [int]$exitCode
}

function Assert-ProjectLayout {
    $required = @(
        "pyproject.toml",
        "launch.pyw",
        "agentmain.py",
        "launcher\api_server.py",
        "gui\package.json"
    )
    foreach ($relativePath in $required) {
        $path = Join-Path $script:Root $relativePath
        if (-not (Test-Path -LiteralPath $path)) {
            throw "Required project file is missing: $relativePath"
        }
    }
    Write-Log "Project layout check passed."
}

function Test-PythonCandidate {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @(),
        [string]$Name = ""
    )
    $probe = 'import sys; print(sys.executable); print(sys.version_info.major); print(sys.version_info.minor); print(sys.version_info.micro)'
    $result = Invoke-CaptureCommand -FilePath $FilePath -Arguments ($Arguments + @("-c", $probe))
    if ($result.ExitCode -ne 0) {
        Write-Log "Python candidate unavailable: $(Format-CommandLine -FilePath $FilePath -Arguments $Arguments)" "WARN"
        return $null
    }

    $lines = @($result.Output | ForEach-Object { $_.ToString().Trim() } | Where-Object { $_.Length -gt 0 })
    if ($lines.Count -lt 4) {
        Write-Log "Python candidate returned incomplete probe output: $(Format-CommandLine -FilePath $FilePath -Arguments $Arguments)" "WARN"
        return $null
    }

    try {
        $executable = [string]$lines[$lines.Count - 4]
        $major = [int]$lines[$lines.Count - 3]
        $minor = [int]$lines[$lines.Count - 2]
        $micro = [int]$lines[$lines.Count - 1]
        $version = [System.Version]::new($major, $minor, $micro)
    } catch {
        Write-Log "Could not parse Python probe output '$($lines -join ' | ')': $($_.Exception.Message)" "WARN"
        return $null
    }

    if (($major -lt 3) -or (($major -eq 3) -and ($minor -lt 10)) -or (($major -eq 3) -and ($minor -ge 14))) {
        Write-Log "Rejected Python $version at $executable. Required: >=3.10,<3.14." "WARN"
        return $null
    }

    if (-not $Name) {
        $Name = Format-CommandLine -FilePath $FilePath -Arguments $Arguments
    }
    return [pscustomobject]@{
        FilePath = $FilePath
        Arguments = @($Arguments)
        Executable = $executable
        Version = $version
        Name = $Name
    }
}

function Find-CompatiblePython {
    param([switch]$IncludeVenv)
    $candidates = @()
    if ($IncludeVenv -and (Test-Path -LiteralPath $script:VenvPython)) {
        $candidates += [pscustomobject]@{ FilePath = $script:VenvPython; Arguments = @(); Name = ".venv" }
    }
    $candidates += [pscustomobject]@{ FilePath = "py"; Arguments = @("-3.12"); Name = "py -3.12" }
    $candidates += [pscustomobject]@{ FilePath = "py"; Arguments = @("-3.11"); Name = "py -3.11" }
    $candidates += [pscustomobject]@{ FilePath = "py"; Arguments = @("-3.10"); Name = "py -3.10" }
    $candidates += [pscustomobject]@{ FilePath = "py"; Arguments = @("-3"); Name = "py -3" }
    $candidates += [pscustomobject]@{ FilePath = "python"; Arguments = @(); Name = "python" }
    $candidates += [pscustomobject]@{ FilePath = "python3"; Arguments = @(); Name = "python3" }

    foreach ($candidate in $candidates) {
        $probe = Test-PythonCandidate -FilePath $candidate.FilePath -Arguments $candidate.Arguments -Name $candidate.Name
        if ($null -ne $probe) {
            Write-Log "Using Python $($probe.Version) from $($probe.Executable) via $($probe.Name)."
            return $probe
        }
    }
    return $null
}

function Ensure-Venv {
    if (Test-Path -LiteralPath $script:VenvPython) {
        $existing = Test-PythonCandidate -FilePath $script:VenvPython -Name ".venv"
        if ($null -eq $existing) {
            throw "Existing .venv Python is missing or incompatible. Move or delete '$script:VenvDir', then rerun."
        }
        Write-Log "Reusing project virtual environment: $script:VenvPython"
        Write-Output $script:VenvPython
        return
    }

    $basePython = Find-CompatiblePython
    if ($null -eq $basePython) {
        throw "No compatible Python found. Install Python 3.10, 3.11, 3.12, or 3.13 and ensure it is on PATH."
    }

    Write-Log "Creating project virtual environment: $script:VenvDir"
    New-Item -ItemType Directory -Force -Path $script:VenvDir | Out-Null
    $null = Invoke-LoggedCommand -FilePath $basePython.FilePath -Arguments ($basePython.Arguments + @("-m", "venv", $script:VenvDir))

    $venv = Test-PythonCandidate -FilePath $script:VenvPython -Name ".venv"
    if ($null -eq $venv) {
        throw "Virtual environment was created, but '$script:VenvPython' is not usable."
    }
    Write-Log "Created .venv with Python $($venv.Version)."
    Write-Output $script:VenvPython
}

function Ensure-EnvFile {
    $envFile = Join-Path $script:Root ".env"
    if (Test-Path -LiteralPath $envFile) {
        Write-Log ".env already exists; preserving existing configuration."
        return
    }

    $example = Join-Path $script:Root ".env.example"
    if (Test-Path -LiteralPath $example) {
        Copy-Item -LiteralPath $example -Destination $envFile -ErrorAction Stop
        Write-Log "Created .env from .env.example. Fill in API keys when needed."
    } else {
        "# wlwl-ass local config" | Set-Content -LiteralPath $envFile -Encoding UTF8
        Write-Log "Created minimal .env because .env.example was not found."
    }
}

function Ensure-RuntimeDirs {
    $dirs = @("temp", "temp\logs", "temp\activity")
    foreach ($relativePath in $dirs) {
        $path = Join-Path $script:Root $relativePath
        New-Item -ItemType Directory -Force -Path $path | Out-Null
    }
    Write-Log "Runtime directories are ready."
}

function Install-PythonDependencies {
    param([string]$PythonExe)
    if ($SkipInstall) {
        Write-Log "Skipping Python dependency installation because -SkipInstall was specified." "WARN"
        return
    }

    $pipCheck = Invoke-LoggedCommand -FilePath $PythonExe -Arguments @("-m", "pip", "--version") -AllowFailure
    if ($pipCheck -ne 0) {
        Write-Log "pip is unavailable in .venv; trying ensurepip." "WARN"
        Invoke-LoggedCommand -FilePath $PythonExe -Arguments @("-m", "ensurepip", "--upgrade")
    }

    $null = Invoke-LoggedCommand -FilePath $PythonExe -Arguments @("-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel")
    $editableTarget = ".[ui]"
    if ($RunTests) {
        $editableTarget = ".[ui,test]"
    }
    $null = Invoke-LoggedCommand -FilePath $PythonExe -Arguments @("-m", "pip", "install", "-e", $editableTarget)
}

function Resolve-Tool {
    param([Parameter(Mandatory = $true)][string]$Name)
    if ($Name -in @("npm", "npx")) {
        foreach ($cmdName in @("$Name.cmd", "$Name.exe", "$Name.bat")) {
            $cmdCandidate = Get-Command $cmdName -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($cmdCandidate) {
                return $cmdCandidate.Source
            }
        }
    }
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd) {
        return $cmd.Source
    }
    # Fallback: probe well-known install dirs the user's PATH may have missed
    # (Node / npm installers don't always trigger an immediate refresh of the
    # PowerShell session's PATH; bash sees them, PowerShell doesn't.)
    $extraDirs = @()
    if ($Name -in @("npm", "node", "npx")) {
        if ($env:APPDATA) { $extraDirs += (Join-Path $env:APPDATA "npm") }
        $extraDirs += (Join-Path $env:USERPROFILE "npm")
        if ($env:ProgramFiles) { $extraDirs += (Join-Path $env:ProgramFiles "nodejs") }
        if (${env:ProgramFiles(x86)}) { $extraDirs += (Join-Path ${env:ProgramFiles(x86)} "nodejs") }
    }
    foreach ($dir in $extraDirs) {
        if (-not (Test-Path -LiteralPath $dir)) { continue }
        foreach ($ext in @(".exe", ".cmd", ".bat", "")) {
            $candidate = Join-Path $dir ($Name + $ext)
            if (Test-Path -LiteralPath $candidate) {
                # Prepend the dir to PATH so spawned children inherit it.
                if (($env:PATH -split [System.IO.Path]::PathSeparator) -notcontains $dir) {
                    $env:PATH = $dir + [System.IO.Path]::PathSeparator + $env:PATH
                    Write-Log "Resolve-Tool: prepending $dir to PATH (found $Name out-of-band)"
                }
                return $candidate
            }
        }
    }
    return $null
}

function Get-NodeVersion {
    $node = Resolve-Tool "node"
    if (-not $node) {
        return $null
    }
    $result = Invoke-CaptureCommand -FilePath $node -Arguments @("--version")
    if ($result.ExitCode -ne 0) {
        return $null
    }
    $text = (@($result.Output) | Select-Object -First 1).ToString().Trim()
    if ($text -match '^v?(\d+)\.(\d+)\.(\d+)') {
        return (New-Object System.Version([int]$Matches[1], [int]$Matches[2], [int]$Matches[3]))
    }
    return $null
}

function Ensure-GuiNodeDependencies {
    param([switch]$Required)
    if ($SkipNode) {
        if ($Required) {
            throw "-SkipNode cannot be used when web UI startup is required."
        }
        Write-Log "Skipping Node dependency installation because -SkipNode was specified." "WARN"
        return $false
    }

    $npm = Resolve-Tool "npm"
    if (-not $npm) {
        if ($Required) {
            throw "npm was not found. Install Node.js 20+ and rerun."
        }
        Write-Log "npm was not found; web UI cannot be prepared." "WARN"
        return $false
    }

    $guiDir = Join-Path $script:Root "gui"
    $nodeModules = Join-Path $guiDir "node_modules"
    $lockFile = Join-Path $guiDir "package-lock.json"
    $installedLock = Join-Path $nodeModules ".package-lock.json"
    $shouldInstall = -not (Test-Path -LiteralPath $nodeModules)

    if ((Test-Path -LiteralPath $lockFile) -and (Test-Path -LiteralPath $installedLock)) {
        if ((Get-Item -LiteralPath $lockFile).LastWriteTimeUtc -gt (Get-Item -LiteralPath $installedLock).LastWriteTimeUtc) {
            $shouldInstall = $true
        }
    }

    if (-not $shouldInstall) {
        Write-Log "GUI node_modules already exists; skipping npm install."
        return $true
    }

    $npmArgs = @("install")
    if (Test-Path -LiteralPath $lockFile) {
        $npmArgs = @("ci")
    }

    $rc = Invoke-LoggedCommand -FilePath $npm -Arguments $npmArgs -WorkingDirectory $guiDir -AllowFailure
    if ($rc -ne 0) {
        if ($Required) {
            throw "Failed to install GUI Node dependencies."
        }
        Write-Log "Failed to install GUI Node dependencies." "WARN"
        return $false
    }
    return $true
}

function Set-RuntimeEnvironment {
    param([string]$PythonExe)
    $venvScripts = Split-Path -Parent $PythonExe
    $env:WLWL_PROJECT_ROOT = $script:Root
    $env:WLWL_PYTHON = $PythonExe
    $env:VIRTUAL_ENV = $script:VenvDir
    $env:PATH = $venvScripts + [System.IO.Path]::PathSeparator + $env:PATH
    Write-Log "WLWL_PYTHON=$env:WLWL_PYTHON"
}

function Run-Validation {
    param([string]$PythonExe)
    $null = Invoke-LoggedCommand -FilePath $PythonExe -Arguments @("-m", "py_compile", "launch.pyw", "agentmain.py", "launcher\api_server.py")
    $null = Invoke-LoggedCommand -FilePath $PythonExe -Arguments @("-m", "launcher.doctor", "--no-color") -AllowFailure
    if ($RunTests) {
        $null = Invoke-LoggedCommand -FilePath $PythonExe -Arguments @("-m", "pytest", "tests", "-q")
    }
}

function Get-LaunchCommand {
    param([string]$RequestedMode)

    $nodeVersion = Get-NodeVersion
    $npm = Resolve-Tool "npm"
    if ($null -eq $nodeVersion) {
        throw "Node.js was not found or did not report a version. Install Node.js >=20.9 and rerun."
    }
    if ($nodeVersion -lt ([System.Version]::new(20, 9, 0))) {
        throw "Node.js $nodeVersion is too old; gui/package.json requires >=20.9.0."
    }
    if (-not $npm) {
        throw "npm was not found. Install Node.js 20+ and rerun."
    }

    Write-Log "Web UI toolchain ready. Node=$nodeVersion, npm=$npm"
    $nodeReady = Ensure-GuiNodeDependencies -Required
    if (-not $nodeReady) {
        throw "Failed to install GUI Node dependencies."
    }
    return [pscustomobject]@{ Script = "launch.pyw"; Args = @() }
}

function Start-WlwlAss {
    param([string]$PythonExe)
    $launch = Get-LaunchCommand -RequestedMode $Mode
    $commandArgs = @($launch.Script) + @($launch.Args) + @($LaunchArgs)
    if ($CheckOnly) {
        Write-Log "Check-only mode complete. Launch command would be: $(Format-CommandLine -FilePath $PythonExe -Arguments $commandArgs)"
        return
    }
    $commandLine = Format-CommandLine -FilePath $PythonExe -Arguments $commandArgs
    Write-Log "RUN ($script:Root): $commandLine"
    Push-Location -LiteralPath $script:Root
    try {
        & $PythonExe @commandArgs
        $exitCode = $LASTEXITCODE
        if ($null -eq $exitCode) {
            $exitCode = 0
        }
    } finally {
        Pop-Location
    }
    Write-Log "EXIT ${exitCode}: $commandLine"
    if ($exitCode -ne 0) {
        throw "Command failed with exit code ${exitCode}: $commandLine"
    }
}

function Main {
    Initialize-Logging
    Write-Log "Mode=$Mode CheckOnly=$CheckOnly SkipInstall=$SkipInstall SkipNode=$SkipNode RunTests=$RunTests"
    Assert-ProjectLayout
    Ensure-RuntimeDirs
    Ensure-EnvFile
    $pythonExe = Ensure-Venv
    Set-RuntimeEnvironment -PythonExe $pythonExe
    Install-PythonDependencies -PythonExe $pythonExe
    Run-Validation -PythonExe $pythonExe
    Start-WlwlAss -PythonExe $pythonExe
    Write-Log "Bootstrap finished successfully."
}

try {
    Main
    exit 0
} catch {
    Write-Log "Startup failed: $($_.Exception.Message)" "ERROR"
    if ($_.ScriptStackTrace) {
        Write-Log $_.ScriptStackTrace "ERROR"
    }
    Write-Host ""
    Write-Host "Startup failed. See log: $script:LogPath"
    exit 1
}
