# Giljabi News Bot

감사길잡이의 **감사 관련 언론 이슈**를 PC가 꺼져 있어도 무료로 갱신하기 위한 공개 스케줄러입니다.

이 저장소에는 감사길잡이 본체 코드나 비공개 데이터가 들어 있지 않습니다. GitHub Actions가 읽기 전용 SSH deploy key로 비공개 본체 `hsgamsa00-netizen/Giljabi`의 최신 `main` 중 필요한 파일만 부분 checkout하고, 본체에 포함된 검증된 수집기만 실행합니다. 검증된 공개 뉴스 데이터 묶음만 별도 새 러너로 넘긴 뒤 쓰기 전용 key로 본체에 push합니다.

## 자동화

| 작업 | 주기 | 역할 |
|---|---:|---|
| `Hourly media update` | 매시간 :17 | Google 뉴스 400개 논리 질의, 검증·아카이브·본체 push |
| `Six-hour media recovery` | 6시간마다 :47 UTC | 최근 6주 누락 가능 구간을 회차당 최대 420초·700질의로 체크포인트 복구 |
| `Public bot keepalive` | 매주 월요일 | 공개 저장소의 60일 무활동 예약 중지를 방지 |
| `Validate public bot` | push/PR | 공개 워크플로의 보안·동시성·SHA 고정 계약 검사 |

시간당 작업은 원격 언론 수집시각이 50분 이내면 즉시 종료합니다. 6시간 복구 작업의 build job은 16분 제한으로, 7분 수집 상한 뒤 clone/setup/검증과 bundle 준비에 9분의 여유를 둡니다. 국내 PC 폴백과 경합해 본체 `main`이 먼저 바뀌면 비교 후 push를 거부하고, 다음 예약 회차가 최신 상태에서 다시 수집하므로 공식 감사자료를 덮어쓰지 않습니다.

복구 주기와 수집 상한의 기준은 `daily-media-recovery.yml` build job의 `RECOVERY_*` 환경값입니다. 계약 테스트는 YAML 계층에서 이 값을 읽고 cron의 실제 실행 시각, shell 명령 인자, job timeout 여유가 같은 계약을 구현하는지 검증합니다.

## 보안 경계

- 대상 저장소 한 곳에만 연결한 읽기용·쓰기용 SSH deploy key를 분리
- 수집 job과 쓰기 job을 새 러너로 격리하고, 1일 보존 공개 데이터 묶음만 전달
- deploy key는 clone과 최종 push 단계에만 일시 주입하며 본체 코드를 실행할 때는 제거
- GitHub 공식 Ed25519 host key 고정 및 엄격한 host 검증
- 모든 GitHub 제공 action을 40자리 commit SHA로 고정
- 배포 workflow는 `schedule`과 `workflow_dispatch`에서만 실행
- `private-target` Environment는 `main` branch에서만 사용
- PR 검증 workflow에는 deploy key를 참조하지 않음
- 비공개 checkout·key·디버그 SSH 로그를 artifact에 넣지 않음
- artifact에는 허용된 공개 feed/archive 파일만 포함하고 1일 뒤 삭제하며 cache는 사용하지 않음
- 같은 본체 branch를 쓰는 시간당 작업과 스윕을 하나의 concurrency group으로 직렬화
- Google 뉴스 오류 시 95% 건강도 gate와 회로 차단이 기존 피드를 보존

2026-08-13에 읽기·쓰기 deploy key 등록과 hourly·six-hour recovery 수동 라이브 게이트를
완료하고 두 배포 workflow를 활성화했습니다. 비공개 본체의 기존 언론 예약은
dispatch-only 비상 경로로 전환했으며, 국내 PC의 50분 언론 폴백은 유지합니다.
키 교체·복구 절차는 [deploy key 안내](docs/DEPLOY_KEY_SETUP.md)를 따릅니다.

표준 GitHub-hosted runner는 공개 저장소에서 무료입니다. 자세한 기준은 [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)을 참고하십시오.
