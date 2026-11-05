<#
.SYNOPSIS
Links the local skills and rules to the global Gemini (AGY), Claude Code, and Codex directories on Windows.
#>

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir

Write-Host "Installing Python dependencies..."
if (Get-Command pip -ErrorAction SilentlyContinue) {
    try {
        $process = Start-Process -FilePath pip -ArgumentList "install", "`"$RepoRoot`"" -Wait -PassThru -NoNewWindow
        if ($process.ExitCode -ne 0) {
            Write-Host "Error: Failed to install dependencies." -ForegroundColor Red
            Write-Host "Please download and install Python and pip from https://www.python.org/downloads/ and try again." -ForegroundColor Yellow
            exit 1
        }
    } catch {
        Write-Host "Error: Failed to install dependencies." -ForegroundColor Red
        Write-Host "Please download and install Python and pip from https://www.python.org/downloads/ and try again." -ForegroundColor Yellow
        exit 1
    }
} else {
    Write-Host "Error: pip not found." -ForegroundColor Red
    Write-Host "Please download and install Python and pip from https://www.python.org/downloads/ and try again." -ForegroundColor Yellow
    exit 1
}

$SourceSkills = Join-Path -Path $RepoRoot -ChildPath ".agents\skills"
$SourceSkillCommands = Join-Path -Path $RepoRoot -ChildPath ".agents\skill_commands"
$SourceRules = Join-Path -Path $RepoRoot -ChildPath ".agents\rules"

function Link-Skills {
    param([string]$SourceDir, [string]$TargetDir)

    if (-not (Test-Path -Path $TargetDir)) {
        New-Item -ItemType Directory -Path $TargetDir | Out-Null
    }
    if (Test-Path -Path $SourceDir) {
        Get-ChildItem -Path $SourceDir -Directory | ForEach-Object {
            $Target = Join-Path -Path $TargetDir -ChildPath $_.Name
            if (Test-Path -Path $Target) {
                Remove-Item -Path $Target -Recurse -Force
            }
            New-Item -ItemType Junction -Path $Target -Value $_.FullName | Out-Null
        }
    }
}

function Link-CodexSkills {
    param([string]$SourceDir, [string]$TargetDir)

    if (-not (Test-Path -Path $TargetDir)) {
        New-Item -ItemType Directory -Path $TargetDir | Out-Null
    }
    if (Test-Path -Path $SourceDir) {
        Get-ChildItem -Path $SourceDir -Directory | ForEach-Object {
            $Target = Join-Path -Path $TargetDir -ChildPath $_.Name
            $CanLink = $true
            if (Test-Path -Path $Target) {
                $Existing = Get-Item -Path $Target -Force
                if (-not ($Existing.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                    Write-Host "Warning: preserving existing Codex skill directory $Target" -ForegroundColor Yellow
                    $CanLink = $false
                } else {
                    Remove-Item -Path $Target -Force
                }
            }
            if ($CanLink) {
                New-Item -ItemType Junction -Path $Target -Value $_.FullName | Out-Null
            }
        }
    }
}

# --- Gemini / Antigravity (AGY) integration ---
$AgyDir = Join-Path -Path $env:USERPROFILE -ChildPath ".gemini\antigravity-cli"
$RulesDir = Join-Path -Path $AgyDir -ChildPath "rules"

Write-Host "Linking skills to global AGY configuration"
Link-Skills -SourceDir $SourceSkills -TargetDir (Join-Path -Path $AgyDir -ChildPath "skills")

if (-not (Test-Path -Path $RulesDir)) {
    New-Item -ItemType Directory -Path $RulesDir | Out-Null
}

Write-Host "Linking rules to global AGY configuration"
if (Test-Path -Path $SourceRules) {
    Get-ChildItem -Path $SourceRules -File | ForEach-Object {
        $Target = Join-Path -Path $RulesDir -ChildPath $_.Name
        if (Test-Path -Path $Target) {
            Remove-Item -Path $Target -Force
        }
        New-Item -ItemType HardLink -Path $Target -Value $_.FullName | Out-Null
    }
}

# --- Claude Code integration ---
$ClaudeDir = Join-Path -Path $env:USERPROFILE -ChildPath ".claude"

Write-Host "Linking skills to global Claude Code configuration"
Link-Skills -SourceDir $SourceSkills -TargetDir (Join-Path -Path $ClaudeDir -ChildPath "skills")

Write-Host "Linking command skills (/work_next_item, /resume_task, /groom_backlogs) to global Claude Code configuration"
Link-Skills -SourceDir $SourceSkillCommands -TargetDir (Join-Path -Path $ClaudeDir -ChildPath "skills")

Write-Host "Registering library rulebook in global Claude memory"
$ClaudeMemory = Join-Path -Path $ClaudeDir -ChildPath "CLAUDE.md"
$MarkerStart = "<!-- ai_knowledge_library:start -->"
$MarkerEnd = "<!-- ai_knowledge_library:end -->"
$ImportLine = "@" + (Join-Path -Path $RepoRoot -ChildPath "AGENTS.global.md")
$Block = "$MarkerStart`n$ImportLine`n$MarkerEnd"

if (Test-Path -Path $ClaudeMemory) {
    $Content = Get-Content -Path $ClaudeMemory -Raw
    if ($Content -match [regex]::Escape($MarkerStart)) {
        $Pattern = [regex]::Escape($MarkerStart) + "[\s\S]*?" + [regex]::Escape($MarkerEnd)
        $Content = [regex]::Replace($Content, $Pattern, $Block)
        Set-Content -Path $ClaudeMemory -Value $Content
    } else {
        Add-Content -Path $ClaudeMemory -Value "`n$Block"
    }
} else {
    Set-Content -Path $ClaudeMemory -Value $Block
}

# --- Codex integration ---
if ($env:CODEX_HOME) {
    $CodexDir = $env:CODEX_HOME
} else {
    $CodexDir = Join-Path -Path $env:USERPROFILE -ChildPath ".codex"
}
$CodexSkillsDir = Join-Path -Path $env:USERPROFILE -ChildPath ".agents\skills"

Write-Host "Linking skills to global Codex configuration"
Link-CodexSkills -SourceDir $SourceSkills -TargetDir $CodexSkillsDir

Write-Host "Linking command workflows to global Codex configuration"
Link-CodexSkills -SourceDir $SourceSkillCommands -TargetDir $CodexSkillsDir

if (-not (Test-Path -Path $CodexDir)) {
    New-Item -ItemType Directory -Path $CodexDir | Out-Null
}

Write-Host "Registering library rulebook in global Codex guidance"
$CodexAgents = Join-Path -Path $CodexDir -ChildPath "AGENTS.md"
$CanonicalRules = Get-Content -Path (Join-Path -Path $RepoRoot -ChildPath "AGENTS.global.md") -Raw
$CodexBlock = "$MarkerStart`n$CanonicalRules"
if (-not $CanonicalRules.EndsWith("`n")) {
    $CodexBlock += "`n"
}
$CodexBlock += $MarkerEnd

if (Test-Path -Path $CodexAgents) {
    $Content = Get-Content -Path $CodexAgents -Raw
    if ($Content -match [regex]::Escape($MarkerStart)) {
        $Pattern = [regex]::Escape($MarkerStart) + "[\s\S]*?" + [regex]::Escape($MarkerEnd)
        $Content = [regex]::Replace($Content, $Pattern, $CodexBlock)
        Set-Content -Path $CodexAgents -Value $Content
    } else {
        Add-Content -Path $CodexAgents -Value "`n$CodexBlock"
    }
} else {
    Set-Content -Path $CodexAgents -Value $CodexBlock
}

$CodexOverride = Join-Path -Path $CodexDir -ChildPath "AGENTS.override.md"
if ((Test-Path -Path $CodexOverride) -and ((Get-Item -Path $CodexOverride).Length -gt 0)) {
    Write-Host "Warning: $CodexOverride takes precedence over the installed Codex guidance." -ForegroundColor Yellow
}

# --- Devin CLI integration ---
$DevinDir = Join-Path -Path $env:APPDATA -ChildPath "devin"
$DevinAgents = Join-Path -Path $DevinDir -ChildPath "AGENTS.md"
$DevinBlock = "$MarkerStart`n$ImportLine`n$MarkerEnd"

if (-not (Test-Path -Path $DevinDir)) {
    New-Item -ItemType Directory -Path $DevinDir | Out-Null
}

Write-Host "Linking skills to global Devin CLI configuration"
Link-Skills -SourceDir $SourceSkills -TargetDir (Join-Path -Path $DevinDir -ChildPath "skills")

Write-Host "Linking command skills (/work_next_item, /resume_task, /groom_backlogs) to global Devin CLI configuration"
Link-Skills -SourceDir $SourceSkillCommands -TargetDir (Join-Path -Path $DevinDir -ChildPath "skills")

Write-Host "Registering library rulebook in global Devin CLI configuration"
$CanonicalRules = Get-Content -Path (Join-Path -Path $RepoRoot -ChildPath "AGENTS.global.md") -Raw
$DevinBlock = "$MarkerStart`n$CanonicalRules"
if (-not $CanonicalRules.EndsWith("`n")) {
    $DevinBlock += "`n"
}
$DevinBlock += $MarkerEnd

if (Test-Path -Path $DevinAgents) {
    $Content = Get-Content -Path $DevinAgents -Raw
    if ($Content -match [regex]::Escape($MarkerStart)) {
        $Pattern = [regex]::Escape($MarkerStart) + "[\s\S]*?" + [regex]::Escape($MarkerEnd)
        $Content = [regex]::Replace($Content, $Pattern, $DevinBlock)
        Set-Content -Path $DevinAgents -Value $Content
    } else {
        Add-Content -Path $DevinAgents -Value "`n$DevinBlock"
    }
} else {
    Set-Content -Path $DevinAgents -Value $DevinBlock
}

Write-Host "Integration complete. HowlPlane is now globally accessible to Gemini, Claude, Codex, and Devin."
