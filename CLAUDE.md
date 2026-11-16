# sonnun (손눈서브) — 작업 규칙

에이전트(Claude Code 등)가 이 저장소를 고칠 때의 규칙. 개요·구조는 `README.md`, 검증 기록은 `docs/VERIFICATION.md`.
테스트는 함수 단위가 아니라 실제로 띄워서(E2E) 산출물을 본다.

- 손눈서브는 도구(눈·손)다. 판단 로직을 확장이나 도구에 넣지 않는다 — 두뇌는 chat.py 의 세션들(과 외부 호출자).
- 권한(끔/눈길/손길)·멈춤·승인(확인 단어) 검사는 **확장(background.js)에서 실제 동작 직전에 한 곳**. 서버에 같은 검사를 또 만들지 않는다.
- 에이전트가 권한·승인·진짜 입력 설정을 바꾸는 도구는 만들지 않는다 (사용자만 패널·설정 화면에서).
- 설정 기본값(DEFAULTS)·사이트 권한 규칙(하위 주소 포함, 구체적인 규칙 우선)은 `extension/shared.js` 한 곳. background·패널·설정 화면이 같이 읽는다.
- 대화 턴은 작업 탭에 묶인다 (`service.py` 가 tab_id 를 붙임). 사용자의 탭 이동을 따라가게 바꾸지 않는다.
- 도구 실행은 모든 창구가 `SonnunService.run_tool` 하나를 거친다 (활동 기록·중지가 여기서).
- 도구를 더하면 `tools.py` TOOLS(+summarize·render) · `background.js` OPS/NEED · `client.py` 메서드 · `sidepanel.js` TOOL_NAME · `export.py`(조작이면) · `browser_check.py`(MCP 도구 수) 를 같이.
- 실모델 과제는 `tools/real_local_check.py`. 고친 뒤 같은 과제로만 확인하지 말고 홀드아웃 과제를 더한다 (`docs/VERIFICATION.md` 실모델 기록).
- 확장·패널을 바꾸면 `python tools/browser_check.py` 를 돌리고 스크린샷을 직접 본다 (README 캡처는 `tools/screenshots.py`).
- 음성: Google 엔진은 패널이 직접(Web Speech API), Whisper 엔진은 패널 녹음 → `/api/stt` → `tools/stt_server.py`. 녹음·받아적은 글은 저장하지 않는다.
- 이 컴퓨터에만 있는 경로·주소를 코드와 문서에 넣지 않는다 (`BASIC_AGENT_PATH`·`SONNUN_LLM_URL`·`SONNUN_STT_URL` 같은 환경 변수로).
