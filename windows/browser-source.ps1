$ErrorActionPreference = 'Stop'
$WarningPreference = 'Stop'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$socket = $null
$cancel = $null
$cacheDisabled = $false

function Receive-Reply {
    $buffer = New-Object byte[] 65536
    $stream = New-Object System.IO.MemoryStream
    try {
        do {
            $segment = New-Object 'System.ArraySegment[byte]' -ArgumentList @(,$buffer)
            $part = $socket.ReceiveAsync($segment, $cancel.Token).GetAwaiter().GetResult()
            if ($part.MessageType -ne [System.Net.WebSockets.WebSocketMessageType]::Text) { throw 'closed' }
            $stream.Write($buffer, 0, $part.Count)
            if ($stream.Length -gt 16777216) { throw 'oversized' }
        } until ($part.EndOfMessage)
        return ([Text.Encoding]::UTF8.GetString($stream.ToArray()) | ConvertFrom-Json)
    } finally { $stream.Dispose() }
}

function Invoke-Command($method, $parameters) {
    $script:sequence += 1
    $id = $script:sequence
    $command = @{id=$id; method=$method; params=$parameters} | ConvertTo-Json -Depth 32 -Compress
    $bytes = [Text.Encoding]::UTF8.GetBytes($command)
    $segment = New-Object 'System.ArraySegment[byte]' -ArgumentList @(,$bytes)
    $null = $socket.SendAsync($segment, [System.Net.WebSockets.WebSocketMessageType]::Text, $true, $cancel.Token).GetAwaiter().GetResult()
    do {
        $reply = Receive-Reply
        if ($reply.method -eq 'Network.responseReceived') {
            $event = $reply.params
            if ($event.type -eq 'Document' -and $event.response.url -eq $inputData.url -and
                $event.response.status -ge 400 -and $event.response.status -le 599) {
                if ($script:documentFailures.Count -ge 32) { $script:documentFailures.RemoveAt(0) }
                $null = $script:documentFailures.Add(@{frame=$event.frameId; loader=$event.loaderId; status=[int]$event.response.status})
            }
        } elseif ($reply.method -eq 'Network.requestWillBeSent') {
            $event = $reply.params
            for ($index = $script:documentRequests.Count - 1; $index -ge 0; $index--) {
                if ($script:documentRequests[$index].id -eq $event.requestId) { $script:documentRequests.RemoveAt($index) }
            }
            if ($event.type -eq 'Document' -and $event.request.url -eq $inputData.url -and $event.requestId) {
                if ($script:documentRequests.Count -ge 32) { $script:documentRequests.RemoveAt(0) }
                $null = $script:documentRequests.Add(@{id=$event.requestId; frame=$event.frameId; loader=$event.loaderId})
            }
        } elseif ($reply.method -eq 'Network.loadingFailed') {
            $event = $reply.params
            if ($event.type -eq 'Document' -and -not $event.canceled -and $event.errorText -notmatch '\bERR_ABORTED\b') {
                foreach ($request in $script:documentRequests) {
                    if ($request.id -eq $event.requestId) {
                        if ($script:documentFailures.Count -ge 32) { $script:documentFailures.RemoveAt(0) }
                        $null = $script:documentFailures.Add(@{frame=$request.frame; loader=$request.loader; code='network_error'})
                    }
                }
            }
        }
    } until ($reply.id -eq $id)
    if ($reply.error) { throw 'protocol' }
    return $reply.result
}

function Complete-SourceError($sourceError) {
    [Console]::Write((@{url=$inputData.url; value=@{error=$sourceError}} | ConvertTo-Json -Depth 8 -Compress))
    exit 0
}

function Test-DocumentFailure($frame) {
    foreach ($failure in $script:documentFailures) {
        if ($failure.frame -eq $navigation.frameId -and $failure.loader -and
            ($failure.loader -eq $navigation.loaderId -or
             ($failure.code -eq 'network_error' -and $previousLoader -and $failure.loader -ne $previousLoader) -or
             ($frame.url -eq $inputData.url -and $frame.id -eq $navigation.frameId -and
              $failure.loader -eq $frame.loaderId -and $previousLoader -and $failure.loader -ne $previousLoader))) {
            if ($failure.code -eq 'network_error') { Complete-SourceError @{code='network_error'} }
            Complete-SourceError @{code='http_error'; http_status=$failure.status}
        }
    }
}

try {
    $inputData = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $address = [Uri]$inputData.websocket
    if ($address.Scheme -ne 'ws' -or $address.Host -ne '127.0.0.1' -or $address.UserInfo -or $address.Query -or $address.Fragment -or
        $address.AbsolutePath -notmatch '^/devtools/page/[A-Za-z0-9_-]+$') { throw 'address' }
    $site = [Uri]$inputData.url
    if ($site.Scheme -ne 'https' -or $site.Host -ne 'sangtacviet.vip' -or $site.Port -ne 443 -or $site.UserInfo -or
        $inputData.url -match '[\s\\]') { throw 'website' }
    $cancel = New-Object System.Threading.CancellationTokenSource
    $cancel.CancelAfter([int][Math]::Ceiling([double]$inputData.timeout * 1000))
    $socket = New-Object System.Net.WebSockets.ClientWebSocket
    $socket.Options.Proxy = $null
    $null = $socket.ConnectAsync($address, $cancel.Token).GetAwaiter().GetResult()
    $script:sequence = 0
    $script:documentFailures = New-Object 'System.Collections.Generic.List[object]'
    $script:documentRequests = New-Object 'System.Collections.Generic.List[object]'
    $null = Invoke-Command 'Page.bringToFront' @{}
    $previousTree = Invoke-Command 'Page.getFrameTree' @{}
    $previousLoader = $previousTree.frameTree.frame.loaderId
    # Cached site scripts can leave revisited chapters stuck at their loading placeholder.
    $cacheDisabled = $true
    $null = Invoke-Command 'Network.setCacheDisabled' @{cacheDisabled=$true}
    $null = Invoke-Command 'Network.enable' @{maxTotalBufferSize=0; maxResourceBufferSize=0; maxPostDataSize=0}
    $script:documentFailures.Clear()
    $script:documentRequests.Clear()
    $reloading = $previousTree.frameTree.frame.url -eq $inputData.url
    if ($reloading) {
        $null = Invoke-Command 'Page.reload' @{ignoreCache=$false}
        $navigation = @{frameId=$previousTree.frameTree.frame.id}
    } else {
        $navigation = Invoke-Command 'Page.navigate' @{url=$inputData.url}
    }
    if ($navigation.errorText) { Complete-SourceError @{code='network_error'} }
    $expected = ConvertTo-Json -InputObject ([string]$inputData.url) -Compress
    $expression = '(async () => { const expected = ' + $expected + '; if (location.href !== expected || document.readyState === "loading") return null; const value = await (' + $inputData.expression + '); if (location.href !== expected || value == null) return null; return {url: location.href, value}; })()'
    while (-not $cancel.IsCancellationRequested) {
        $tree = Invoke-Command 'Page.getFrameTree' @{}
        $frame = $tree.frameTree.frame
        Test-DocumentFailure $frame
        # The site's language picker can reload this URL with a later loader.
        if ($frame.url -eq $inputData.url -and $frame.id -eq $navigation.frameId -and
            ((-not $reloading -and -not $navigation.loaderId) -or
             ($navigation.loaderId -and $frame.loaderId -eq $navigation.loaderId) -or
             ($previousLoader -and $frame.loaderId -and $frame.loaderId -ne $previousLoader))) {
            $evaluation = Invoke-Command 'Runtime.evaluate' @{expression=$expression; returnByValue=$true; awaitPromise=$true}
            Test-DocumentFailure $frame
            if ($evaluation.exceptionDetails) { throw 'expression' }
            $result = $evaluation.result.value
            if ($null -ne $result -and $result.url -eq $inputData.url) {
                [Console]::Write(($result | ConvertTo-Json -Depth 64 -Compress))
                exit 0
            }
        }
        Start-Sleep -Milliseconds 100
    }
    throw 'timeout'
} catch {
    $code = if ($cancel -and $cancel.IsCancellationRequested) { 'browser_timeout' } else { 'browser_error' }
    [Console]::Write((@{error=$code} | ConvertTo-Json -Compress))
    exit 1
} finally {
    if ($cacheDisabled -and $socket -and $socket.State -eq [System.Net.WebSockets.WebSocketState]::Open) {
        $readCancel = $cancel
        $cancel = New-Object System.Threading.CancellationTokenSource
        $cancel.CancelAfter(250)
        try { $null = Invoke-Command 'Network.setCacheDisabled' @{cacheDisabled=$false} } catch {} finally {
            $cancel.Dispose()
            $cancel = $readCancel
        }
    }
    if ($socket) { $socket.Dispose() }
    if ($cancel) { $cancel.Dispose() }
}
