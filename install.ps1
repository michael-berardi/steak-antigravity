<#
Steak Antigravity installer for Windows (PowerShell 5.1 and PowerShell 7+):
  irm https://github.com/michael-berardi/steak-antigravity/releases/latest/download/install.ps1 | iex

Downloads steak-antigravity.tar.gz and SHA256SUMS over HTTPS, verifies SHA-256 fail-closed before touching disk,
stages and swaps atomically into the managed install directory, installs a CLI launcher in the bin
directory, and wires Antigravity CLI (unless -NoWire). Never edits profiles or requires admin rights.
#>
param(
  [string]$Version = '',
  [string]$Dir = '',
  [string]$BinDir = '',
  [switch]$DryRun,
  [switch]$NoWire,
  [switch]$Uninstall,
  [switch]$Help
)

function Invoke-SteakAntigravityInstaller {
  param(
    [string]$VersionArg,
    [string]$DirArg,
    [string]$BinDirArg,
    [bool]$DryRunFlag,
    [bool]$NoWireFlag,
    [bool]$UninstallFlag,
    [bool]$HelpFlag
  )

  $savedErrorPref = $ErrorActionPreference
  $savedProgressPref = $ProgressPreference
  $savedBinEnv = [Environment]::GetEnvironmentVariable('STEAK_ANTIGRAVITY_BIN_DIR')
  $savedSkipEnv = [Environment]::GetEnvironmentVariable('STEAK_ANTIGRAVITY_SKIP_BIN_LINK')
  $savedDnt = [Environment]::GetEnvironmentVariable('DO_NOT_TRACK')
  $savedHomebrewAnalytics = [Environment]::GetEnvironmentVariable('HOMEBREW_NO_ANALYTICS')
  $savedNextTelemetry = [Environment]::GetEnvironmentVariable('NEXT_TELEMETRY_DISABLED')
  $savedHfTelemetry = [Environment]::GetEnvironmentVariable('HF_HUB_DISABLE_TELEMETRY')
  $ErrorActionPreference = 'Stop'
  $ProgressPreference = 'SilentlyContinue'

  # Never let a tool we invoke send its own usage analytics; restore the caller's values below.
  [Environment]::SetEnvironmentVariable('DO_NOT_TRACK', '1')
  [Environment]::SetEnvironmentVariable('HOMEBREW_NO_ANALYTICS', '1')
  [Environment]::SetEnvironmentVariable('NEXT_TELEMETRY_DISABLED', '1')
  [Environment]::SetEnvironmentVariable('HF_HUB_DISABLE_TELEMETRY', '1')

  $repo = 'michael-berardi/steak-antigravity'
  $name = 'steak-antigravity'
  $archive = "$name.tar.gz"
  $managedMark = '.steak-antigravity-managed'
  $shimMark = 'steak-antigravity installer shim'
  $nodeMinMajor = 22
  $onWindows = ($PSVersionTable.PSVersion.Major -lt 6) -or $IsWindows

  $tmp = $null
  $stage = $null

  function Show-Usage {
    Write-Host @"
Usage: install.ps1 [-Version <vX.Y.Z>] [-Dir <path>] [-BinDir <path>] [-DryRun] [-NoWire] [-Uninstall] [-Help]

Environment overrides (useful with irm ... | iex):
  STEAK_ANTIGRAVITY_VERSION    Pin release version (e.g. 0.4.0 or v0.4.0)
  STEAK_ANTIGRAVITY_HOME       Override managed install directory
  STEAK_ANTIGRAVITY_BIN_DIR    Override CLI launcher directory
  STEAK_ANTIGRAVITY_BASE_URL   Override download base URL (https:// or file:// for offline tests)
  STEAK_ANTIGRAVITY_DRY_RUN=1  Show planned actions without changing disk
  STEAK_ANTIGRAVITY_NO_WIRE=1  Install files and CLI launcher without wiring Antigravity
  STEAK_ANTIGRAVITY_UNINSTALL=1 Unwire Antigravity and remove the managed install
"@
  }

  function Test-EnvTruthy([string]$Val) {
    return ($Val -eq '1' -or $Val -ieq 'true' -or $Val -ieq 'yes')
  }

  function Invoke-Tool {
    param([string]$Exe, [string[]]$ToolArgs = @(), [switch]$Capture)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $out = ''
    try {
      if ($Capture) { $out = (& $Exe @ToolArgs 2>&1 | ForEach-Object { "$_" }) -join "`n" }
      else { & $Exe @ToolArgs | Out-Host }
      $code = $LASTEXITCODE
    } catch {
      $code = 1
      $out = "$_"
    } finally { $ErrorActionPreference = $prev }
    return [pscustomobject]@{ Code = $code; Output = $out }
  }

  function Find-Python {
    foreach ($candidate in @('python3', 'python')) {
      $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
      if ($cmd) {
        $r = Invoke-Tool -Exe $cmd.Source -ToolArgs @('-c', 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)') -Capture
        if ($r.Code -eq 0) { return [pscustomobject]@{ Exe = $cmd.Source; Prefix = @() } }
      }
    }
    if ($onWindows) {
      $py = Get-Command 'py' -ErrorAction SilentlyContinue
      if ($py) {
        $r = Invoke-Tool -Exe $py.Source -ToolArgs @('-3', '-c', 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)') -Capture
        if ($r.Code -eq 0) { return [pscustomobject]@{ Exe = $py.Source; Prefix = @('-3') } }
      }
    }
    return $null
  }

  function Find-UnsafeEntry([string]$RootPath) {
    $pending = New-Object 'System.Collections.Generic.Stack[string]'
    $pending.Push($RootPath)
    while ($pending.Count -gt 0) {
      $dir = $pending.Pop()
      foreach ($child in @(Get-ChildItem -LiteralPath $dir -Force)) {
        if ($child.Name -eq '.git' -or ($child.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
          return $child.FullName
        }
        if ($child.PSIsContainer) { $pending.Push($child.FullName) }
      }
    }
    return $null
  }

  function Test-OwnExistingInstall([string]$DirPath) {
    if (Test-Path -LiteralPath (Join-Path $DirPath $managedMark) -PathType Leaf) { return $true }
    $pluginPath = Join-Path $DirPath 'plugin.json'
    $pkgPath = Join-Path $DirPath 'package.json'
    $binPath = Join-Path $DirPath "bin/$name"
    if ((Test-Path -LiteralPath $pluginPath -PathType Leaf) -and
        (Test-Path -LiteralPath $pkgPath -PathType Leaf) -and
        (Test-Path -LiteralPath $binPath -PathType Leaf)) {
      try {
        $pObj = Get-Content -LiteralPath $pluginPath -Raw | ConvertFrom-Json
        $kObj = Get-Content -LiteralPath $pkgPath -Raw | ConvertFrom-Json
        if ($pObj.name -eq $name -and $kObj.name -eq $name) { return $true }
      } catch {
        return $false
      }
    }
    return $false
  }

  function Get-RemoteFile([string]$Url, [string]$OutFile) {
    if ($Url.StartsWith('file://', [StringComparison]::OrdinalIgnoreCase)) {
      $localPath = ([Uri]$Url).LocalPath
      if (-not (Test-Path -LiteralPath $localPath -PathType Leaf)) {
        throw "download failed: $Url (file not found)"
      }
      Copy-Item -LiteralPath $localPath -Destination $OutFile -Force
      return
    }
    if (-not $Url.StartsWith('https://', [StringComparison]::OrdinalIgnoreCase)) {
      throw "refusing non-HTTPS URL: $Url"
    }
    for ($attempt = 1; ; $attempt++) {
      try {
        Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing
        return
      } catch {
        $failure = $_
        $status = 0
        try { $status = [int]$failure.Exception.Response.StatusCode } catch { $status = 0 }
        if ($attempt -ge 4 -or ($status -ge 400 -and $status -lt 500)) {
          throw "download failed: $Url ($($failure.Exception.Message))"
        }
        Write-Host "Download interrupted; trying again ($attempt of 3)..."
        Start-Sleep -Seconds ($attempt * 2)
      }
    }
  }

  function Assert-Sha256([string]$FilePath, [string]$AssetName, [string]$SumsPath) {
    $escaped = [regex]::Escape($AssetName)
    $sumMatches = @(Get-Content -LiteralPath $SumsPath | Where-Object { $_ -match "^\s*([0-9a-fA-F]{64})\s+\*?$escaped\s*$" })
    if ($sumMatches.Count -ne 1) {
      throw "expected 1 valid SHA-256 entry for $AssetName in SHA256SUMS (found $($sumMatches.Count)); nothing was installed"
    }
    $want = ($sumMatches[0].Trim() -split '\s+')[0].ToLower()
    $got = (Get-FileHash -LiteralPath $FilePath -Algorithm SHA256).Hash.ToLower()
    if ($got -ne $want) {
      throw "checksum mismatch for $AssetName (expected $want, got $got); nothing was installed"
    }
  }

  function Assert-SafeTargetDir([string]$Candidate, [string]$UserHome) {
    if (-not $Candidate) { throw 'install directory must not be empty' }
    if ($Candidate -match '[\r\n]') { throw 'install directory must not contain newlines' }
    $norm = $Candidate.TrimEnd('\', '/')
    $homeNorm = $UserHome.TrimEnd('\', '/')
    $root = [IO.Path]::GetPathRoot($Candidate).TrimEnd('\', '/')
    if (-not $norm -or $norm -ieq $root -or $norm -ieq $homeNorm) {
      throw "refusing unsafe install directory: $Candidate"
    }
  }

  try {
    if ($HelpFlag) { Show-Usage; return }

    if ($PSVersionTable.PSVersion.Major -lt 6) {
      [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    }

    $userHome = [Environment]::GetFolderPath('UserProfile')
    if (-not $userHome) { $userHome = $HOME }
    if (-not $userHome) { throw 'cannot determine user home directory' }

    $versionPin = if ($VersionArg) { $VersionArg } elseif ($env:STEAK_ANTIGRAVITY_VERSION) { $env:STEAK_ANTIGRAVITY_VERSION } else { '' }
    $versionPin = $versionPin.TrimStart('v')
    if ($versionPin) {
      if ($versionPin -notmatch '^[0-9A-Za-z][0-9A-Za-z._+-]*$' -or $versionPin.Contains('..')) {
        throw "invalid version '$versionPin' (expected e.g. 0.4.0 or v0.4.0)"
      }
    }

    $defaultDir = if ($onWindows -and $env:LOCALAPPDATA) {
      Join-Path $env:LOCALAPPDATA $name
    } else {
      Join-Path (Join-Path (Join-Path $userHome '.local') 'share') $name
    }
    $targetRaw = if ($DirArg) { $DirArg } elseif ($env:STEAK_ANTIGRAVITY_HOME) { $env:STEAK_ANTIGRAVITY_HOME } else { $defaultDir }
    $binRaw = if ($BinDirArg) { $BinDirArg } elseif ($env:STEAK_ANTIGRAVITY_BIN_DIR) { $env:STEAK_ANTIGRAVITY_BIN_DIR } else { Join-Path (Join-Path $userHome '.local') 'bin' }

    $target = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($targetRaw).TrimEnd('\', '/')
    $binDir = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($binRaw).TrimEnd('\', '/')
    Assert-SafeTargetDir -Candidate $target -UserHome $userHome

    $dry = $DryRunFlag -or (Test-EnvTruthy $env:STEAK_ANTIGRAVITY_DRY_RUN)
    $noWire = $NoWireFlag -or (Test-EnvTruthy $env:STEAK_ANTIGRAVITY_NO_WIRE)
    $uninstall = $UninstallFlag -or (Test-EnvTruthy $env:STEAK_ANTIGRAVITY_UNINSTALL)

    $shimPath = if ($onWindows) { Join-Path $binDir "$name.cmd" } else { Join-Path $binDir $name }

    if ($uninstall) {
      if ($dry) {
        Write-Host "[dry-run] Would uninstall ${name}:"
        if (-not $noWire) { Write-Host "  unwire:   $(Join-Path $target "bin\$name") uninstall" }
        else { Write-Host '  unwire:   skipped (-NoWire)' }
        Write-Host "  launcher: remove $shimPath (only if managed by this installer)"
        Write-Host "  dir:      remove $target (only if managed by this installer)"
        return
      }

      if (Test-Path -LiteralPath $target) {
        $item = Get-Item -LiteralPath $target -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
          throw "$target is a symbolic link or junction; refusing to remove it"
        }
        if (-not $item.PSIsContainer) { throw "$target is not a directory; refusing to remove it" }
        if (Test-Path -LiteralPath (Join-Path $target '.git')) { throw "$target is a git checkout; refusing to remove it" }
        if (-not (Test-Path -LiteralPath (Join-Path $target $managedMark))) {
          throw "$target was not created by this installer (missing $managedMark); refusing to remove it"
        }
        if (-not ((Test-Path -LiteralPath (Join-Path $target 'plugin.json')) -and (Test-Path -LiteralPath (Join-Path $target "bin/$name")))) {
          throw "$target does not look like a $name installation; refusing to remove it"
        }
        $unsafe = Find-UnsafeEntry $target
        if ($unsafe) { throw "$target contains a link or .git entry ($unsafe); refusing to remove it" }

        if (-not $noWire) {
          $py = Find-Python
          if ($py) {
            $env:STEAK_ANTIGRAVITY_BIN_DIR = $binDir
            $r = Invoke-Tool -Exe $py.Exe -ToolArgs ($py.Prefix + @('-I', '-B', (Join-Path $target "bin/$name"), 'uninstall'))
            if ($r.Code -ne 0) { throw "unwire failed; $target was left untouched" }
          } else {
            Write-Host 'steak-antigravity install: warning: Python 3 not found on PATH; skipped Antigravity unwiring.'
          }
        }
      }

      if (Test-Path -LiteralPath $shimPath) {
        $sItem = Get-Item -LiteralPath $shimPath -Force
        $isLink = [bool]($sItem.Attributes -band [IO.FileAttributes]::ReparsePoint)
        if ($onWindows) {
          $text = if (-not $sItem.PSIsContainer -and -not $isLink) { [string](Get-Content -LiteralPath $shimPath -Raw -ErrorAction SilentlyContinue) } else { '' }
          if ($text.Contains($shimMark)) { Remove-Item -LiteralPath $shimPath -Force }
          else { Write-Host "steak-antigravity install: warning: $shimPath was not created by this installer; leaving it alone." }
        } else {
          if ($isLink -and ($sItem.Target -eq (Join-Path $target "bin/$name"))) { Remove-Item -LiteralPath $shimPath -Force }
          else { Write-Host "steak-antigravity install: warning: $shimPath does not point to this install; leaving it alone." }
        }
      }

      if (Test-Path -LiteralPath $target) {
        [IO.Directory]::Delete($target, $true)
        Write-Host "Removed $name from $target."
      } else {
        Write-Host "$name is not installed at $target."
      }
      return
    }

    $base = if ($env:STEAK_ANTIGRAVITY_BASE_URL) { $env:STEAK_ANTIGRAVITY_BASE_URL.TrimEnd('/') }
    elseif ($versionPin) { "https://github.com/$repo/releases/download/v$versionPin" }
    else { "https://github.com/$repo/releases/latest/download" }

    if (-not ($base.StartsWith('https://', [StringComparison]::OrdinalIgnoreCase) -or $base.StartsWith('file://', [StringComparison]::OrdinalIgnoreCase))) {
      throw "refusing non-HTTPS base URL: $base (only https:// or file:// is allowed)"
    }

    if ($dry) {
      Write-Host "[dry-run] Would install ${name}:"
      Write-Host "  archive:  $base/$archive"
      Write-Host "  sums:     $base/SHA256SUMS"
      Write-Host "  dir:      $target"
      Write-Host "  launcher: $shimPath"
      if ($noWire) { Write-Host '  wire:     skipped (-NoWire)' }
      else { Write-Host "  wire:     python3 -I -B $(Join-Path $target "bin/$name") install" }
      return
    }

    if (-not (Get-Command tar -ErrorAction SilentlyContinue)) {
      throw 'needs tar (built into Windows 10 1803+ and Windows 11); install tar and run again'
    }

    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("steak-antigravity-install-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $tmp | Out-Null

    Write-Host "Downloading $name..."
    $archivePath = Join-Path $tmp $archive
    $sumsPath = Join-Path $tmp 'SHA256SUMS'
    Get-RemoteFile -Url "$base/$archive" -OutFile $archivePath
    Get-RemoteFile -Url "$base/SHA256SUMS" -OutFile $sumsPath
    Assert-Sha256 -FilePath $archivePath -AssetName $archive -SumsPath $sumsPath

    # Fail closed on unsafe archive paths before extracting anything.
    $listRes = Invoke-Tool -Exe 'tar' -ToolArgs @('-tzf', $archivePath) -Capture
    if ($listRes.Code -ne 0 -or -not $listRes.Output.Trim()) {
      throw 'cannot read archive listing or archive is empty; nothing was installed'
    }
    foreach ($entry in ($listRes.Output -split "`r?`n")) {
      if (-not $entry) { continue }
      if ($entry.StartsWith('/') -or $entry.StartsWith('\') -or $entry -match '^[A-Za-z]:') {
        throw "archive contains absolute path '$entry'; nothing was installed"
      }
      foreach ($part in ($entry -split '[/\\]')) {
        if ($part -eq '..') { throw "archive contains path traversal '$entry'; nothing was installed" }
      }
    }

    # Inspect existing target BEFORE staging into parent.
    if (Test-Path -LiteralPath $target) {
      $oldItem = Get-Item -LiteralPath $target -Force
      if ($oldItem.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "$target is a symbolic link or junction; refusing to overwrite"
      }
      if (-not $oldItem.PSIsContainer) { throw "$target exists and is not a directory; refusing to overwrite" }
      if (Test-Path -LiteralPath (Join-Path $target '.git')) {
        throw "$target is a git checkout; refusing to overwrite (update it with git instead)"
      }
      $unsafeOld = Find-UnsafeEntry $target
      if ($unsafeOld) { throw "$target contains a link or .git entry ($unsafeOld); refusing to overwrite" }
      if (-not (Test-OwnExistingInstall $target)) {
        throw "$target already exists and is not a $name installation; choose another -Dir"
      }
    }

    $previous = ''
    if (Test-Path -LiteralPath (Join-Path $target 'VERSION')) {
      $previous = (Get-Content -LiteralPath (Join-Path $target 'VERSION') -Raw).Trim()
    } elseif (Test-Path -LiteralPath (Join-Path $target 'package.json')) {
      try { $previous = (Get-Content -LiteralPath (Join-Path $target 'package.json') -Raw | ConvertFrom-Json).version } catch { $previous = '' }
    }

    # Stage directly under target's parent so Move-Item never crosses drives on Windows PowerShell 5.1.
    $targetParent = Split-Path -Path $target -Parent
    New-Item -ItemType Directory -Path $targetParent -Force | Out-Null
    $stage = Join-Path $targetParent (".$name.new." + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $stage | Out-Null

    $unpack = Invoke-Tool -Exe 'tar' -ToolArgs @('-xzf', $archivePath, '-C', $stage) -Capture
    if ($unpack.Code -ne 0) { throw "cannot unpack archive ($($unpack.Output)); nothing was installed" }

    $unsafeNew = Find-UnsafeEntry $stage
    if ($unsafeNew) { throw "archive contains forbidden link or .git entry ($unsafeNew); nothing was installed" }

    foreach ($req in @('package.json', 'plugin.json', "bin/$name", 'VERSION')) {
      if (-not (Test-Path -LiteralPath (Join-Path $stage $req) -PathType Leaf)) {
        throw "archive is missing required file $req; nothing was installed"
      }
    }
    $pkg = Get-Content -LiteralPath (Join-Path $stage 'package.json') -Raw | ConvertFrom-Json
    if ($pkg.name -ne $name) { throw "archive package name is '$($pkg.name)', expected $name; nothing was installed" }
    $ver = (Get-Content -LiteralPath (Join-Path $stage 'VERSION') -Raw).Trim()
    if (-not $ver -or $ver -ne $pkg.version) {
      throw "archive VERSION ('$ver') does not match package.json ('$($pkg.version)'); nothing was installed"
    }
    [IO.File]::WriteAllText(
      (Join-Path $stage $managedMark),
      "managed-by=$name-installer`nversion=$ver`n",
      (New-Object Text.UTF8Encoding $false)
    )

    if (-not $onWindows) {
      $null = Invoke-Tool -Exe 'chmod' -ToolArgs @('755', (Join-Path $stage "bin/$name")) -Capture
      foreach ($pyFile in @(Get-ChildItem -LiteralPath (Join-Path $stage 'scripts') -Filter '*.py' -ErrorAction SilentlyContinue)) {
        $null = Invoke-Tool -Exe 'chmod' -ToolArgs @('755', $pyFile.FullName) -Capture
      }
    }

    # Atomic swap with guarded rollback.
    if (Test-Path -LiteralPath $target) {
      $backup = Join-Path $targetParent (".$name.old." + [guid]::NewGuid().ToString('N'))
      Move-Item -LiteralPath $target -Destination $backup
      try {
        Move-Item -LiteralPath $stage -Destination $target
      } catch {
        $swapErr = $_.Exception.Message
        try {
          Move-Item -LiteralPath $backup -Destination $target
        } catch {
          throw "cannot move staged release into $target ($swapErr), and rollback failed ($($_.Exception.Message)); previous copy is preserved at $backup"
        }
        throw "cannot move staged release into $target ($swapErr); previous copy was restored"
      }
      $stage = $null
      try {
        [IO.Directory]::Delete($backup, $true)
      } catch {
        Write-Host "steak-antigravity install: warning: could not remove temporary backup folder $backup ($($_.Exception.Message))"
      }
    } else {
      Move-Item -LiteralPath $stage -Destination $target
      $stage = $null
    }

    # Install CLI launcher without clobbering unrelated user files.
    New-Item -ItemType Directory -Path $binDir -Force | Out-Null
    $py = Find-Python
    $launcherWritten = $false
    if ($onWindows) {
      $canWrite = $true
      if (Test-Path -LiteralPath $shimPath) {
        $sItem = Get-Item -LiteralPath $shimPath -Force
        $isLink = [bool]($sItem.Attributes -band [IO.FileAttributes]::ReparsePoint)
        $existingText = if (-not $sItem.PSIsContainer -and -not $isLink) { [string](Get-Content -LiteralPath $shimPath -Raw -ErrorAction SilentlyContinue) } else { '' }
        if (-not $existingText.Contains($shimMark)) {
          Write-Host "steak-antigravity install: warning: $shimPath exists and was not written by this installer; leaving it untouched."
          $canWrite = $false
        }
      }
      if ($canWrite) {
        $pyCmd = if ($py) { "`"$($py.Exe)`" $($py.Prefix -join ' ')".Trim() } else { 'python' }
        $cliScript = Join-Path $target "bin\$name"
        $cmdLines = @(
          '@echo off',
          "rem $shimMark`: runs $name. Delete this file to remove the command.",
          "$pyCmd -I -B `"$cliScript`" %*"
        )
        $oem = [Text.Encoding]::GetEncoding([Globalization.CultureInfo]::CurrentCulture.TextInfo.OEMCodePage)
        [IO.File]::WriteAllText($shimPath, (($cmdLines -join "`r`n") + "`r`n"), $oem)
        $launcherWritten = $true
      }
    } else {
      $oldLink = Get-Item -LiteralPath $shimPath -Force -ErrorAction SilentlyContinue
      if (-not $oldLink -or ($oldLink.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        if ($oldLink) { $oldLink.Delete() }
        New-Item -ItemType SymbolicLink -Path $shimPath -Target (Join-Path $target "bin/$name") | Out-Null
        $launcherWritten = $true
      } else {
        Write-Host "steak-antigravity install: warning: $shimPath exists and is not a symlink; leaving it untouched."
      }
    }

    # Wire Antigravity CLI unless -NoWire.
    $wired = $false
    if ($noWire) {
      Write-Host 'Skipped Antigravity wiring (-NoWire).'
    } elseif ($py) {
      $env:STEAK_ANTIGRAVITY_BIN_DIR = $binDir
      $env:STEAK_ANTIGRAVITY_SKIP_BIN_LINK = '1'
      $r = Invoke-Tool -Exe $py.Exe -ToolArgs ($py.Prefix + @('-I', '-B', (Join-Path $target "bin/$name"), 'install'))
      if ($r.Code -ne 0) { throw "$name install failed; files are installed at $target" }
      $wired = $true
    } else {
      Write-Host "steak-antigravity install: warning: Python 3.9+ not found on PATH; skipped Antigravity wiring. Install Python 3, then run: python $(Join-Path $target "bin/$name") install"
    }

    # Check Node prerequisite hint.
    $nodeCmd = Get-Command node -ErrorAction SilentlyContinue
    if (-not $nodeCmd) {
      Write-Host "Note: Node ${nodeMinMajor}+ is not on PATH (optional for hooks, required for the USAP MCP bridge)."
    } else {
      $nr = Invoke-Tool -Exe $nodeCmd.Source -ToolArgs @('-p', 'process.versions.node') -Capture
      $major = 0
      if ($nr.Code -eq 0 -and [int]::TryParse($nr.Output.Trim().Split('.')[0], [ref]$major) -and $major -lt $nodeMinMajor) {
        Write-Host "Note: Node $($nr.Output.Trim()) found; Node ${nodeMinMajor}+ is required for the USAP MCP bridge."
      }
    }

    if ($previous) { Write-Host "Upgraded $name from $previous to $ver." }
    else { Write-Host "Installed $name $ver." }
    Write-Host "Install dir: $target"
    if ($launcherWritten) { Write-Host "CLI launcher: $shimPath" }

    $pathParts = $env:PATH -split [IO.Path]::PathSeparator | ForEach-Object { $_.TrimEnd('\', '/') }
    if ($pathParts -notcontains $binDir) {
      if ($onWindows) { Write-Host "Add to PATH: `$env:PATH = `"$binDir;`$env:PATH`" (or add $binDir to your User PATH)" }
      else { Write-Host "Add to PATH: export PATH=`"$binDir`:`$PATH`"" }
    }

    if ($noWire) { Write-Host "Next: $name install; $name doctor" }
    elseif (-not (Get-Command agy -ErrorAction SilentlyContinue)) { Write-Host "Next: install Antigravity CLI (agy), then run: $name doctor" }
    elseif ($wired) { Write-Host "Next: $name doctor" }
  } finally {
    if ($stage -and (Test-Path -LiteralPath $stage)) { Remove-Item -Recurse -Force -LiteralPath $stage -ErrorAction SilentlyContinue }
    if ($tmp -and (Test-Path -LiteralPath $tmp)) { Remove-Item -Recurse -Force -LiteralPath $tmp -ErrorAction SilentlyContinue }
    [Environment]::SetEnvironmentVariable('STEAK_ANTIGRAVITY_BIN_DIR', $savedBinEnv)
    [Environment]::SetEnvironmentVariable('STEAK_ANTIGRAVITY_SKIP_BIN_LINK', $savedSkipEnv)
    [Environment]::SetEnvironmentVariable('DO_NOT_TRACK', $savedDnt)
    [Environment]::SetEnvironmentVariable('HOMEBREW_NO_ANALYTICS', $savedHomebrewAnalytics)
    [Environment]::SetEnvironmentVariable('NEXT_TELEMETRY_DISABLED', $savedNextTelemetry)
    [Environment]::SetEnvironmentVariable('HF_HUB_DISABLE_TELEMETRY', $savedHfTelemetry)
    $ProgressPreference = $savedProgressPref
    $ErrorActionPreference = $savedErrorPref
  }
}

Invoke-SteakAntigravityInstaller `
  -VersionArg $Version `
  -DirArg $Dir `
  -BinDirArg $BinDir `
  -DryRunFlag ([bool]$DryRun) `
  -NoWireFlag ([bool]$NoWire) `
  -UninstallFlag ([bool]$Uninstall) `
  -HelpFlag ([bool]$Help)
