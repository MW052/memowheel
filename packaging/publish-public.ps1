<#
.SYNOPSIS
  Publish a clean snapshot of the current committed source to the PUBLIC mirror.

.DESCRIPTION
  Development happens in the private repo (origin). This script exports ONLY the
  git-tracked files of a committed ref (default: master) via `git archive`, so
  anything gitignored or untracked -- STATUS.md, data/, .env, *.db, build output
  -- can never be included. It then commits that snapshot as a single commit on
  top of the public mirror's history and pushes it. No force-push; the public
  repo keeps a linear "release" history, one commit per publish.

  Uncommitted working-tree changes are ignored on purpose: only what you have
  committed to the source ref gets published. Commit first, then publish.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File packaging\publish-public.ps1
  powershell -ExecutionPolicy Bypass -File packaging\publish-public.ps1 -Message "date-range import + phone-over-USB docs"
#>
param(
  [string]$Message   = "",
  [string]$PublicUrl = "https://github.com/MW052/memowheel.git",
  [string]$SourceRef = "master"
)
$ErrorActionPreference = "Stop"

$repo = (git rev-parse --show-toplevel).Trim()
Set-Location $repo

# Refuse to publish a ref that doesn't exist.
git rev-parse --verify --quiet "$SourceRef^{commit}" > $null
if ($LASTEXITCODE -ne 0) { throw "Source ref '$SourceRef' not found." }

$srcSha = (git rev-parse --short $SourceRef).Trim()
$date   = Get-Date -Format "yyyy-MM-dd"

# Note (don't block) if the working tree has uncommitted changes -- they won't ship.
$dirty = git status --porcelain
if ($dirty) {
  Write-Host "Note: uncommitted changes present; publishing committed $SourceRef ($srcSha) only." -ForegroundColor Yellow
}

$tmp = Join-Path $env:TEMP ("mstudio-publish-" + [Guid]::NewGuid().ToString("N"))
$tar = Join-Path $env:TEMP ("mstudio-publish-" + [Guid]::NewGuid().ToString("N") + ".tar")
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
  # Clone the public mirror (may be an empty repo on the very first run).
  git clone --quiet $PublicUrl $tmp
  if ($LASTEXITCODE -ne 0) { throw "Clone of $PublicUrl failed." }

  $head = git -C $tmp rev-parse --verify --quiet HEAD
  $isFirst = -not $head

  # Wipe everything except .git, then lay down the exact tracked tree of the ref.
  # Clearing first makes deletions in the source propagate to the mirror.
  Get-ChildItem -Path $tmp -Force | Where-Object { $_.Name -ne ".git" } | Remove-Item -Recurse -Force

  # Binary-safe export: archive to a .tar file, then extract (piping binary
  # through a PowerShell pipeline would corrupt it).
  git archive --format=tar -o $tar $SourceRef
  if ($LASTEXITCODE -ne 0) { throw "git archive of $SourceRef failed." }
  # Use Windows' bundled bsdtar explicitly. A bare `tar` can resolve to Git's GNU
  # tar, which misreads a `C:\...tar` path as a remote host ("Cannot connect to
  # C:") and fails; bsdtar handles drive letters natively.
  $tarExe = Join-Path $env:SystemRoot "System32\tar.exe"
  if (-not (Test-Path $tarExe)) { $tarExe = "tar" }
  & $tarExe -x -f $tar -C $tmp
  if ($LASTEXITCODE -ne 0) { throw "tar extract failed." }

  # Inherit committer identity from the outer repo if the clone lacks one.
  if (-not (git -C $tmp config user.email)) { git -C $tmp config user.email (git config user.email) }
  if (-not (git -C $tmp config user.name))  { git -C $tmp config user.name  (git config user.name)  }

  git -C $tmp add -A
  if (-not (git -C $tmp status --porcelain)) {
    Write-Host "No changes since the last published snapshot -- nothing to do."
    return
  }

  if ($isFirst) {
    $msg = "Initial public release"
  } elseif ($Message) {
    $msg = "Publish ${date}: $Message (src $srcSha)"
  } else {
    $msg = "Publish snapshot $date (src $srcSha)"
  }

  git -C $tmp commit --quiet -m $msg
  git -C $tmp branch -M master
  git -C $tmp push --quiet origin master
  if ($LASTEXITCODE -ne 0) { throw "Push to $PublicUrl failed." }

  Write-Host ""
  Write-Host "Published to $PublicUrl (master)" -ForegroundColor Green
  Write-Host "  commit: $msg"
} finally {
  Set-Location $repo
  Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
  Remove-Item -Force $tar -ErrorAction SilentlyContinue
}
