# 대상 저장소 deploy key 연결

현재 활성 GitHub CLI 계정은 본체 저장소에 push 권한은 있지만 deploy key 관리용 admin 권한은 없습니다. 따라서 본체 소유자 권한으로 다음 1회 설정이 필요합니다.

1. 이 봇 전용 Ed25519 키 쌍을 생성합니다.
2. **공개키**를 `hsgamsa00-netizen/Giljabi`의 `Settings → Deploy keys → Add deploy key`에 등록합니다.
3. 제목은 `Giljabi-news-bot`, `Allow write access`를 선택합니다.
4. 공개 봇 저장소에 `private-target` Environment를 만들고 배포 branch를 `main`으로 제한합니다. 무인 예약 실행이므로 필수 승인자는 두지 않습니다.
5. **개인키**는 `private-target` Environment의 Actions secret `GILJABI_DEPLOY_KEY`에만 저장합니다.
6. 로컬 개인키 파일은 secret 저장 성공 직후 삭제합니다.
7. 비활성화해 둔 `Hourly media update`를 활성화하고 `force=true`로 수동 실행하여 본체 push와 Pages 배포를 확인합니다.
8. `Daily media recovery`도 수동으로 1회 검증한 뒤 활성화합니다.

개인 계정 SSH 키나 `repo` 전체 권한 PAT를 대신 사용하지 마십시오. deploy key는 대상 저장소 하나에만 권한이 묶여 사고 범위를 제한합니다.
