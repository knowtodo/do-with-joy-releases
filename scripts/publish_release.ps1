$ErrorActionPreference = "Stop"

$app = $env:APP_ID
$version = $env:APP_VERSION
$zipPath = $env:ZIP_PATH
$expectedZipSha256 = $env:ZIP_SHA256
$releaseNotes = $env:RELEASE_NOTES
$token = $env:PUBLISH_TOKEN

if ($app -notin @("timesaver", "infofinder")) { throw "Unsupported application." }
if ($version -notmatch "^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$") {
    throw "Version must be in X.Y.Z format."
}
if ($env:PUBLISH_CONFIRMATION -ne "PUBLISH $app $version") {
    throw "Explicit publication confirmation does not match."
}
if ([string]::IsNullOrWhiteSpace($releaseNotes)) { throw "At least one release note is required." }
if (-not (Test-Path -LiteralPath $zipPath -PathType Leaf)) { throw "Validated candidate ZIP is missing." }
if ($expectedZipSha256 -notmatch "^[0-9a-fA-F]{64}$") { throw "Candidate ZIP SHA-256 is invalid." }
if ([string]::IsNullOrWhiteSpace($token)) { throw "Scoped GitHub App token is missing." }

$repository = "knowtodo/do-with-joy-releases"
$tag = "$($app)-v$($version)"
if ($app -eq "timesaver") {
    $assetName = "TIMESAVER-$version.zip"
    $title = "TIMESAVER $version"
} else {
    $assetName = "InfoFinder-$version.zip"
    $title = "INFOFINDER $version"
}
if ([System.IO.Path]::GetFileName($zipPath) -ne $assetName) {
    throw "Candidate ZIP name does not match the selected application and version."
}

$headers = @{
    Accept = "application/vnd.github+json"
    Authorization = "Bearer $token"
    "X-GitHub-Api-Version" = "2022-11-28"
}
$releaseApi = "https://api.github.com/repos/$repository/releases/tags/$tag"
$existingRelease = $null
try {
    $existingRelease = Invoke-RestMethod -Uri $releaseApi -Headers $headers
} catch {
    $statusCode = 0
    if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
        $statusCode = [int]$_.Exception.Response.StatusCode
    }
    if ($statusCode -ne 404) {
        throw "Could not safely determine whether release $tag already exists."
    }
}
if ($existingRelease) {
    throw "Release or tag $tag already exists. It will not be overwritten."
}

$gh = Get-Command gh -ErrorAction Stop
$notesPath = Join-Path $env:RUNNER_TEMP "release-notes-$app-$version.md"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($notesPath, $releaseNotes.Trim() + [Environment]::NewLine, $utf8NoBom)
$createArgs = @(
    "release", "create", $tag, $zipPath,
    "--repo", $repository,
    "--draft",
    "--title", $title,
    "--notes-file", $notesPath,
    "--target", "main"
)
$env:GH_TOKEN = $token
& $gh.Source @createArgs
if ($LASTEXITCODE -ne 0) {
    throw "Draft release creation or ZIP upload failed. Inspect GitHub before retrying."
}

$draft = Invoke-RestMethod -Uri $releaseApi -Headers $headers
if (-not $draft.draft -or $draft.prerelease -or $draft.tag_name -ne $tag) {
    throw "New Release is not the expected draft; update JSON was not changed."
}
$assets = @($draft.assets | Where-Object { $_.name -eq $assetName })
if ($assets.Count -ne 1 -or $draft.assets.Count -ne 1) {
    throw "Draft Release does not contain exactly the expected ZIP; update JSON was not changed."
}
$localSize = (Get-Item -LiteralPath $zipPath).Length
if ($assets[0].size -ne $localSize) {
    throw "Uploaded draft ZIP size differs from candidate; update JSON was not changed."
}

$publishBody = @{ draft = $false } | ConvertTo-Json
$published = Invoke-RestMethod -Method Patch -Uri "https://api.github.com/repos/$repository/releases/$($draft.id)" -Headers $headers -ContentType "application/json" -Body $publishBody
if ($published.draft -or $published.prerelease) { throw "GitHub did not publish the draft release." }

$publicZipPath = Join-Path $env:RUNNER_TEMP $assetName
Invoke-WebRequest -Uri $assets[0].browser_download_url -OutFile $publicZipPath
$actualPublicHash = (Get-FileHash -LiteralPath $publicZipPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualPublicHash -ne $expectedZipSha256.ToLowerInvariant()) {
    throw "Public ZIP SHA-256 differs from candidate. Update JSON was not changed."
}
Write-Output "Public ZIP SHA-256 verified for $tag."

$env:GH_TOKEN = $token
& python scripts/release_pipeline.py update-metadata --app $app --version $version --public-zip $publicZipPath
if ($LASTEXITCODE -ne 0) {
    throw "Release is public, but update metadata was not confirmed. Inspect GitHub before retrying."
}
