# Security policy

## Trust boundary

배포 권한이 있는 workflow는 기본 브랜치의 `schedule` 및 명시적인 `workflow_dispatch`로만 실행합니다. `pull_request_target`은 사용하지 않으며, 공개 PR 검증은 별도 read-only workflow에서 수행합니다.

`GILJABI_READ_KEY`와 `GILJABI_DEPLOY_KEY`는 `hsgamsa00-netizen/Giljabi` 한 저장소에만 연결한 별도 deploy key여야 합니다. 수집 job에는 읽기 key만, 새 publish 러너에는 쓰기 key만 제공합니다. 개인 계정 SSH 키나 범용 PAT를 저장하지 마십시오.

## Secret handling

- 개인키를 파일, 로그, artifact, cache, commit에 남기지 않습니다.
- `StrictHostKeyChecking=no`, credential 포함 URL, `ssh -v`를 사용하지 않습니다.
- 키가 노출되었거나 의심되면 대상 저장소의 deploy key를 즉시 폐기하고 공개 봇 secret을 삭제한 뒤 새 키로 교체합니다.
- GitHub Actions log에 민감정보가 출력되었다면 키 폐기 후 해당 log도 삭제합니다.

## Reporting

보안 문제는 공개 issue 대신 저장소 소유자에게 비공개로 전달하십시오.
