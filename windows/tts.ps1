param(
    [ValidateSet('Voices', 'Synthesize')][string]$Action = 'Voices',
    [string]$TextPath = '',
    [string]$OutputPath = '',
    [string]$VoiceId = ''
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$synthesizer = $null
$speechStream = $null
$readStream = $null
$outputStream = $null

try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    [Windows.Media.SpeechSynthesis.SpeechSynthesizer,Windows.Media.SpeechSynthesis,ContentType=WindowsRuntime] | Out-Null
    [Windows.Media.SpeechSynthesis.SpeechSynthesisStream,Windows.Media.SpeechSynthesis,ContentType=WindowsRuntime] | Out-Null

    $installed = @([Windows.Media.SpeechSynthesis.SpeechSynthesizer]::AllVoices)
    if ($Action -eq 'Voices') {
        $list = @($installed | ForEach-Object {
            @{ id = $_.Id; name = $_.DisplayName; language = $_.Language; gender = $_.Gender.ToString(); offline = $true }
        })
        @{ voices = $list } | ConvertTo-Json -Depth 5 -Compress
        exit 0
    }
    if ($installed.Count -eq 0) { throw 'No installed Windows speech voice is available.' }
    if (-not $TextPath -or -not $OutputPath) { throw 'TextPath and OutputPath are required.' }
    $text = [IO.File]::ReadAllText($TextPath, [Text.Encoding]::UTF8)
    if ([string]::IsNullOrWhiteSpace($text) -or $text.Length -gt 50000 -or $text.Contains([char]0)) {
        throw 'Speech text must contain 1 to 50000 characters without NUL.'
    }
    $synthesizer = New-Object Windows.Media.SpeechSynthesis.SpeechSynthesizer
    if ($VoiceId) {
        $voice = $installed | Where-Object { $_.Id -ceq $VoiceId } | Select-Object -First 1
        if ($null -eq $voice) { throw 'Requested voice is not installed. Select an available Windows voice.' }
        $synthesizer.Voice = $voice
    }
    $operationMethod = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    } | Select-Object -First 1
    $operation = $synthesizer.SynthesizeTextToStreamAsync($text)
    $task = $operationMethod.MakeGenericMethod([Windows.Media.SpeechSynthesis.SpeechSynthesisStream]).Invoke($null, @($operation))
    $speechStream = $task.GetAwaiter().GetResult()
    if ($speechStream.Size -le 44 -or $speechStream.Size -gt 268435456) { throw 'Speech output is empty or exceeds the 256 MiB WAV limit.' }
    $readStream = [System.IO.WindowsRuntimeStreamExtensions]::AsStreamForRead($speechStream.GetInputStreamAt(0))
    $outputStream = [IO.File]::Open($OutputPath, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::None)
    $readStream.CopyTo($outputStream)
    $outputStream.Dispose()
    $outputStream = $null
    @{ written = $true; voice_id = $synthesizer.Voice.Id; language = $synthesizer.Voice.Language } | ConvertTo-Json -Compress
} catch {
    $failure = $_.Exception
    while ($null -ne $failure.InnerException) { $failure = $failure.InnerException }
    @{ error = $failure.Message; hresult = ('0x{0:X8}' -f $failure.HResult) } | ConvertTo-Json -Compress
    exit 1
} finally {
    if ($null -ne $outputStream) { $outputStream.Dispose() }
    if ($null -ne $readStream) { $readStream.Dispose() }
    if ($null -ne $speechStream) { $speechStream.Dispose() }
    if ($null -ne $synthesizer) { $synthesizer.Dispose() }
}
