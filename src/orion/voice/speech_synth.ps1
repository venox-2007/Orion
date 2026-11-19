param(
    [string]$Text,
    [string]$TextFile,
    [int]$Rate = 1
)

try {
    Add-Type -AssemblyName System.Speech
    $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
    $synth.Rate = [Math]::Max(-10, [Math]::Min(10, $Rate))

    $toSpeak = ""
    if ($TextFile -and (Test-Path $TextFile)) {
        $toSpeak = Get-Content -Path $TextFile -Raw -Encoding UTF8
    } elseif ($Text) {
        $toSpeak = $Text
    } else {
        $toSpeak = [Console]::In.ReadToEnd()
    }

    if ($toSpeak -and $toSpeak.Trim()) {
        $synth.Speak($toSpeak)
    }
} catch {
    Write-Error $_.Exception.Message
    exit 1
}
