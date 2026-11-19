param(
    [double]$TimeoutSec = 6.0
)

try {
    Add-Type -AssemblyName System.Speech

    $recs = [System.Speech.Recognition.SpeechRecognitionEngine]::InstalledRecognizers()
    if (-not $recs -or $recs.Count -eq 0) {
        Write-Output ""
        exit 0
    }

    $engine = $null
    # Try user culture first, fallback to first available
    $userCulture = [System.Globalization.CultureInfo]::CurrentCulture
    foreach ($r in $recs) {
        if ($r.Culture.Name -eq $userCulture.Name) {
            $engine = New-Object System.Speech.Recognition.SpeechRecognitionEngine($r.Culture)
            break
        }
    }
    if (-not $engine) {
        $engine = New-Object System.Speech.Recognition.SpeechRecognitionEngine($recs[0].Culture)
    }

    try {
        $engine.SetInputToDefaultAudioDevice()
    } catch {
        # No microphone available
        Write-Output ""
        exit 0
    }

    $grammar = New-Object System.Speech.Recognition.DictationGrammar
    $engine.LoadGrammar($grammar)

    $timeSpan = [TimeSpan]::FromSeconds($TimeoutSec)
    $res = $engine.Recognize($timeSpan)
    if ($res -and $res.Text) {
        Write-Output $res.Text
    } else {
        Write-Output ""
    }
} catch {
    Write-Output ""
    exit 0
}
