# Codex Reset KST

[@codex_resets](https://t.me/codex_resets)의 초기화 공지를 읽어 UTC 시간을 한국시간(KST, UTC+9)으로 바꾸고, 내 텔레그램 봇으로 개인 알림을 보냅니다. Python + Telethon + SQLite를 사용하며 NAS의 Docker Compose에서 실행합니다. GPU, LLM API, 외부 포트 개방, 웹 관리 서버는 필요하지 않습니다.

```text
Codex 초기화 알림
상태: 발표
한국시간: 2026-10-16 14:00 KST
원문시간: 2026-10-16 05:00 UTC
원문: https://t.me/codex_resets/메시지번호
```

## 준비할 것

- Docker Engine과 Docker Compose v2가 설치된 NAS. Synology는 Container Manager, QNAP은 Container Station을 먼저 설치합니다. 아래 명령은 NAS의 SSH 터미널에서 실행합니다.
- `git`, 텔레그램 사용자 계정과 앱, BotFather에서 만들 알림 전용 봇.
- NAS에서 Telegram으로 나가는 HTTPS(443) 및 Telethon 연결이 허용되어야 합니다. 공유기에 포트를 열 필요는 없습니다.
- 로그인할 **사용자 계정으로** [원본 채널](https://t.me/codex_resets)에 가입하세요. 알림 봇을 원본 채널에 추가할 필요는 없습니다.

NAS에서 먼저 확인합니다.

```bash
docker --version
docker compose version
git --version
```

명령이 없거나 Docker 권한 오류가 나오면 NAS 패키지/관리자 설정에서 Docker, Compose v2, Git 및 SSH 계정의 Docker 접근 권한을 설정하세요. 아래의 `docker compose` 명령은 모두 같은 프로젝트 디렉터리에서 실행합니다.

## 1. Telegram API ID / Hash 발급

1. 브라우저에서 [my.telegram.org](https://my.telegram.org)에 접속합니다.
2. 사용자 계정의 전화번호를 국가번호와 함께 입력합니다. 한국 번호 예: `+821012345678`.
3. 텔레그램 앱으로 받은 로그인 코드를 입력합니다.
4. **API development tools**에서 애플리케이션을 만듭니다. App title과 Short name을 입력하고, 개인용으로 Platform은 Other 등을 선택합니다.
5. 발급된 **App api_id**(숫자)와 **App api_hash**를 보관합니다.

이 값은 BotFather의 봇 토큰과 다릅니다. 다른 사람이 운영하는 채널을 읽으려면 Telethon의 사용자 계정 로그인이 필요합니다.

## 2. 개인 알림 봇 만들기

1. 텔레그램의 공식 [@BotFather](https://t.me/BotFather)를 엽니다.
2. `/newbot`을 보내고 안내에 따라 봇 표시 이름과 `bot`으로 끝나는 사용자명을 지정합니다.
3. 받은 토큰을 보관합니다. 예시 형식: `123456789:발급된_문자열`.
4. 생성한 **내 봇의 개인 대화**를 열어 Start 버튼 또는 `/start`를 보냅니다. BotFather 대화에 보내는 것이 아닙니다.

초기 `/start`만으로는 소유자를 등록하지 않습니다. 설치 후 `/start 등록코드`로 인증합니다. 이 봇은 이 프로그램 전용으로 사용하세요. 다른 프로그램의 `getUpdates`나 webhook과 함께 사용할 수 없습니다.

## 3. 저장소와 .env 준비

쓰기 가능한 NAS 폴더에서 실행합니다.

```bash
git clone https://github.com/summerbank/codex-reset-kst.git
cd codex-reset-kst
cp -n .env.sample .env
chmod 600 .env
vi .env
```

이미 저장소가 있으면 해당 디렉터리로 이동해 사용합니다. `cp -n`은 기존 `.env`를 덮어쓰지 않습니다. `vi` 대신 NAS 파일 편집기를 사용해도 됩니다. 다음 값을 실제 발급 정보로 바꿉니다.

```ini
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=발급받은_API_HASH
BOT_TOKEN=BotFather에서_발급받은_토큰
SOURCE_CHANNEL=@codex_resets
BOT_REGISTRATION_SECRET=
BOT_CHAT_ID=
```

| 항목 | 필수 여부 | 설명 / 예시 |
| --- | --- | --- |
| `TELEGRAM_API_ID` | 필수 | my.telegram.org에서 발급된 양의 정수. 예: `12345678` |
| `TELEGRAM_API_HASH` | 필수 | 같은 화면에서 받은 API Hash 문자열. 예제 문구를 실제 값으로 교체 |
| `BOT_TOKEN` | 필수 | BotFather의 토큰 전체. 콜론 포함 |
| `SOURCE_CHANNEL` | 필수 | **`@`을 붙인 공개 채널 사용자명**. 예: `@codex_resets`. `https://t.me/...` 주소가 아님 |
| `BOT_REGISTRATION_SECRET` | 자동 등록 시 필수 | 아래 명령으로 생성하는 24~64자 영문/숫자/`_`/`-` 비밀코드. 자동 등록 후에도 `.env`에 유지 |
| `BOT_CHAT_ID` | 선택 | 개인 채팅의 **양의 숫자 ID**. 예: `123456789`. `@username`이나 전화번호가 아님. 자동 등록 시 빈칸 |
| `DATA_DIR` | 선택 | 로컬 Python 실행 시 상태 저장 폴더. 기본값은 코드 옆 `data/`. Compose는 `/app/data`로 지정하므로 `.env`에 추가할 필요 없음 |

`BOT_CHAT_ID`를 이미 아는 기존 사용자는 숫자를 유지하면 등록코드 없이 바로 사용할 수 있습니다. 저장된 소유자와 다른 숫자를 넣으면 시작을 거부합니다. 그룹/채널 대상은 지원하지 않습니다.

## 4. 이미지 빌드와 등록코드 생성

```bash
docker compose build
docker compose run --rm --no-deps monitor python monitor.py --generate-registration-secret
```

출력된 랜덤 문자열을 복사해서 `.env`의 `BOT_REGISTRATION_SECRET=` 뒤에 붙이고 저장합니다. 코드 생성 명령은 파일을 자동으로 수정하지 않습니다.

```bash
vi .env
```

코드를 채널, 그룹, GitHub, 스크린샷으로 공유하지 마세요. 누구든 이 코드를 먼저 사용하면 최초 소유자가 될 수 있습니다. 등록 이후에는 올바른 코드를 아는 다른 계정도 소유자를 덮어쓸 수 없습니다.

## 5. NAS에서 사용자 계정 최초 인증

백그라운드 실행 전에 **한 번** 실행합니다. 이미 실행 중인 컨테이너가 있다면 먼저 중지합니다.

```bash
docker compose stop
docker compose run --rm monitor python monitor.py --login
```

프롬프트에 순서대로 입력합니다.

1. 사용자 전화번호: `+821012345678`처럼 국가번호 포함.
2. 텔레그램 앱 등으로 받은 로그인 코드.
3. 2단계 인증을 사용하면 계정 비밀번호. 입력 문자가 화면에 표시되지 않을 수 있습니다.

`Telegram user authentication saved`가 나오고 명령이 종료되면 성공입니다. 봇 토큰을 전화번호 프롬프트에 넣지 마세요. 인증코드와 비밀번호를 `.env`에 저장할 필요는 없습니다.

인증 세션은 NAS의 `./data/telegram_user.session`에 저장됩니다. `--rm`은 일회용 컨테이너만 제거하고 이 폴더는 남깁니다. 인증 중에는 모니터를 동시에 실행하지 마세요.

## 6. 백그라운드 실행과 개인 채팅 등록

```bash
docker compose up -d
docker compose logs -f --tail=100 monitor
```

`Watching @codex_resets`와 등록 대기 로그가 보이면, 내 봇의 **개인 대화**에서 `/start` 뒤에 공백과 4단계에서 만든 실제 코드를 붙여 보냅니다.

```text
/start 여기에_생성한_실제_등록코드
```

`개인 채팅 등록 완료!` 답장이 오면 끝입니다. Chat ID를 조회하거나 `.env`에 옮길 필요가 없습니다. 이후 `/start`만 보내면 등록 상태를 확인합니다. 코드가 없거나 틀린 요청, 그룹 대화, 다른 계정의 등록 시도에는 응답하지 않습니다.

소유자 ID와 봇 업데이트 위치는 `./data/sent.sqlite3`에 보존됩니다. 컨테이너 재시작/이미지 업데이트 후에도 등록은 유지됩니다. 봇이 꺼져 있을 때 보낸 명령은 Telegram에서 최대 24시간 보관되므로, 응답이 없으면 모니터가 실행된 뒤 다시 보내세요.

로그 보기의 `Ctrl+C`는 로그 보기만 종료합니다. 서비스 상태는 다음으로 확인합니다.

```bash
docker compose ps
```

## 7. 실제 알림 전송 테스트

등록 확인 답장을 받은 후 실행합니다. 같은 세션을 동시에 열지 않도록 먼저 모니터를 중지합니다.

```bash
docker compose stop
docker compose run --rm monitor python monitor.py --test-notification
docker compose up -d
```

봇에서 현재 KST가 적힌 `Codex 초기화 테스트 알림`을 확인하세요. 이 명령은 테스트 메시지만 보내며 채널 감지 자체를 검증하지는 않습니다. 실제 감지는 다음 원본 채널 공지가 올라왔을 때 원문 링크와 한국시간으로 확인합니다.

## 운영 명령

재시작 (단순 프로세스 재시작):

```bash
docker compose restart monitor
```

`.env` 변경 적용 (restart만으로는 변경되지 않음):

```bash
docker compose up -d --force-recreate monitor
```

코드 업데이트:

```bash
git pull --ff-only
docker compose up -d --build
docker compose logs --tail=100 monitor
```

중지 / 다시 실행:

```bash
docker compose stop
docker compose up -d
```

현재 Compose는 `./data:/app/data` 바인드 볼륨을 사용합니다. 세션, 소유자 등록, 전송 기록, 전송 대기열이 모두 `./data/`에 남습니다. `data/`를 삭제하거나 빈 폴더로 교체하면 인증과 등록 및 중복 방지 기록이 사라집니다. NAS 백업에는 `.env`와 `data/`를 함께 포함하고 외부 공개를 막으세요. SQLite 백업은 서비스를 중지한 상태에서 `data/` 전체를 복사하는 것이 안전합니다. Docker 로그는 파일당 10MB, 최대 3개로 회전합니다.

## 처리 규칙과 복구 범위

- `announced`는 발표, `expected`와 실제 채널의 `Possible Codex reset`은 예상, `confirmed`는 완료 확인으로 표시합니다. 공지 첫 번째 비어 있지 않은 줄만 파싱하여 인용문 속 다른 상태/날짜를 잘못 읽지 않습니다.
- 연도 없는 영문 날짜는 **원본 게시 시각(UTC)**에 가장 가까운 전년/당년/다음 해를 선택합니다. 연말·연초와 윤일을 처리하고, 180일 넘게 떨어진 모호한 날짜와 유효하지 않은 날짜는 무시합니다. 명시된 연도도 지원합니다. 수정 시각을 기준으로 연도를 바꾸지 않습니다.
- 시간이 없는 `confirmed`는 시간을 추정하지 않고 “시간 표기 없음”으로 알립니다.
- 같은 채널 메시지의 상태 또는 UTC 시간이 바뀌면 재알림합니다. 인용문/서식만 바뀐 수정은 재알림하지 않습니다. 아직 전송되지 않은 여러 수정은 최신 상태로 합칩니다.
- **처음 실행하거나 채널을 처음 바꿀 때 이전 공지를 일괄 전송하지 않습니다.** 그 시점 이후의 메시지를 추적합니다. 구버전에서 처음 업그레이드할 때도 같은 기준을 설정하고 기존 `sent` 기록은 보존합니다.
- 재시작/연결 복구 후 마지막 스캔 위치 이후의 새 게시물을 복구합니다. 60초마다 기록을 대조하고 최근 100개 메시지의 수정도 확인합니다. 초기 추적 기준 이전의 미전송 과거 공지는 제외합니다. 중단 중 수정된 100개 범위 밖의 과거 게시물은 복구를 보장하지 않습니다.
- 알림은 SQLite 대기열에 먼저 저장하고, 개인 채팅 등록 전이나 전송 실패 시에도 남겨 둡니다. 일시 장애와 429 제한은 재시도하며 `retry_after`를 따릅니다. 봇 차단을 해제하면 대기 알림을 다시 시도합니다.
- 일반적인 중복은 채널 ID + 메시지 ID + 상태/시간으로 방지합니다. Telegram이 전송은 받았지만 응답이 유실되거나 전송 직후 DB 기록 전에 프로세스가 종료되면 드물게 중복될 수 있습니다. 엄밀한 “정확히 한 번” 전송은 보장하지 않습니다.
- 삭제된 원문을 조회해 이미 쌓인 대기 알림까지 취소하는 기능은 없습니다. 같은 메시지가 비공지로 수정된 것을 감지하면 대기열에서 제외합니다.
- 같은 `DATA_DIR`의 동시 프로세스는 잠금으로 거부합니다. 서로 다른 NAS/폴더에서도 같은 봇으로 프로그램을 동시에 실행하지 마세요.

## 문제 해결

| 증상 | 확인할 내용 |
| --- | --- |
| `Missing` 또는 `.env에 ... 설정` | `.env.sample`이 아니라 `.env`를 편집했는지, 예제 문구를 실제 발급 정보로 바꿨는지 확인 |
| 자동 등록에 코드가 필요하다는 오류 | 4단계에서 코드를 생성하고 `.env`에 저장. 숫자 `BOT_CHAT_ID`가 있으면 코드 생략 가능 |
| 최초 인증 필요 | 서비스 중지 후 5단계 `--login` 실행. `.env`의 API ID/Hash는 사용자 계정용 값 |
| 입력 프롬프트가 보이지 않음 | SSH의 대화형 터미널에서 `docker compose run --rm ... --login` 실행. `up -d`로 최초 인증하지 않음 |
| 봇이 `/start`에 답하지 않음 | 내 봇 개인 대화인지, 서비스가 실행 중인지 확인. 최초에는 `/start 실제등록코드`가 필요. 등록 후에는 소유자의 `/start`만 응답 |
| `code=401` / `404` | BotFather 토큰 확인. 노출됐다면 BotFather에서 재발급 후 `.env` 수정 및 컨테이너 재생성 |
| `code=409` | 같은 봇의 다른 `getUpdates` 프로그램/컨테이너를 중지. webhook 설정 봇은 다른 서비스에 영향이 없도록 전용 봇으로 교체 |
| `code=403` / 전송 대기 | 내 봇 차단 해제 및 `/start` 전송. 저장된 소유자가 맞는지 확인 |
| `code=429` | Telegram 제한. 지정된 대기 시간 뒤 자동 재시도하므로 반복 재시작하지 않음 |
| 인증 취소 / 계정 인증 오류 | `docker compose logs --tail=100 monitor` 확인. Telegram 앱에서 세션이 취소됐다면 서비스 중지 후 `--login` 재실행 |
| 연결/스캔 재시도 | NAS DNS, 시간 동기화, 방화벽, 인터넷 및 Telegram 접근 확인. 일시 오류는 재시도하고 연결 종료 시 Compose가 재시작 |
| 새 알림이 없음 | 첫 실행은 과거 공지를 보내지 않음. 동일 사용자 계정의 채널 가입, `SOURCE_CHANNEL=@codex_resets`, 등록 확인 답장, 원문 신규 공지 확인 |
| DB 잠금 / 동시 프로세스 오류 | `--login` 또는 테스트 실행 전에 `docker compose stop`. 동일 `data/`를 쓰는 다른 컨테이너가 없는지 확인 |
| Permission denied | NAS 폴더/마운트 접근 권한 확인. 컨테이너가 만든 `data/` 파일은 NAS에서 root 소유일 수 있음. `chmod 777`로 공개하지 않음 |
| `.env`를 바꿨는데 그대로 | `docker compose up -d --force-recreate monitor` 실행 |

소유자 또는 봇을 바꿔야 한다면 기존 등록을 덮어쓰거나 DB를 지우지 말고, 서비스를 중지한 뒤 기존 데이터를 별도 폴더에 보관하세요. **아래는 의도적으로 소유자를 새로 등록하려는 경우에만** 실행합니다. 이미 `data.previous`가 있으면 먼저 다른 백업 이름을 정하세요.

```bash
docker compose stop
mv -n data data.previous
```

`.env`에서 새 봇/등록코드를 설정하고 `BOT_CHAT_ID`를 비운 다음, 5~7단계를 다시 진행합니다. 새 데이터 폴더이므로 사용자 계정 인증도 다시 필요하며 이전 공지는 가져오지 않습니다. 기존 인증 파일을 GitHub나 채팅으로 전달하지 마세요.

## 개발 및 검증

Python 3.12 이상과 Linux 환경에서 실행합니다. 운영 이미지는 Python 3.12를 사용합니다. 테스트는 실제 Telegram 접속이나 `.env` 읽기 없이 파서, DB, 비밀코드 등록, API 모형을 검증합니다.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

Docker와 같은 Python 3.12 환경에서도 확인할 수 있습니다.

```bash
docker compose build
docker run --rm --entrypoint sh -v "$PWD/tests:/tests:ro" codex-reset-kst-monitor -c 'python -m unittest discover -s /tests -v'
```

`.env`, `*.session*`, SQLite 파일과 `data/`는 Git 및 Docker 빌드 컨텍스트에서 제외합니다. 로그에 Bot API URL/토큰이나 인증코드를 출력하지 않습니다. 세션 파일은 계정 접근 권한을 가진 민감정보로 취급하세요. 커밋 전에는 `git diff --cached`로 민감정보가 없는지 확인합니다.

프로토콜 참고: [Telegram Bot API](https://core.telegram.org/bots/api#getupdates), [Telethon 클라이언트 문서](https://docs.telethon.dev/en/stable/modules/client.html). 원본 채널 문구가 바뀌면 파서 업데이트가 필요할 수 있습니다.
