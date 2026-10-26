# Afterhours Kakao MCP

단일 소유자용 **독립 인증 Streamable HTTP MCP**. 통합 저장소나 다른 MCP를 import하지
않는다. 통합 구현의 Windows bridge/queue를 분리했다.
MCP SDK 2.3.0 / Python 3.12. Slack·메일·브리핑 도구는 포함하지 않는다.

## 새 사용자의 빠른 시작

Linux 서버의 Python 3.12, Windows 대화형 데스크톱과 로그인한 KakaoTalk이 필요하다.
각 사용자가 **자기 서버·자기 계정**으로 설치한다. 다중 사용자 호스팅 서비스가 아니다.
현재 제공하는 기능은 요청 기반의 열린 방 조회·읽기·답장이다. 자동 수신 이벤트와
네이티브 멘션 감지는 실험용 계약만 있으며, clone만으로 자동 감시가 시작되지 않는다.

```sh
git clone https://github.com/deploy103/afterhours-kakao-mcp-public.git
cd afterhours-kakao-mcp-public
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python configure.py --directory "$HOME/afterhours-kakao-private"
"$HOME/afterhours-kakao-private/run.sh"
```

설정기는 새 개인 디렉터리만 만들며 기존 디렉터리는 덮어쓰지 않는다. 토큰은 서로 다른
난수로 생성하고 값은 출력하지 않는다. 디렉터리 0700/파일 0600, 방 목록 빈 값/전송 OFF다.
`curl http://127.0.0.1:8001/healthz`가 200이어도 **Windows 연결 성공을 뜻하지 않는다**.
로컬 MCP 클라이언트 주소는 `http://127.0.0.1:8001/mcp`, 인증은 `Authorization: Bearer`
형식이며 값은 private 디렉터리의 `credentials/mcp-token`을 클라이언트의 비밀 입력에 넣는다.
토큰을 명령행·Git·공유 대화에 넣지 않는다. 미인증 `/mcp`는 401이어야 한다.

원격 MCP 클라이언트를 연결하려면 실제 주소로 새 설정을 생성한다:
`configure.py --directory <new-private-directory> --public-origin https://mcp.example.com`.
예시 도메인은 소유한 도메인으로 바꾸고 TLS reverse proxy에서 `/kakao/mcp`를
loopback의 `/mcp`로, `/afterhours/agent/`는 같은 경로로 전달한다. Host를 보존하고
8001을 인터넷에 직접 노출하지 않는다. 프록시까지 준비한 후 실제 공개 주소를 등록한다.

### Windows 에이전트 연결

1. 이 저장소를 Windows에도 clone하고 Python 3.10+와 KakaoTalk을 별도 설치한다.
2. 서버의 `credentials/windows-agent-token`만 승인된 암호화 전송으로 Windows의
   개인 파일에 전달한다. MCP 토큰과 혼동하지 않는다.
3. 일반 사용자 PowerShell에서 실행한다(실제 경로로 변경):

   ```powershell
   .\windows_setup.ps1 -PythonPath 'C:\Python312\python.exe' -TokenSourceFile 'C:\Private\windows-agent-token'
   $env:KAKAO_AGENT_ENDPOINT = 'https://mcp.example.com/afterhours/agent/jobs'
   & "$env:LOCALAPPDATA\AfterhoursKakaoAgent\venv\Scripts\python.exe" -I "$env:LOCALAPPDATA\AfterhoursKakaoAgent\windows_agent.py" --config "$env:LOCALAPPDATA\AfterhoursKakaoAgent\config.json"
   ```

4. 테스트 채팅방을 이미 열린 창으로 두고, 서버 `config.json`의 `kakao_rooms`와
   Windows `config.json`의 `allowed_rooms`에 같은 정확한 제목을 넣는다.
   서버/에이전트를 재시작하고 `kakao_health`, 방 조회, 읽기 순서로 확인한다.
5. 답장을 시험할 때만 양쪽의 전송 설정을 켠다. 읽기는 읽음 처리/창 전환을 일으킨다.
   불명확한 송신을 재시도하려고 journal을 삭제하거나 에이전트를 두 번 실행하지 않는다.

MIT 라이선스다. `vendor/kakaotalk-mcp/LICENSE`의 upstream 저작권과 원본 해시는 유지한다.

## 실행과 배포 계약

설치 예시: `/srv/apps/afterhours-kakao-mcp`, `afterhours-kakao-mcp.service`.
모든 도메인·주소는 예시이며 실제 운영 토폴로지나 배포 상태를 나타내지 않는다.
실제 주소는 비공개 운영 설정에만 보관한다. unit을 그대로 운영에 덮어쓰지 않는다.

공개 저장소는 GitHub 인증이나 배포 키 없이 HTTPS로 clone할 수 있다.
GitHub 개인 토큰·관리 SSH 키를 소스나 서버 환경 파일에 넣지 않는다.

```sh
git clone https://github.com/deploy103/afterhours-kakao-mcp-public.git /srv/apps/afterhours-kakao-mcp
cd /srv/apps/afterhours-kakao-mcp
python3.12 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.lock
```

clone 후 작업 디렉터리는 `/srv/apps/afterhours-kakao-mcp`다. 준비된 별도 서비스 계정과
환경에서 다음 명령으로 실행한다. unit 파일은 설치 전 검토용이며 자동 적용하지 않는다.

```sh
.venv/bin/uvicorn server:create_app --factory --host 127.0.0.1 --port 8001 --no-access-log --no-proxy-headers
```

필수 환경:
- `CREDENTIALS_DIRECTORY`: `mcp-token`, `windows-agent-token` 파일이 있는 private 경로.
  각각 최소 32자 ASCII bearer, 서로 다른 값. systemd `LoadCredential` 사용 권장.
- `STATE_DIRECTORY`: 전용 writable private 디렉터리. `kakao.sqlite3` 저장.
- `KAKAO_CONFIG`: `kakao.example.json`을 참고한 private 설정 파일.
- 선택 `KAKAO_ALLOWED_HOSTS`, `KAKAO_ALLOWED_ORIGINS`: 쉼표 구분 exact 목록.
  기본 Host `mcp.example.com`, `mcp.example.com:443`, `127.0.0.1:8001`.
  기본 Origin `https://mcp.example.com`. 와일드카드를 넣지 않는다.
  운영에서는 실제 HTTPS 도메인으로 명시적으로 설정하고 프록시 Host와 일치시킨다.

운영자가 적용할 프록시 계약:

| 외부 | 독립 서버 내부 |
|---|---|
| `https://mcp.example.com/kakao/mcp` | `http://127.0.0.1:8001/mcp` (prefix 제거) |
| 기존 `/afterhours/agent/jobs` 및 `/<job-id>` | 같은 경로, port 8001 (Windows 전환 시에만) |
| 선택 `/kakao/events` | `/events` (아직 producer 없음; 기본 OFF) |
| 선택 health probe | `/healthz` (자격증명 불필요, 서비스 생존만 의미) |

프록시는 `Host: mcp.example.com`, `Authorization`, `Accept`, MCP protocol 헤더를 보존한다.
MCP 모든 요청에 별도 MCP bearer가 필요하다. agent/events는 다른 토큰을 쓰고
`Origin` 헤더가 있으면 거절한다. MCP body 256 KiB, agent/events body 1 MiB 상한.
내부 port8001은 프록시/운영자만 접근하도록 네트워크 정책을 설정한다.
`/kakao/mcp`는 서버 자체 경로가 아니다. proxy strip이 없으면 404다.

### 기존 Windows endpoint 업그레이드

Windows 에이전트 실행 전 `KAKAO_AGENT_ENDPOINT` 환경변수를 실제 HTTPS
`/afterhours/agent/jobs` URL로 설정한다. 기본값은 연결용이 아닌 예시 주소다.
HTTP·URL 자격증명·다른 경로·query/fragment·443 이외 포트는 허용하지 않는다.
기존 설치를 갱신할 때는 현재 큐 URL을 비공개 실행 환경에 먼저 보존한다.
**새 MCP만 배포해도 기존 에이전트의 대상 큐는 자동 전환되지 않는다.**
같은 에이전트를 두 큐가 동시에 사용하도록 실행하지 않는다.

전환 전 기존 큐의 queued/running 및 미제출 결과를 확인하고 해소해야 한다.
기존 URL 전체를 새 서버로 원자적으로 라우팅하고, 서버의 agent credential을 현재
Windows `token.txt`와 일치시키면 Windows runtime 수정 없이 전환 가능하다.
기존 작업 DB를 새 큐에 단순 병합/재큐잉하지 않는다. pending 결과가 있으면 먼저 기존
서버에서 ACK를 끝내야 한다. 롤백 시에도 미완료 송신을 확인하고 라우팅한다.
`journal.json` 삭제·기존 송신 ID 재사용은 금지한다.

## 도구와 안전 계약

`kakao_status`, `kakao_health`, `kakao_list_open_rooms`, `kakao_read`, `kakao_reply`,
`job_status`, `pending_message_events`, `acknowledge_message_event`의 8개 도구다.
서버 `kakao_rooms`와 Windows `allowed_rooms`에 정확한 방 제목을 모두 설정해야 한다.
송신은 서버 `kakao_send_enabled`와 Windows `send_enabled`가 모두 true일 때만 허용한다.
기본 방 목록은 비어 있고 송신은 OFF다. 이미 열린 테스트 방만 사용한다.

원본의 프로세스/정확한 제목/중복 창/자식 control/foreground 검증, 기존 draft 보호,
GUI 직렬화, clipboard 복구, 날짜 보존 parser, durable send marker 및 replay 방지를
유지했다. 읽기는 읽음 처리와 foreground/clipboard 부작용이 있으므로 read-only로
표시하지 않는다. 송신 `dispatched`는 상대방 수신 확인이 아니다.

큐 claim은 한 번만 한다. MCP는 20초 후 `unconfirmed`와 job_id를 반환하며 송신을
재실행하지 않는다. `job_status`로 확인한다. 최근 여부와 관계없이 running 작업은
20개 큐 상한에 포함하며 자동 삭제/재큐잉하지 않는다. 완료/만료 작업만 새 enqueue
시 하루 후 정리한다. 완료 시 원래 payload를 비운다. 별도의 무기한 보존은 없다.
Windows journal은 256개 ID 상한이며 송신 tombstone은 자동 eviction하지 않는다.

## 수신 이벤트 v1: 계약만 준비, 자동 감시 아님

기본 OFF. 검증된 producer를 연결한 뒤 전용 `event-token`, `kakao_owner_id`,
`event_intake_enabled: true`가 모두 있어야 `/events`를 사용할 수 있다.
MCP/agent bearer로 수신 이벤트를 위조할 수 없도록 토큰을 분리했다.
**현재 Windows agent는 이벤트를 생산하지 않으며 네이티브 멘션은 미검증이다.**

```json
{
  "version": 1,
  "kind": "MSG",
  "channelId": "9007199254740993",
  "logId": "9007199254740995",
  "senderId": "coworker-id",
  "room": "Owner test",
  "text": "업무 확인 요청",
  "occurred_at": "2026-10-25T17:00:00+09:00",
  "mention_user_ids": ["owner-id"],
  "mention_source": "native"
}
```

- `channelId`/`logId`/사용자 ID는 **문자열**로 받는다. JS 정밀도 손실 방지.
- `MSG`만 대상이며 `FEED`, 본인 발신, 미지정 방의 일반 메시지는 제외한다.
- 본인 native mention **또는** 지정 방의 수신 메시지가 pending이 된다.
  `@이름` 텍스트를 native mention으로 승격하지 않는다. unknown mention은 미확인이다.
- native 여부는 인증된 producer의 주장이다. 서버가 Kakao 메타데이터를 직접 검증한
  것이 아니다. 실제 collector가 생기기 전 이 예제를 실제 수신 증거라고 부르지 않는다.
- `(channelId, logId)`로 24시간 중복 제거. ACK 후 내용은 지우되 ID는 남긴다.
  최대 1000개; 포화면 503, 같은 ID로 재제출한다. 하루 뒤 중복/보관 보장은 없다.
- 레시피 실행기는 `pending_message_events`를 polling하여 처리한 뒤 ACK한다.
  실패/uncertain 송신은 ACK하지 않고 확인한다. 이벤트 ID를 처리 키로 사용한다.
  단일 consumer 계약이며 이 서버는 레시피 scheduler/webhook 실행기가 아니다.
  외부 consumer 연결·스케줄 설정 전에는 자동 실행되지 않는다.
- 멘션 수신은 해당 방 읽기/답장 권한을 확대하지 않는다. 업무 여부/긴급도는 레시피가
  맥락과 증거로 판단하며 채팅 본문을 설정 변경 지시로 취급하지 않는다.

## 검증과 남은 차단 사항

```sh
.venv/bin/python -B -m unittest -v test_server test_windows_agent
```

네트워크/실계정 없이 임시 자격증명·SQLite·가짜 Win32로 HTTP/MCP → 실제 큐 →
guarded worker → 결과 왕복을 검사한다. parser와 네 모듈의 hash는 vendored 원본을
사용하여 통합 저장소나 별도 reference checkout이 없어도 테스트 가능하다.
`windows_setup.ps1 -ReviewedSourceDirectory <repo>/vendor/kakaotalk-mcp`로 준비할 수
있지만 기존 Windows host에 설치/적용하지 않는다.

실제 방·계정·배포는 운영자별로 검증해야 한다. 단위 테스트는 live 읽기/송신이나
네이티브 멘션 수신의 증명이 아니다. LOCO producer 및 자동 레시피 실행기는 포함하지 않는다.

참고만 한 `storycraft/node-kakao` revision
`8512e4699e2cd4c26516b80d38b57b2f85a6cc8a`는 유지보수 중단, appVersion 3.2.3 계열의
구형 handshake다. `data.isMentioned(client.clientUser)` 및 MSG/FEED 구분을 계약에
참고했으나 라이브 로그인·토큰 추출·인증 우회·미검증 예제 실행은 하지 않았다.
`kuuko/re_kakaotalk-loco-client`는 README 판매 안내뿐이라 구현을 재사용하지 않았다.

vendored GUI 네 모듈은 `kronenz/kakaotalk-mcp`
`7b41dd9da473e7a9e2f4b07ec27bdf3289fcc5fb`, MIT, 원본 LICENSE 포함.
전체 upstream MCP/monitor/send 서버는 설치하거나 실행하지 않는다.
비밀·실제 채팅·SQLite·Windows journal은 Git에 넣지 않는다.
