# Giljabi News Bot

감사길잡이의 **감사 관련 언론 이슈**를 PC가 꺼져 있어도 무료로 갱신하기 위한 공개 스케줄러입니다.

이 저장소에는 감사길잡이 본체 코드나 비공개 데이터가 들어 있지 않습니다. GitHub Actions가 전용 SSH deploy key로 비공개 본체 `hsgamsa00-netizen/Giljabi`의 최신 `main`을 얕게 checkout하고, 본체에 포함된 검증된 수집기만 실행한 뒤 결과 파일을 다시 본체에 push합니다.

## 자동화

| 작업 | 주기 | 역할 |
|---|---:|---|
| `Hourly media update` | 매시간 :17 | Google 뉴스 400개 논리 질의, 검증·아카이브·본체 push |
| `Daily media recovery` | 매일 18:47 UTC | 최근 6주 누락 가능 구간을 체크포인트 방식으로 복구 |
| `Public bot keepalive` | 매주 월요일 | 공개 저장소의 60일 무활동 예약 중지를 방지 |
| `Validate public bot` | push/PR | 공개 워크플로의 보안·동시성·SHA 고정 계약 검사 |

시간당 작업은 원격 언론 수집시각이 50분 이내면 즉시 종료합니다. 국내 PC 폴백과 경합해 본체 `main`이 먼저 바뀌면 비교 후 push를 거부하고, 다음 예약 회차가 최신 상태에서 다시 수집하므로 공식 감사자료를 덮어쓰지 않습니다.

## 보안 경계

- 대상 저장소 한 곳에만 쓰는 SSH deploy key 사용
- deploy key는 clone과 최종 push 단계에만 일시 주입하고 수집·검증 단계에서는 제거
- GitHub 공식 Ed25519 host key 고정 및 엄격한 host 검증
- 모든 GitHub 제공 action을 40자리 commit SHA로 고정
- 배포 workflow는 `schedule`과 `workflow_dispatch`에서만 실행
- `private-target` Environment는 `main` branch에서만 사용
- PR 검증 workflow에는 deploy key를 참조하지 않음
- artifact·cache·디버그 SSH 로그를 만들지 않음
- 같은 본체 branch를 쓰는 시간당 작업과 스윕을 하나의 concurrency group으로 직렬화
- Google 뉴스 오류 시 95% 건강도 gate와 회로 차단이 기존 피드를 보존

초기 배포 시 키가 등록될 때까지 두 배포 workflow는 비활성 상태로 둡니다. 설정 절차는 [deploy key 안내](docs/DEPLOY_KEY_SETUP.md)를 따릅니다.

표준 GitHub-hosted runner는 공개 저장소에서 무료입니다. 자세한 기준은 [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)을 참고하십시오.
