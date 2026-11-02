param(
    [ValidateSet('Capabilities', 'Extract')][string]$Action = 'Capabilities',
    [string]$Path = '',
    [string]$Language = ''
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)

try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    [Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime] | Out-Null
    [Windows.Data.Pdf.PdfDocument,Windows.Data.Pdf,ContentType=WindowsRuntime] | Out-Null
    [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime] | Out-Null
    [Windows.Graphics.Imaging.BitmapDecoder,Windows.Foundation,ContentType=WindowsRuntime] | Out-Null
    [Windows.Graphics.Imaging.SoftwareBitmap,Windows.Foundation,ContentType=WindowsRuntime] | Out-Null
    [Windows.Storage.Streams.InMemoryRandomAccessStream,Windows.Storage.Streams,ContentType=WindowsRuntime] | Out-Null
    [Windows.Globalization.Language,Windows.Globalization,ContentType=WindowsRuntime] | Out-Null

    $languages = @([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | ForEach-Object {
        @{ tag = $_.LanguageTag; name = $_.DisplayName }
    })
    if ($Action -eq 'Capabilities') {
        @{ pdf_rendering = $true; ocr_available = ($languages.Count -gt 0); languages = $languages; max_image_dimension = [Windows.Media.Ocr.OcrEngine]::MaxImageDimension } | ConvertTo-Json -Depth 6 -Compress
        exit 0
    }

    $operationMethod = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    } | Select-Object -First 1
    $actionMethod = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and -not $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction'
    } | Select-Object -First 1
    function Wait-Operation($operation, [Type]$type) {
        $task = $operationMethod.MakeGenericMethod($type).Invoke($null, @($operation))
        $task.GetAwaiter().GetResult()
    }
    function Wait-Action($operation) {
        $task = $actionMethod.Invoke($null, @($operation))
        $task.GetAwaiter().GetResult() | Out-Null
    }

    if ($Language) {
        if (-not ($languages | Where-Object { $_.tag -eq $Language })) {
            throw "OCR language '$Language' is not installed. Installed: $($languages.tag -join ', ')."
        }
        $languageObject = New-Object Windows.Globalization.Language($Language)
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($languageObject)
    } else {
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
        if ($null -eq $engine -and $languages.Count -gt 0) {
            $languageObject = New-Object Windows.Globalization.Language($languages[0].tag)
            $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($languageObject)
        }
    }
    if ($null -eq $engine) { throw 'No usable Windows OCR language is installed.' }
    $file = Wait-Operation ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
    $document = Wait-Operation ([Windows.Data.Pdf.PdfDocument]::LoadFromFileAsync($file)) ([Windows.Data.Pdf.PdfDocument])
    if ($document.PageCount -gt 500) { throw 'PDF exceeds the 500-page import limit.' }
    if ($document.PageCount -eq 0) { throw 'PDF has no pages.' }
    $pages = New-Object 'System.Collections.Generic.List[object]'
    for ($index = 0; $index -lt $document.PageCount; $index++) {
        $page = $document.GetPage($index)
        $stream = New-Object Windows.Storage.Streams.InMemoryRandomAccessStream
        $bitmap = $null
        try {
            $options = New-Object Windows.Data.Pdf.PdfPageRenderOptions
            $maxDimension = [Math]::Min(4000, [Windows.Media.Ocr.OcrEngine]::MaxImageDimension)
            $scale = [Math]::Min(3.0, $maxDimension / [Math]::Max($page.Size.Width, $page.Size.Height))
            $options.DestinationWidth = [uint32][Math]::Max(1, [Math]::Floor($page.Size.Width * $scale))
            $options.DestinationHeight = [uint32][Math]::Max(1, [Math]::Floor($page.Size.Height * $scale))
            Wait-Action ($page.RenderToStreamAsync($stream, $options))
            $stream.Seek(0)
            $decoder = Wait-Operation ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
            $bitmap = Wait-Operation ($decoder.GetSoftwareBitmapAsync([Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8, [Windows.Graphics.Imaging.BitmapAlphaMode]::Premultiplied)) ([Windows.Graphics.Imaging.SoftwareBitmap])
            $result = Wait-Operation ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
            $text = (@($result.Lines | ForEach-Object { $_.Text }) -join "`n")
            $pages.Add(@{ page = $index + 1; text = $text })
        } finally {
            if ($null -ne $bitmap) { $bitmap.Dispose() }
            $stream.Dispose()
            $page.Dispose()
        }
    }
    @{ language = $engine.RecognizerLanguage.LanguageTag; pages = @($pages.ToArray()) } | ConvertTo-Json -Depth 6 -Compress
} catch {
    @{ error = $_.Exception.Message } | ConvertTo-Json -Compress
    exit 1
}
