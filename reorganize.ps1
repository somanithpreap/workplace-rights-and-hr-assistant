# reorganize.ps1 - run ONCE inside D:\hr-assistant after copying the updated .py files:
#   powershell -ExecutionPolicy Bypass -File .\reorganize.ps1
# Moves files into data\raw (originals) and data\processed (converted). Safe to run twice.

$dirs = "data\raw\csv", "data\raw\laws", "data\raw\company", "data\processed"
foreach ($d in $dirs) { New-Item -ItemType Directory -Force -Path $d | Out-Null }

function MoveIf($from, $to) {
    if (Test-Path $from) { Move-Item -Force $from $to; Write-Host "moved  $from -> $to" }
}

MoveIf "seed\*.csv"                  "data\raw\csv\"
MoveIf "api\exchange_rate_usd.json"  "data\raw\"
MoveIf "knowledge_base.jsonl"        "data\processed\"
MoveIf "knowledge_base"              "data\processed\"
MoveIf "vectors.npy"                 "data\processed\"
MoveIf "vectors_meta.json"           "data\processed\"
MoveIf "query_cache.json"            "data\processed\"
MoveIf "hr.db"                       "data\processed\"

foreach ($old in "seed", "api") {
    if ((Test-Path $old) -and -not (Get-ChildItem $old)) { Remove-Item $old; Write-Host "removed empty $old\" }
}

Write-Host ""
Write-Host "Done. Now copy by hand (from the course pack and your teammates' zip):"
Write-Host "  the 5 law PDFs                       -> data\raw\laws\"
Write-Host "  mekong-apparel-internal-work-rules.md -> data\raw\company\"
Write-Host "  the knowledge_base folder of .md cards -> data\processed\knowledge_base\  (if not moved above)"
