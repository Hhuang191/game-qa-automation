param(
    [string]$Config = "games.local.json",
    [string]$Output = "reports/latest",
    [switch]$NoVideo
)

$ErrorActionPreference = "Stop"
python -m pip install -r requirements.txt
$arguments = @("game_tester.py", "--config", $Config, "--output", $Output)
if ($NoVideo) { $arguments += "--no-video" }
python @arguments
