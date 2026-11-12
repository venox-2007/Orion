param(
    [Parameter(Mandatory=$true)]
    [string]$ImagePath
)

try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    Add-Type -AssemblyName System.Drawing

    if (-not (Test-Path $ImagePath)) {
        @{
            success = $false
            error = "Image file not found: $ImagePath"
            text = ""
            lines = @()
            words = @()
        } | ConvertTo-Json -Depth 5 -Compress
        exit 0
    }

    $fullPath = (Resolve-Path $ImagePath).Path

    # Helper for awaiting WinRT IAsyncOperation
    $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { 
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' 
    })[0]

    function Await-WinRT($WinRtTask, $ResultType) {
        $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
        $netTask = $asTask.Invoke($null, @($WinRtTask))
        $netTask.Wait(-1) | Out-Null
        return $netTask.Result
    }

    # WinRT types
    [Windows.Media.Ocr.OcrEngine, Windows.Foundation.Diagnostics, ContentType = WindowsRuntime] | Out-Null
    [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation.Diagnostics, ContentType = WindowsRuntime] | Out-Null
    [Windows.Storage.StorageFile, Windows.Foundation.Diagnostics, ContentType = WindowsRuntime] | Out-Null

    $ocr = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
    if (-not $ocr) {
        $lang = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]::new('en-US')
        $ocr = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
    }

    if (-not $ocr) {
        @{
            success = $false
            error = "Windows OCR Engine could not be initialized."
            text = ""
            lines = @()
            words = @()
        } | ConvertTo-Json -Depth 5 -Compress
        exit 0
    }

    $file = Await-WinRT ([Windows.Storage.StorageFile]::GetFileFromPathAsync($fullPath)) ([Windows.Storage.StorageFile])
    $stream = Await-WinRT ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Await-WinRT ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $softwareBitmap = Await-WinRT ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $ocrResult = Await-WinRT ($ocr.RecognizeAsync($softwareBitmap)) ([Windows.Media.Ocr.OcrResult])

    $linesOut = @()
    $wordsOut = @()

    foreach ($line in $ocrResult.Lines) {
        $lineWords = @()
        foreach ($w in $line.Words) {
            $wordObj = @{
                text = $w.Text
                x = [int][Math]::Round($w.BoundingRect.X)
                y = [int][Math]::Round($w.BoundingRect.Y)
                w = [int][Math]::Round($w.BoundingRect.Width)
                h = [int][Math]::Round($w.BoundingRect.Height)
            }
            $lineWords += $wordObj
            $wordsOut += $wordObj
        }
        $linesOut += @{
            text = $line.Text
            words = $lineWords
        }
    }

    $stream.Dispose()
    $softwareBitmap.Dispose()

    @{
        success = $true
        text = $ocrResult.Text
        lines = $linesOut
        words = $wordsOut
    } | ConvertTo-Json -Depth 5 -Compress

} catch {
    @{
        success = $false
        error = $_.Exception.Message
        text = ""
        lines = @()
        words = @()
    } | ConvertTo-Json -Depth 5 -Compress
}
