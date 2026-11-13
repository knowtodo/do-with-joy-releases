# TIMESAVER Release 자동화

`.github/workflows/publish-timesaver.yml`은 main 브랜치에서 두 경로로 작동합니다.

- 정상 운영: 사용자가 Codex에 해당 버전 정식 배포를 명시적으로 승인한 뒤 `release-requests/timesaver/X.Y.Z.json` 새 파일이 main에 추가되면 push 이벤트로 자동 실행됩니다.
- 비상/복구: 기존 `workflow_dispatch` 수동 실행을 main에서 사용할 수 있습니다.
- PR 검증: YAML, 배포 gate와 요청 검증 테스트만 실행하며 Release 게시 job은 실행하지 않습니다.

일반 코드·문서·업데이트 JSON 변경이나 PR만으로는 게시 job이 실행되지 않습니다. push 방식은 배포 저장소 main에 들어온 변경 전체가 해당 버전 요청 JSON의 단일 신규 파일 추가일 때만 통과합니다. 요청 파일 수정·삭제·다중 파일 변경, 잘못된 버전/경로/해시, 현재 공개 버전 이하 요청은 차단됩니다.

## 최초 1회 권한 설정

### 1. 소스 Artifact 읽기 토큰

비공개 소스 저장소의 Artifact는 배포 저장소의 기본 `GITHUB_TOKEN`만으로 읽을 수 없으므로 읽기 전용 fine-grained PAT가 필요합니다.

1. GitHub 프로필 → **Settings** → **Developer settings** → **Personal access tokens** → **Fine-grained tokens**에서 토큰을 생성합니다.
2. Resource owner를 `knowtodo`로, Repository access를 `Only select repositories` → `timesaver`로 설정합니다.
3. Repository permissions에서 **Actions: Read-only**만 부여합니다. Metadata는 기본 읽기 권한으로 둡니다.
4. 토큰을 배포 저장소 `knowtodo/do-with-joy-releases`의 **Settings** → **Secrets and variables** → **Actions** → **New repository secret**에서 아래 이름으로 저장합니다.
   - Name: `TIMESAVER_SOURCE_ARTIFACT_READ_TOKEN`
   - Secret: 방금 만든 PAT 값
5. 토큰을 코드, 배포 요청 파일, 채팅 또는 로그에 넣지 않습니다. 만료/폐기되면 Secret만 교체합니다.

### 2. 배포 저장소의 GITHUB_TOKEN

배포 저장소 **Settings** → **Actions** → **General** → **Workflow permissions**에서 workflow에 `contents: write`가 허용되어야 합니다. publisher는 workflow에 그 최소 권한을 명시해 Release/asset과 검증 완료 후 업데이트 JSON만 변경합니다.

조직 정책이나 main 보호 규칙이 이 권한을 막으면 그 설정을 우회하거나 낮추지 말고 저장소 소유자가 검토해야 합니다.

## v1.1.6 배포 요청

요청 파일은 비밀정보를 포함하지 않는 새 JSON 파일입니다. 파일명은 버전과 정확히 일치해야 하며 예: `release-requests/timesaver/1.1.6.json`. 실제 후보 Artifact의 검증된 수치만 기록합니다.

```json
{
  "application": "timesaver",
  "version": "1.1.6",
  "source_repository": "knowtodo/timesaver",
  "source_run_id": "42489954978",
  "artifact_name": "TIMESAVER-1.1.6-RELEASE-CANDIDATE",
  "expected_source_commit": "1fab21d2af15b3b5dd8f247ff0376cf59f3fef96",
  "expected_zip_sha256": "C78E1A8FC2546A40327C701E8E6DDB461DC679A322AD900EAA741405B1808B2D",
  "expected_exe_sha256": "FC0E418D431D753A3C920199F61868B63B74003CFBAA44A5A9794D905820F333",
  "expected_updater_sha256": "CEB28ED1E8DFDE50FD1BD8511DE9D05900D4F3C98339AA2D55CEFBC6F49CD498",
  "release_notes": "[변경사항]\n- 구직촉진수당 상담일지에 가족수당 확인 기능 추가\n- 가족수당 대상자 및 금액 선택 기능 추가\n- 가족수당 금액 단일 선택 오류 수정\n- 취업예정 기업명 및 취업예정일 입력 개선\n- IAP 1유형에 소득·가족수당·취업예정 확인 기능 추가\n- 입력 내용에 따른 상담일지 자동 문구 생성 개선"
}
```

새 요청 파일 또는 검증된 재시도 파일의 push 한 번이 기존 publisher의 검증·공개 절차를 시작합니다. 게시 전에 source Actions run의 성공 상태·검증 workflow 경로·커밋, 후보 Artifact의 정확한 파일 목록, 버전 메모, ZIP 구성, ZIP/EXE/Updater/복구 도구의 SHA-256·크기 및 SHA256SUMS 전체 항목을 확인합니다. 소스 Secret 부재나 검증 실패 시 Release 변경 없이 중단합니다. 공개 Release ZIP 재다운로드와 해시 검증이 성공한 뒤에만 `TIMESAVER_update.json`을 갱신합니다.

### 선택적 별도 복구 도구 자산

구형 후보 요청의 스키마와 동작은 그대로 유지됩니다. 별도 복구 실행 파일을 포함하는 신규 요청에만 다음 두 문자열 필드를 함께 추가할 수 있습니다.

- `expected_repair_sha256`: 후보 Artifact에 실제 포함된 `TIMESAVER_Update_Repair.exe`의 승인된 SHA-256
- `expected_repair_bytes`: 같은 파일의 승인된 양의 바이트 크기(10진 문자열)

두 필드는 모두 없거나 모두 유효해야 합니다. 배포 요청 검증은 후보 파일 자체의 크기와 SHA-256을 요청값과 대조하고 SHA256SUMS.txt가 후보 파일 전체의 실제 해시와 정확히 일치하는지 확인합니다. Release 자산은 일반 ZIP과 분리 게시되며, ZIP 내부 구성은 기존처럼 `TIMESAVER.exe`, `Updater.exe` 두 파일만 허용합니다. 기존 Draft의 자산 목록과 파일 크기를 검사해 누락된 자산만 이어 올리고, 이미 있는 자산을 덮어쓰지 않습니다. 두 자산의 API 다운로드와 공개 다운로드가 모두 기대 크기·SHA-256을 통과하기 전에는 업데이트 JSON을 변경하지 않습니다.

### 기존 수동 실행

비상시 Actions → **Publish TIMESAVER release** → **Run workflow** → branch `main`으로 실행할 수 있으며, 기존 입력/검증 단계를 그대로 사용합니다.

## 재실행과 중복 방지

- 동일한 배포 요청 파일을 수정/삭제하는 push와 두 개 이상의 변경 파일은 거부합니다.
- 요청 버전이 현재 JSON보다 낮으면 거부합니다. 이미 공개된 동일 버전은 새 실행에서 거부되며, 동일한 완료 run의 재시도만 JSON 값이 완전히 동일할 때 무변경 통과합니다.
- 같은 tag/Release가 있더라도 notes, asset 및 SHA-256이 기대값과 다르면 덮어쓰지 않고 중단합니다. 일치하는 중간 상태는 기존 workflow의 복구 로직이 안전하게 재사용합니다.
- push 요청 실패 뒤 같은 run을 재실행하면 그 run이 참조한 과거 workflow가 다시 사용될 수 있습니다. 복구할 때는 기존 배포 요청을 수정하지 말고 \`release-requests/timesaver/retries/X.Y.Z-N.json\`을 단일 신규 파일로 추가합니다.
- 재시도 JSON은 원본 \`release-requests/timesaver/X.Y.Z.json\`과 모든 필드 및 해시가 정확히 같아야 하며 인증정보를 포함할 수 없습니다. N은 해당 버전의 다음 순번이어야 합니다.
- 재시도는 원본 배포 요청이 처음 추가되기 직전의 공개 버전이 아직 현재와 같을 때만 허용됩니다. 이미 목표 버전이 공개됐거나 원본 요청과 값이 다르면 중단합니다.
- 재시도 요청의 push는 기존 \`release-requests/timesaver/**\` 경로 필터와 현재 main의 workflow를 사용하므로 새 workflow나 수동 Actions 실행이 필요하지 않습니다. 이미 update JSON까지 게시된 버전을 다시 배포하지 않습니다.
- 공개 JSON 내용, blob SHA와 이전 commit 기록은 기존 publisher의 Actions summary/Git history에 유지됩니다.

## 향후 패치

1. Codex가 소스 수정·테스트·후보 빌드를 완료하고 해당 버전의 검증된 run ID, artifact, commit, ZIP/EXE/Updater 해시와 notes를 확보합니다.
2. 사용자가 정식 배포를 승인한 뒤, Codex가 새 버전 파일 `release-requests/timesaver/X.Y.Z.json` 하나만 main에 추가합니다.
3. GitHub Actions가 Artifact를 다시 검증하고 Release, ZIP 공개 검증, update JSON 갱신을 자동 수행합니다.
4. Codex는 Actions 결과, 공개 Release, raw JSON과 공개 ZIP 해시를 확인하고 최종 상태를 보고합니다.

새 배포 시스템이나 매 패치마다 별도의 workflow를 추가하지 않습니다. 자동 실행 여부는 GitHub push event의 실제 Actions run으로 확인하며, GitHub가 해당 인증 주체의 push 이벤트를 전달하지 않으면 배포 완료로 간주하지 않습니다.
