$ErrorActionPreference = 'Stop'
$root = 'D:\Work\xbud_module'
$sdk = 'D:\android-sdk'
$bt = "$sdk\bt\android-15"
$androidJar = "$sdk\platform\android-35\android.jar"
$build = "$root\build"

New-Item -ItemType Directory -Force -Path $build, "$build\stubs", "$build\classes", "$build\dex" | Out-Null

Write-Host '== stubs =='
& javac -nowarn -source 8 -target 8 -bootclasspath $androidJar -d "$build\stubs" (Get-ChildItem "$root\stubs" -Recurse -Filter *.java | ForEach-Object FullName)
if ($LASTEXITCODE -ne 0) { throw 'stub compile failed' }

Write-Host '== module =='
& javac -nowarn -source 8 -target 8 -bootclasspath $androidJar -classpath "$build\stubs" -d "$build\classes" (Get-ChildItem "$root\src" -Recurse -Filter *.java | ForEach-Object FullName)
if ($LASTEXITCODE -ne 0) { throw 'module compile failed' }

Write-Host '== dex =='
& jar cf "$build\module.jar" -C "$build\classes" .
& "$bt\d8.bat" --min-api 26 --lib $androidJar --output "$build\dex" "$build\module.jar"
if ($LASTEXITCODE -ne 0) { throw 'd8 failed' }

Write-Host '== link =='
& "$bt\aapt2.exe" link -o "$build\base.apk" -I $androidJar --manifest "$root\AndroidManifest.xml" --min-sdk-version 26 --target-sdk-version 35
if ($LASTEXITCODE -ne 0) { throw 'aapt2 link failed' }

Copy-Item "$build\base.apk" "$build\unsigned.apk" -Force
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::Open("$build\unsigned.apk", 'Update')
foreach ($pair in @(
    @{ Path = "$build\dex\classes.dex"; Name = 'classes.dex' },
    @{ Path = "$root\assets\xposed_init"; Name = 'assets/xposed_init' }
)) {
    $entry = $zip.GetEntry($pair.Name)
    if ($entry) { $entry.Delete() }
    [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $pair.Path, $pair.Name, 'Optimal')
}
$zip.Dispose()

Write-Host '== align/sign =='
& "$bt\zipalign.exe" -f 4 "$build\unsigned.apk" "$build\aligned.apk"
if ($LASTEXITCODE -ne 0) { throw 'zipalign failed' }

if (-not (Test-Path "$root\debug.keystore")) {
    & keytool -genkeypair -keystore "$root\debug.keystore" -storepass android -keypass android `
        -alias androiddebugkey -keyalg RSA -keysize 2048 -validity 10000 `
        -dname 'CN=Android Debug,O=Android,C=US'
}
& "$bt\apksigner.bat" sign --ks "$root\debug.keystore" --ks-pass pass:android --key-pass pass:android `
    --v1-signing-enabled true --v2-signing-enabled true `
    --out "$build\xbudloc.apk" "$build\aligned.apk"
if ($LASTEXITCODE -ne 0) { throw 'apksigner failed' }

Get-Item "$build\xbudloc.apk" | Select-Object FullName, Length
