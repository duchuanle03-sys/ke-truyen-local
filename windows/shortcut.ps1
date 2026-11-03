param([Parameter(Mandatory = $true)][string]$InputPath)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$temporaryLink = $null

# WScript.Shell uses ANSI strings, so use Windows' Unicode shell-link interface.
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;
[ComImport, Guid("00021401-0000-0000-C000-000000000046")] class ShellLink {}
[ComImport, Guid("000214F9-0000-0000-C000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IShellLinkW {
    void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder value, int size, IntPtr findData, uint flags);
    void GetIDList(out IntPtr value);
    void SetIDList(IntPtr value);
    void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder value, int size);
    void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string value);
    void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder value, int size);
    void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string value);
    void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder value, int size);
    void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string value);
    void GetHotkey(out short value);
    void SetHotkey(short value);
    void GetShowCmd(out int value);
    void SetShowCmd(int value);
    void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder value, int size, out int index);
    void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string value, int index);
    void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string value, uint reserved);
    void Resolve(IntPtr window, uint flags);
    void SetPath([MarshalAs(UnmanagedType.LPWStr)] string value);
}
public static class BookShortcut {
    public static string[] Read(string path) {
        var link = (IShellLinkW)new ShellLink();
        try {
            ((IPersistFile)link).Load(path, 0);
            var target = new StringBuilder(32768);
            var arguments = new StringBuilder(32768);
            var directory = new StringBuilder(32768);
            var icon = new StringBuilder(32768);
            int iconIndex;
            link.GetPath(target, target.Capacity, IntPtr.Zero, 4);
            link.GetArguments(arguments, arguments.Capacity);
            link.GetWorkingDirectory(directory, directory.Capacity);
            link.GetIconLocation(icon, icon.Capacity, out iconIndex);
            return new [] { target.ToString(), arguments.ToString(), directory.ToString(), icon.ToString() + "," + iconIndex };
        } finally { Marshal.ReleaseComObject(link); }
    }
    public static void Save(string path, string target, string arguments, string workingDirectory, string icon) {
        var link = (IShellLinkW)new ShellLink();
        try {
            link.SetPath(target);
            link.SetArguments(arguments);
            link.SetWorkingDirectory(workingDirectory);
            link.SetDescription("Ke Truyen");
            link.SetIconLocation(icon, 0);
            ((IPersistFile)link).Save(path, true);
        } finally { Marshal.ReleaseComObject(link); }
    }
}
'@

function Test-OwnedShortcut([string]$Path, $Data) {
    try {
        $existing = [BookShortcut]::Read($Path)
        return ([string]::Equals($existing[0], $Data.target, [StringComparison]::OrdinalIgnoreCase) -and
                $existing[1] -ceq $Data.arguments)
    } catch { return $false }
}

try {
    $data = [IO.File]::ReadAllText($InputPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
    if ($data.identity -notmatch '^[a-f0-9]{16}$' -or $data.filename.Length -gt 120 -or
        $data.filename.IndexOfAny([IO.Path]::GetInvalidFileNameChars()) -ge 0 -or
        $data.arguments.Length -gt 16000) { throw 'Invalid shortcut input.' }
    foreach ($path in @($data.desktop, $data.target, $data.working_directory, $data.icon_path, $data.temporary)) {
        if (-not [IO.Path]::IsPathRooted($path)) { throw 'Shortcut paths must be absolute.' }
    }
    [void][IO.Directory]::CreateDirectory($data.desktop)
    $destination = $null
    foreach ($item in @(Get-ChildItem -LiteralPath $data.desktop -Filter "*$($data.identity)*.lnk" -File)) {
        if (Test-OwnedShortcut $item.FullName $data) { $destination = $item.FullName; break }
    }
    if (-not $destination) {
        for ($number = 1; $number -le 1000; $number++) {
            $suffix = if ($number -eq 1) { '' } else { " ($number)" }
            $candidate = [IO.Path]::Combine($data.desktop, $data.filename + $suffix + '.lnk')
            if (-not (Test-Path -LiteralPath $candidate)) { $destination = $candidate; break }
        }
        if (-not $destination) { throw 'Too many conflicting Desktop shortcut names.' }
    }

    $usedCover = $false
    $png = $null
    if ($data.cover_path -and [IO.File]::Exists($data.cover_path)) {
        $stream = $image = $bitmap = $graphics = $null
        try {
            if ((Get-Item -LiteralPath $data.cover_path).Length -gt 16777216) { throw 'Cover exceeds 16 MiB.' }
            Add-Type -AssemblyName System.Drawing
            $stream = [IO.File]::OpenRead($data.cover_path)
            $image = [Drawing.Image]::FromStream($stream, $false, $false)
            if ($image.Width -gt 8192 -or $image.Height -gt 8192 -or
                [long]$image.Width * [long]$image.Height -gt 16000000) { throw 'Cover dimensions exceed the icon limit.' }
            $bitmap = [Drawing.Bitmap]::new(256, 256, [Drawing.Imaging.PixelFormat]::Format32bppArgb)
            $graphics = [Drawing.Graphics]::FromImage($bitmap)
            $graphics.Clear([Drawing.Color]::Transparent)
            $graphics.InterpolationMode = [Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
            $graphics.PixelOffsetMode = [Drawing.Drawing2D.PixelOffsetMode]::HighQuality
            $scale = [Math]::Min(256.0 / $image.Width, 256.0 / $image.Height)
            $width = [Math]::Max(1, [int][Math]::Round($image.Width * $scale))
            $height = [Math]::Max(1, [int][Math]::Round($image.Height * $scale))
            $rectangle = [Drawing.Rectangle]::new([int]((256 - $width) / 2), [int]((256 - $height) / 2), $width, $height)
            $graphics.DrawImage($image, $rectangle, 0, 0, $image.Width, $image.Height, [Drawing.GraphicsUnit]::Pixel)
            $png = [IO.MemoryStream]::new()
            $bitmap.Save($png, [Drawing.Imaging.ImageFormat]::Png)
        } catch {
            if ($null -ne $png) { $png.Dispose(); $png = $null }
        } finally {
            if ($null -ne $graphics) { $graphics.Dispose() }
            if ($null -ne $bitmap) { $bitmap.Dispose() }
            if ($null -ne $image) { $image.Dispose() }
            if ($null -ne $stream) { $stream.Dispose() }
        }
    }
    if ($null -ne $png) {
        $writer = $null
        try {
            $temporaryIcon = [IO.Path]::Combine($data.temporary, 'cover.ico')
            $writer = [IO.BinaryWriter]::new([IO.File]::Create($temporaryIcon))
            $writer.Write([uint16]0); $writer.Write([uint16]1); $writer.Write([uint16]1)
            $writer.Write([byte]0); $writer.Write([byte]0); $writer.Write([byte]0); $writer.Write([byte]0)
            $writer.Write([uint16]1); $writer.Write([uint16]32)
            $writer.Write([uint32]$png.Length); $writer.Write([uint32]22)
            $writer.Write($png.ToArray())
            $writer.Dispose(); $writer = $null
            if ([IO.File]::Exists($data.icon_path)) {
                if ((Get-FileHash -LiteralPath $temporaryIcon).Hash -cne (Get-FileHash -LiteralPath $data.icon_path).Hash) {
                    [IO.File]::Replace($temporaryIcon, $data.icon_path, [NullString]::Value)
                }
            }
            else { [IO.File]::Move($temporaryIcon, $data.icon_path) }
            $usedCover = $true
        } finally {
            if ($null -ne $writer) { $writer.Dispose() }
            $png.Dispose()
        }
    }

    $iconPath = if ($usedCover) { $data.icon_path } else { $data.target }
    if ([IO.File]::Exists($destination)) {
        $existing = [BookShortcut]::Read($destination)
        if ([string]::Equals($existing[0], $data.target, [StringComparison]::OrdinalIgnoreCase) -and
            $existing[1] -ceq $data.arguments -and
            [string]::Equals($existing[2], $data.working_directory, [StringComparison]::OrdinalIgnoreCase) -and
            [string]::Equals($existing[3], $iconPath + ',0', [StringComparison]::OrdinalIgnoreCase)) {
            @{ path = $destination; used_cover = $usedCover } | ConvertTo-Json -Compress
            exit 0
        }
    }
    $temporaryLink = [IO.Path]::Combine($data.desktop, '.reader-' + [Guid]::NewGuid().ToString('N') + '.lnk')
    [BookShortcut]::Save($temporaryLink, $data.target, $data.arguments, $data.working_directory, $iconPath)
    if ([IO.File]::Exists($destination)) {
        if (-not (Test-OwnedShortcut $destination $data)) { throw 'Desktop shortcut changed; the existing file was preserved.' }
        [IO.File]::Replace($temporaryLink, $destination, [NullString]::Value)
    } else { [IO.File]::Move($temporaryLink, $destination) }
    $temporaryLink = $null
    @{ path = $destination; used_cover = $usedCover } | ConvertTo-Json -Compress
} catch {
    $failure = $_.Exception
    while ($null -ne $failure.InnerException) { $failure = $failure.InnerException }
    @{ error = $failure.Message; hresult = ('0x{0:X8}' -f $failure.HResult) } | ConvertTo-Json -Compress
    exit 1
} finally {
    if ($temporaryLink -and [IO.File]::Exists($temporaryLink)) { [IO.File]::Delete($temporaryLink) }
}
