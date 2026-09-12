<#
    为 *-pc-windows-msvc 目标的 cargo 构建准备真正的 MSVC 链接器环境。

    `cargo build --target x86_64-pc-windows-msvc` 需要一个真正的 MSVC link.exe：

      * 普通 PowerShell 里它不在 PATH 上。本机实测：`Get-Command link.exe` 为空，
        LIB / INCLUDE 也未设置；
      * Git Bash 里 PATH 上的 /usr/bin/link.exe 是 GNU coreutils 的 link，会遮蔽
        MSVC 链接器，报 "missing operand after ..." 之类的错误。

    这里用 vswhere（回退到固定目录）定位工具链，把 bin 前置到 PATH，并补齐
    LIB / INCLUDE。

    探测失败只告警、不抛异常：cargo 自身在部分版本上能自动发现工具链，让构建
    自己决定成败——不伪造成功。
#>

function Initialize-MsvcEnvironment {
    [CmdletBinding()]
    param()

    # 不依赖 ${env:ProgramFiles(x86)}：本机 PowerShell 5.1 里它是空字符串。
    $pf86 = "C:\Program Files (x86)"

    $vsRoot = $null
    $vswhere = Join-Path $pf86 "Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path -LiteralPath $vswhere -PathType Leaf) {
        $found = & $vswhere -latest -products * `
            -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
            -property installationPath 2>$null
        if ($found) { $vsRoot = ([string]($found | Select-Object -First 1)).Trim() }
    }

    $candidates = @()
    if ($vsRoot) { $candidates += $vsRoot }
    $candidates += @(
        (Join-Path $pf86 "Microsoft Visual Studio\2022\BuildTools"),
        (Join-Path $pf86 "Microsoft Visual Studio\2022\Community"),
        (Join-Path $pf86 "Microsoft Visual Studio\2022\Professional"),
        (Join-Path $pf86 "Microsoft Visual Studio\2022\Enterprise")
    )

    $toolchain = $null
    foreach ($root in $candidates) {
        if (-not $root) { continue }
        $msvcDir = Join-Path $root "VC\Tools\MSVC"
        if (-not (Test-Path -LiteralPath $msvcDir)) { continue }
        $version = Get-ChildItem -LiteralPath $msvcDir -Directory -ErrorAction SilentlyContinue |
            Sort-Object { try { [version]$_.Name } catch { [version]"0.0" } } -Descending |
            Select-Object -First 1
        if (-not $version) { continue }
        $binDir = Join-Path $version.FullName "bin\Hostx64\x64"
        if (Test-Path -LiteralPath (Join-Path $binDir "link.exe") -PathType Leaf) {
            $toolchain = @{ Root = $root; Msvc = $version.FullName; Bin = $binDir }
            break
        }
    }

    if (-not $toolchain) {
        Write-Warning "未找到 MSVC 工具链；若 cargo 链接失败，请改用 VS 开发者命令提示符运行本脚本。"
        return
    }

    $sdkIncludeRoot = Join-Path $pf86 "Windows Kits\10\Include"
    $sdk = Get-ChildItem -LiteralPath $sdkIncludeRoot -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like "10.*" } |
        Sort-Object { try { [version]$_.Name } catch { [version]"0.0" } } -Descending |
        Select-Object -First 1

    $libParts = @((Join-Path $toolchain.Msvc "lib\x64"))
    $includeParts = @((Join-Path $toolchain.Msvc "include"))
    if ($sdk) {
        $libParts += @(
            (Join-Path $sdk.FullName "ucrt\x64"),
            (Join-Path $sdk.FullName "um\x64")
        )
        $includeParts += @(
            (Join-Path $sdk.FullName "ucrt"),
            (Join-Path $sdk.FullName "shared"),
            (Join-Path $sdk.FullName "um"),
            (Join-Path $sdk.FullName "winrt"),
            (Join-Path $sdk.FullName "cppwinrt")
        )
    }

    # 已经处在开发者命令提示符里时，这两个变量的取值本来就是完整且正确的，
    # 不要用我们拼出来的更短的列表覆盖它。
    if (-not $env:LIB) { $env:LIB = ($libParts -join ";") }
    if (-not $env:INCLUDE) { $env:INCLUDE = ($includeParts -join ";") }

    if (($env:PATH -split ";") -notcontains $toolchain.Bin) {
        $env:PATH = "$($toolchain.Bin);$env:PATH"
    }

    Write-Host "cargo 链接器: $(Join-Path $toolchain.Bin 'link.exe')"
}
