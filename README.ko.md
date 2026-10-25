# keydrop

[English](README.md)

API 키를 대화창에 붙여 넣지 않고 코딩 에이전트에게 넘긴다.

에이전트가 비밀값이 필요하면 keydrop 이 맥의 [Tailscale](https://tailscale.com) 주소에 일회용 입력 페이지를 연다.
휴대폰이나 다른 기기에서 링크를 열고 키를 붙여 넣으면 값은 `.env` 파일, macOS 키체인, 또는 stdin 을 읽는
명령으로 바로 간다. 에이전트는 저장된 변수 이름만 알게 되므로 값이 대화 기록·로그·명령줄에 남지 않는다.

[Agent Skill](https://agentskills.io) 하나를 Claude Code·Codex 플러그인으로 묶은 것이다. `SKILL.md` 하나와
표준 라이브러리만 쓰는 Python 스크립트 하나로 되어 있다. 저장한 값은 `keydrop exec` 로 명령 하나의 환경에만
넣어 쓴다. 그래서 같은 세션에서도 값을 출력하지 않고 바로 쓸 수 있다.

## 설치

| 클라이언트 | 방법 |
|---|---|
| Claude Code | `claude plugin marketplace add risingdream/keydrop-plugin` 후 `claude plugin install keydrop@keydrop-plugin` |
| Claude Desktop (Code) | 설정 → 플러그인 → 추가 → 저장소에서 추가 → `risingdream/keydrop-plugin` |
| Codex | `codex plugin marketplace add risingdream/keydrop-plugin` 후 `codex plugin add keydrop@keydrop-plugin` |
| OpenCode | 스킬 폴더를 `~/.config/opencode/skills/`(모든 프로젝트) 또는 `.opencode/skills/`(한 프로젝트)에 복사. 명령은 [README.md](README.md#install) |
| 그 밖의 에이전트 | `plugins/keydrop/skills/keydrop/` 폴더를 에이전트 스킬 폴더에 복사 |

같은 이름(`keydrop`)의 스킬이 이미 깔려 있으면 하나만 남긴다. 에이전트는 이름으로 하나만 고르므로 이 판이 안 쓰일 수 있다.

필요한 것: `python3` 3.9 이상. 기본 네트워크 모드는 Tailscale. `--keychain` 은 macOS.

## 사용

에이전트에게 키가 필요한 일을 시키면("이 프로젝트에 OpenAI 키 설정해 줘") 에이전트가 이렇게 실행한다.

```bash
python3 -I keydrop.py --env-auto --title myapp --var OPENAI_API_KEY:sk- --lang ko
```

`http://100.x.y.z:54321/<무작위>` 같은 링크를 받으면 tailnet 에 붙은 기기에서 열고 키를 붙여 넣은 뒤 저장을 누른다.
페이지가 닫히고 에이전트는 `{"event": "saved", "vars": ["OPENAI_API_KEY"]}` 만 받는다.

`--env-auto` 는 프로젝트의 `.env*` 중 git 이 무시하는 파일을 변수마다 고른다. 이미 정의된 파일이 있으면 그 파일(여럿이면 모두)을 고치고,
새 이름은 `.env.local` 또는 `.env` 에 쓴다. git 이 추적하는 파일과 `.env.example` 같은 견본은 쓰지 않으며, 안전한 파일이 없으면 페이지를 열기 전에 멈춘다.

저장한 값을 지금 세션에서 쓸 때는 명령을 `exec` 로 감싼다. 값은 그 명령의 환경에만 들어가고 출력되지 않으며,
종료 코드는 그 명령 것이 그대로 나온다. 이름이 없거나 비어 있으면 125 로 끝나고 아무것도 실행하지 않는다.

```bash
python3 -I keydrop.py exec --keychain myapp OPENAI_API_KEY -- python3 app.py
python3 -I keydrop.py exec --env-auto A_TOKEN -- sh -c 'curl -sf -H "Authorization: Bearer $A_TOKEN" https://api.example.com/health'
```

저장 대상(`--env`·`--keychain`·`--sink-cmd`), 옵션, 종료 코드, 보안 모델은 [README.md](README.md) 에 정리했다.

`exec --env-auto` 는 `.env` → `.env.local` 순서로 읽고(뒤가 이김), `--mode development` 를 주면 `.env.development`·`.env.development.local` 도 Next.js·Vite 와 같은 순서로 읽는다.

Claude Code 에서는 세션을 시작할 때 훅이 이 프로젝트의 git 무시 `.env*` 에 든 키 **이름**과 쓸 `exec` 명령을 알려 준다. 값은 프롬프트에 들어가지 않는다.
Codex 플러그인은 훅을 실을 수 없어(`plugin_hooks` 제거됨) 스킬 안내만 쓴다.

## 막는 것과 못 막는 것

- 막는 것: 비밀값이 에이전트 컨텍스트·대화 기록·모델 제공자 로그·셸 기록·프로세스 목록에 남는 일.
- 못 막는 것: 쓰기 전에 URL 을 얻은 같은 tailnet 사용자, 저장 뒤 `.env` 를 읽는 에이전트, 다른 프로젝트의 에이전트가 이 프로젝트 키를 읽는 일(같은 OS 사용자라 경계가 없다), 이미 침해된 기기.

## 라이선스

[MIT](LICENSE)
