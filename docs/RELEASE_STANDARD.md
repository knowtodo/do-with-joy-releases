# Shared Windows application release process

This repository coordinates public releases and update metadata for TIMESAVER and INFOFINDER. The source repositories retain separate Python runtimes and candidate build workflows. This repository does not build either application.

## Safety boundary

- Pull requests, pushes, and source candidate builds never create or publish a GitHub Release.
- The only publisher is .github/workflows/publish-approved-release.yml. It is workflow_dispatch-only and runs from main.
- Candidate validation happens before the production approval gate. The release job runs only after a required reviewer approves the production-release Environment.
- The dispatch requires the exact confirmation string PUBLISH {app} {version}.
- Candidate provenance is accepted only from a successful workflow_dispatch build on the selected source repository's main branch and expected workflow.
- The candidate artifact and manifest are checked before any Contents-write release token is minted, then re-downloaded and rechecked after approval.
- Existing tags/releases are never overwritten. A duplicate version is a hard stop.
- The public Release ZIP is downloaded and SHA-256 checked before the corresponding update JSON is committed.
- The two update JSON files are updated independently. INFOFINDER release_notes retains cumulative versioned sections and notes contains the current version only.

## One-time GitHub setup

1. Create two GitHub Apps so source-artifact read access cannot publish releases.
2. Source reader App: install only on knowtodo/timesaver and knowtodo/infofinder; grant Actions: read-only permission. In this repository, add repository variable SOURCE_READER_APP_ID and repository secret SOURCE_READER_APP_PRIVATE_KEY. This App cannot write Releases or update JSON.
3. Release publisher App: install only on knowtodo/do-with-joy-releases; grant Contents: read/write permission. This App cannot access source Actions artifacts.
4. In do-with-joy-releases Settings, create an Environment named production-release. Require a reviewer and restrict deployment branches to main. Without a required-reviewer rule, GitHub will not pause for environment approval.
5. Add environment variable RELEASE_PUBLISHER_APP_ID and environment secret RELEASE_PUBLISHER_APP_PRIVATE_KEY. The publisher private key is available only to the post-approval job. Never put keys or tokens in source files, workflow inputs, or logs.
6. Ensure Actions are enabled in all three repositories and the source candidate workflows can upload artifacts. Candidate artifacts are retained for 30 days.
7. Keep sufficient Actions artifact storage in both source repositories. If artifact upload reports a quota failure, do not substitute a local or unverified ZIP.

## Standard release steps

1. On the selected source repository's main branch, manually run its Windows candidate workflow. Wait for tests, Tk/Tcl checks, PyInstaller, ZIP verification, and artifact upload to succeed.
2. Record the successful workflow run ID and version. The artifact expires after its configured retention period.
3. In this repository, run Publish approved application release from main. Select the app, enter the exact version and source run ID, type PUBLISH {app} {version}, and provide release notes.
4. The validation job checks the source run and candidate ZIP before requesting environment approval. Review the source run and requested version, then approve the production-release deployment.
5. The post-approval job re-downloads and revalidates the same source run and ZIP. It checks exact ZIP entries, CRC, EXE and common Updater hashes, ZIP hash, test summary, source commit and unused release tag.
6. The workflow creates a draft Release, confirms the uploaded asset, publishes it, downloads the public asset, and checks the downloaded SHA-256.
7. Only after the public-download check does it update the selected app's JSON on main and re-read the public raw JSON URL.

## Credential and permission model

- The source-reader App is installed only on the two source repositories and requests Actions: read for the selected source repository.
- The release-publisher App is installed only on this public repository and requests Contents: write only in the approved job.
- The source-reader key is a repository secret because validation runs before environment approval; its App has no write permission. The release-publisher key is an Environment secret and is unavailable until the required reviewer approves.
- The workflow's default GITHUB_TOKEN remains contents: read and is never used to publish a Release or update JSON.
- A successful build is not release approval. Manual dispatch confirmation and environment review are distinct gates.

## Failure handling

- Invalid request, source run, artifact, ZIP, test summary, hash, or duplicate tag stops before a Release is created.
- If draft creation or upload fails, inspect GitHub for a partial draft/tag before retrying. The workflow never replaces or deletes it.
- If public download verification fails after publication, the Release may be public while the update JSON remains unchanged. Do not retry blindly; inspect the Release asset and checksum.
- If the metadata commit succeeds but public raw verification times out, the JSON may already be updated. Inspect the commit and public URL before retrying.
- Publishing update metadata does not roll back versions already installed by users. If an invalid update is exposed, stop further updates and publish a corrective higher version; do not assume clients self-rollback.

## TIMESAVER 1.1.4 manual completion fallback

The local automated release credential is not currently available to Codex. An authorized maintainer can use the GitHub website:

1. Open knowtodo/do-with-joy-releases, select Releases, then Draft a new release.
2. Create tag timesaver-v1.1.4 targeting main and use title TIMESAVER 1.1.4.
3. Upload the previously verified TIMESAVER-1.1.4.zip candidate. Expected size: 121180377 bytes. Expected SHA-256: B18D0672CCA95E48856E23737882300C9D6BBFF9A5F44A08D9DBFDE64F83F1B2.
4. Confirm the selected local file has that hash. Publish only after the authorized user approves it.
5. Download the published ZIP from its public asset URL and verify its SHA-256.
6. Only after that check, update TIMESAVER_update.json to version 1.1.4, its exact public asset URL, and the verified ZIP hash. Do not change INFOFINDER_update.json. Re-read the public raw JSON and verify it.
7. If a tag or Release already exists, stop and inspect it; never overwrite. Do not publish an unverified asset or activate an update URL that returns 404.

The known 1.1.4 ZIP currently exists locally with the recorded size/hash, but this PR does not create a Release, change either JSON, or repeat the Windows GUI checklist.
