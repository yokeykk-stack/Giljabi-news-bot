# 대상 저장소 deploy key 연결

현재 활성 GitHub CLI 계정은 본체 저장소에 push 권한은 있지만 deploy key 관리용 admin 권한은 없습니다. 따라서 본체 소유자 권한으로 다음 1회 설정이 필요합니다.

1. 이 봇 전용 Ed25519 키 쌍을 **두 개** 생성합니다. 다른 저장소나 개인 SSH key와 재사용하지 않습니다.
2. 첫 번째 **공개키**를 `hsgamsa00-netizen/Giljabi`의 `Settings → Deploy keys → Add deploy key`에 `Giljabi-news-bot read`로 등록하고 `Allow write access`는 선택하지 않습니다.
3. 두 번째 **공개키**를 같은 화면에 `Giljabi-news-bot write`로 등록하고 이 키에만 `Allow write access`를 선택합니다.
4. 공개 봇 저장소에 `private-target` Environment를 만들고 배포 branch를 `main`으로 제한합니다. 무인 예약 실행이므로 필수 승인자는 두지 않습니다.
5. 첫 번째 **개인키**는 `private-target` Environment secret `GILJABI_READ_KEY`, 두 번째 **개인키**는 `GILJABI_DEPLOY_KEY`에 저장합니다.
6. 두 secret 저장 성공 직후 로컬 개인키 파일을 삭제합니다.
7. 비활성화해 둔 `Hourly media update`를 활성화하고 `force=true`로 수동 실행하여 본체 push와 Pages 배포를 확인합니다.
8. `Daily media recovery`도 수동으로 1회 검증한 뒤 활성화합니다.

개인 계정 SSH 키나 `repo` 전체 권한 PAT를 대신 사용하지 마십시오. 쓰기 key는 수집 코드가 실행되는 러너에 주입되지 않으며, 공개 데이터 묶음을 검증·커밋하는 별도 러너의 clone과 최종 push 단계에만 잠시 사용됩니다.
