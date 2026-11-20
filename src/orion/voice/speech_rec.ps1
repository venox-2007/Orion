param(
    [double]$TimeoutSec = 6.0
)

try {
    Add-Type -AssemblyName System.Speech

    $recs = [System.Speech.Recognition.SpeechRecognitionEngine]::InstalledRecognizers()
    if (-not $recs -or $recs.Count -eq 0) {
        [Console]::Error.WriteLine("No speech recognizers installed.")
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
        [Console]::Error.WriteLine("Microphone unavailable or audio input error: $($_.Exception.Message)")
        Write-Output ""
        exit 0
    }

    # Configure timeouts and lower rejection threshold for laptop microphones
    $engine.BabbleTimeout = [TimeSpan]::FromSeconds(0)
    $engine.InitialSilenceTimeout = [TimeSpan]::FromSeconds($TimeoutSec)
    $engine.EndSilenceTimeout = [TimeSpan]::FromMilliseconds(150)
    $engine.EndSilenceTimeoutAmbiguous = [TimeSpan]::FromMilliseconds(500)

    # 1. Weighted Wake-Word Grammar with engine culture matching
    $choices = New-Object System.Speech.Recognition.Choices
    $choices.Add([string[]]@(
        "Orion", "Hey Orion", "Hi Orion", "Ok Orion",
        "Orion open", "Orion close", "Orion read screen", "Orion ocr",
        "Orion click", "Orion type", "Orion status", "Orion help", "Orion stop",
        "Orion sleep", "Orion turn off screen", "Orion lock", "Orion where is my mouse",
        "Orion start", "Orion pause", "Orion resume"
    ))
    $wakeBuilder = New-Object System.Speech.Recognition.GrammarBuilder($choices)
    $wakeBuilder.Culture = $engine.RecognizerInfo.Culture
    $wakeGrammar = New-Object System.Speech.Recognition.Grammar($wakeBuilder)
    $wakeGrammar.Name = "WakeWordGrammar"
    $wakeGrammar.Weight = 1.0
    $engine.LoadGrammar($wakeGrammar)

    # 2. General Dictation Grammar
    $dictGrammar = New-Object System.Speech.Recognition.DictationGrammar
    $dictGrammar.Name = "DictationGrammar"
    $dictGrammar.Weight = 0.5
    $engine.LoadGrammar($dictGrammar)

    $timeSpan = [TimeSpan]::FromSeconds($TimeoutSec)
    $res = $engine.Recognize($timeSpan)
    if ($res -and $res.Text) {
        $conf = [math]::Round($res.Confidence, 3)
        [Console]::Error.WriteLine("Raw hypothesis: '$($res.Text)' (confidence: $conf)")
        if ($res.Confidence -ge 0.35) {
            Write-Output $res.Text
        } else {
            [Console]::Error.WriteLine("Rejected hypothesis '$($res.Text)': confidence $conf < 0.35")
            Write-Output ""
        }
    } else {
        [Console]::Error.WriteLine("No speech detected within timeout ($TimeoutSec s).")
        Write-Output ""
    }
} catch {
    [Console]::Error.WriteLine("Speech recognition error: $($_.Exception.Message)")
    Write-Output ""
    exit 0
}
