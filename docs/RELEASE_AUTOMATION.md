# TIMESAVER Release 자동화

`.github/workflows/publish-timesaver.yml`은 `workflow_dispatch`로만 실행됩니다. 소스 Artifact를 비공개 `knowtodo/timesaver`에서 읽고, 검증된 ZIP만 `knowtodo/do-with-joy-releases`의 Release로 게시한 뒤 공개 ZIP 해시 검증에 성공했을 때 `TIMESAVER_update.json`을 마지막으로 갱신합니다. PR이나 일반 push만으로는 배포하지 않습니다.

## 최초 1회 설정

### 1. 소스 Artifact 읽기 토큰

비공개 소스 저장소의 Artifact는 배포 저장소의 기본 `GITHUB_TOKEN`만으로 읽을 수 없으므로 읽기 전용 fine-grained PAT가 필요합니다.

1. GitHub 프로필 → **Settings** → **Developer settings** → **Personal access tokens** → **Fine-grained tokens** → **Generate new token**.
2. Resource owner를 `knowtodo`로, Repository access를 `Only select repositories` → `timesaver`로 설정합니다.
3. Repository permissions에서 **Actions: Read-only**를 선택합니다. Metadata는 기본 읽기 권한으로 둡니다. 다른 권한은 부여하지 않습니다.
4. 토큰을 복사한 뒤 배포 저장소 `knowtodo/do-with-joy-releases`의 **Settings** → **Secrets and variables** → **Actions** → **New repository secret**에서 다음 이름으로 저장합니다.
   - Name: `TIMESAVER_SOURCE_ARTIFACT_READ_TOKEN`
   - Secret: 방금 만든 PAT 값
5. 토큰 값을 코드, workflow 입력, 채팅 또는 로그에 넣지 않습니다. 만료된 토큰은 같은 절차로 교체합니다.

토큰 발급이나 Secret 저장 권한이 없으면 저장소 소유자가 직접 설정해야 합니다. Codex는 토큰 값을 받거나 출력하지 않습니다.

### 2. 배포 저장소의 GITHUB_TOKEN

배포 저장소 **Settings** → **Actions** → **General** → **Workflow permissions**에서 Actions workflow가 `contents: write`를 사용할 수 있어야 합니다. workflow는 이 권한으로 Release/asset을 만들고, 검증이 끝난 뒤 update JSON을 커밋합니다.

main 브랜치 보호 규칙이 Actions의 직접 콘텐츠 업데이트를 차단하면 JSON 단계는 실패하고 기존 JSON을 보존합니다. 보호 규칙을 무단으로 낮추지 말고 저장소 소유자가 별도로 검토해야 합니다.

## v1.1.6 실행

이 PR을 검토 후 main에 병합하고 위 Secret 및 workflow 권한을 설정한 다음:

1. 저장소 **Actions** → **Publish TIMESAVER release** → **Run workflow**를 선택합니다.
2. Branch는 `main`을 선택합니다.
3. v1.1.6 기본 입력을 확인합니다.
   - version: `1.1.6`
   - source_repository: `knowtodo/timesaver`
   - source_run_id: `42489954978`
   - artifact_name: `TIMESAVER-1.1.6-RELEASE-CANDIDATE`
   - expected_source_commit: `1fab21d2af15b3b5dd8f247ff0376cf59f3fef96`
   - ZIP SHA-256: `C78E1A8FC2546A40327C701E8E6DDB461DC679A322AD900EAA741405B1808B2D`
   - EXE SHA-256: `FC0E418D431D753A3C920199F61868B63B74003CFBAA44A5A9794D905820F333`
   - Updater SHA-256: `CEB28ED1E8DFDE50FD1BD8511DE9D05900D4F3C98339AA2D55CEFBC6F49CD498`
4. Release notes는 workflow 기본값을 사용하거나 해당 버전에 맞는 문구를 입력합니다.

## 안전성 및 재실행

- 입력된 소스 run이 성공 완료됐는지, 저장소와 커밋이 artifact manifest와 일치하는지 확인합니다.
- 외부 Artifact 파일 목록, manifest, SHA256SUMS, ZIP CRC, 정확한 ZIP 항목 및 EXE 해시를 모두 검증합니다.
- 동일 태그/Release가 이미 있으면 재사용하며, 이름·설명·asset이 다르거나 해시가 틀리면 덮어쓰지 않고 중단합니다.
- Draft Release는 업로드 asset을 검증한 후 공개합니다. 공개 URL에서 ZIP을 다시 받아 크기·압축·SHA-256을 확인하기 전에는 update JSON을 변경하지 않습니다.
- JSON이 이미 해당 버전과 정확히 일치하면 재실행은 변경 없이 성공합니다. 더 높은 버전, 같은 버전의 다른 값, 동시 변경(파일 blob SHA 충돌)은 덮어쓰지 않습니다.
- JSON 변경 전에 기존 공개 JSON 내용, blob SHA 및 이전 커밋을 Actions 요약에 기록합니다. 기존 내용은 Git 커밋 이력에도 남습니다.
- 실패 후에는 같은 입력으로 재실행할 수 있습니다. 정상 Release와 asset은 재사용하고, 마지막 JSON 단계부터 안전하게 이어집니다.
- workflow 로그에는 버전, 커밋 해시, 파일 해시와 검증 결과만 기록하며 토큰은 출력하지 않습니다.

## 향후 버전

소스 main에서 테스트와 빌드를 완료하고 후보 Artifact를 업로드한 뒤, 이 workflow를 다시 실행합니다. version, source run ID, artifact name, source commit, 실제 ZIP/EXE/Updater 해시 및 release notes를 해당 후보 값으로 바꿉니다. 해시는 Artifact의 실제 파일에서 얻어야 하며 임의 값으로 입력하지 않습니다.

이 문서는 workflow의 동작과 권한 경계를 설명하며, 실제 게시 성공은 해당 Actions 실행이 끝나고 공개 URL 및 raw JSON 검증이 통과한 경우에만 인정합니다.
