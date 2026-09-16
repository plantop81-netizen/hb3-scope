# doctor-watch — 병원 홈페이지 의료진 변동(이직) 주간 추적기

전국 병원 홈페이지의 **의료진 소개 페이지를 매주 자동 수집**하고, 지난주 명단과 비교해
**신규 합류 · 명단 제외 · 진료과/직위 변경**을 찾아내며, A 병원에서 빠지고 B 병원에 나타난 의사를
**이직으로 자동 연결**합니다. 결과는 매주 월요일 아침 Slack/Telegram/이메일로 브리핑되고,
언제든 웹 대시보드에서 병원별·의사별로 확인할 수 있습니다.

```
심평원 API ──▶ 병원 목록(홈페이지 URL) ──▶ 의료진 페이지 자동 탐색 ──▶ 수집(EUC-KR/JS 대응)
                                                                      │
        ⭐ 고객 명단 대조 ◀── 이직 연결 ◀── 지난주와 비교 ◀── Claude 로 이름/진료과/직위 추출
                │
                ├─▶ 월요일 07:00 브리핑 (Slack · Telegram · 이메일 · reports/*.html)
                └─▶ 웹 대시보드 (python -m doctor_watch serve)
```

## 왜 이 방식인가 (검토한 대안)

| 방법 | 판단 |
|---|---|
| 심평원 **병원정보서비스 API** (`getHospBasisList`) | 전국 요양기관의 **홈페이지 URL(`hospUrl`)·신고 의사 수** 를 무료로 제공 → "홈페이지 있는 병원 리스트업"을 사람이 할 필요가 없음. **채택 (병원 목록 소스)** |
| 심평원 **전문과목별 전문의 수** (`getSpcSbjtSdrInfo`) | 이름은 없지만 과별 인원 증감이 나옴. 홈페이지가 없는 병·의원용 보조 신호. **채택 (`hira-counts`)** |
| 병원 홈페이지 의료진 페이지 크롤링 | 이름·진료과·직위가 있는 유일한 공개 소스. 사이트마다 형식이 달라 규칙 파싱은 불가능에 가까움 → **Claude 구조화 추출로 해결. 채택 (핵심)** |
| 의협/학회 명부, 논문 소속 | 갱신 주기가 길고 접근 제한. 미채택 |
| 뉴스/보도자료 검색 | 대형병원 교수급만 잡힘. 미채택 (추후 보조 신호로 추가 가능) |

핵심 설계 포인트
- **내용이 바뀐 페이지만 Claude 를 호출**합니다(해시 캐시). 수천 곳을 매주 돌려도 대부분은 API 비용 0.
- 사이트가 죽거나 개편으로 명단이 통째로 사라진 주에는 "제외" 오탐을 내지 않고 **검토 필요**로만 표시합니다.
- 동명이인은 진료과로 구분하고, 이직 연결은 진료과가 명백히 다르면(내과↔정형외과) 연결하지 않습니다.
- **고객 명단(watchlist)** 에 있는 의사의 변동은 브리핑 맨 위에 ⭐ 로 올라옵니다.

## 빠른 시작 (로컬)

```bash
cd doctor-watch
pip install -r requirements.txt
cp .env.example .env            # HIRA_SERVICE_KEY, ANTHROPIC_API_KEY 입력

# 1) 병원 목록: 심평원 API 로 가져오기 (부산·울산·경남의 상급종합/종합병원/병원 중 홈페이지 보유)
python -m doctor_watch init-hospitals --sido 부산,울산,경남 --with-url-only
#    또는 CSV 로 직접 등록 (name,url,staff_urls ...)
python -m doctor_watch import-hospitals data/hospitals.seed.csv

# 2) 고객 의사 명단 등록 (선택)
python -m doctor_watch watchlist import --csv data/watchlist.example.csv

# 3) 첫 수집 (기준선) — 몇 곳만 먼저 시험
python -m doctor_watch run --limit 5 --report
python -m doctor_watch run --report          # 전체

# 4) 대시보드
python -m doctor_watch serve                 # http://127.0.0.1:8000
```

두 번째 실행부터 변동이 계산됩니다. `python -m doctor_watch report --stdout` 로 브리핑을 바로 볼 수 있고,
`python -m doctor_watch notify --dry-run` 으로 발송 문구를 미리 볼 수 있습니다.

## 매주 월요일 07:00 자동 실행 (GitHub Actions)

`.github/workflows/doctor-watch.yml` 이 **매주 일요일 21:30 UTC(= 월요일 06:30 KST)** 에 실행되어
수집 → 비교 → 보고서 → 발송 → 결과(DB, reports/) 커밋까지 합니다. 서버가 필요 없습니다.

저장소 **Settings → Secrets and variables → Actions** 에 등록:

| 종류 | 이름 | 설명 |
|---|---|---|
| Secret | `HIRA_SERVICE_KEY` | 공공데이터포털 병원정보서비스 키 (없으면 CSV 목록만 사용) |
| Secret | `ANTHROPIC_API_KEY` | Claude API 키 (없으면 규칙 기반 추출로 동작, 정확도 하락) |
| Secret | `SLACK_WEBHOOK_URL` | Slack Incoming Webhook (선택) |
| Secret | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | 텔레그램 (선택) |
| Secret | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `MAIL_FROM`, `MAIL_TO` | 이메일 (선택) |
| Variable | `DOCTOR_WATCH_SIDO` | 예: `부산,울산,경남` (비우면 전국) |
| Variable | `DOCTOR_WATCH_MODEL` | 기본 `claude-opus-5`. 비용 절감 시 `claude-haiku-4-5` |
| Variable | `DOCTOR_WATCH_CONCURRENCY` | 동시 접속 수 (기본 8) |

Actions 탭에서 **Run workflow** 로 수동 실행(테스트용 `limit`, `no_llm` 입력 가능)도 됩니다.
주간 보고서는 `doctor-watch/reports/index.html` 에 항상 최신본이 커밋되며, 워크플로 artifact 로도 받을 수 있습니다.

## 웹 대시보드

- **브리핑**: 이번 주 변동, 이직 추정, 고객 명단 변동, 수집 실패 병원. 과거 주차 선택 가능.
- **병원**: 목록/상태, 병원별 현재 의료진·변동 이력·수집 페이지. 의료진 페이지 URL 수동 지정, 비활성화.
- **의사 검색**: 이름으로 현재 소속과 변동 이력 조회.
- **고객 명단**: 추가/삭제/CSV 업로드, 해당 변동 이력.
- **지금 수집** 버튼으로 즉시 실행. JSON API: `/api/briefing`, `/api/hospitals/{id}/roster`, `/api/status`.

```bash
python -m doctor_watch serve --host 0.0.0.0 --port 8000
```

## 명령어

| 명령 | 설명 |
|---|---|
| `init-hospitals --sido 부산 --cl 상급종합,종합병원,병원 --with-url-only` | 심평원 API 로 병원 등록/갱신 |
| `import-hospitals file.csv` | CSV 로 병원 등록 (`name,url[,ykiho,sido,cl_name,addr,staff_urls]`, staff_urls 는 `\|` 구분) |
| `hospitals` | 병원 목록·상태 |
| `set-staff-urls <id> <url...>` | 자동 탐색이 실패한 병원의 의료진 페이지 수동 지정 |
| `run [--limit N] [--hospital-id ID] [--no-llm] [--report] [--notify]` | 수집·비교 |
| `report [--run-id N] [--stdout]` | 보고서(md/html/json) 생성 |
| `notify [--dry-run]` | 브리핑 발송 |
| `watchlist list\|add\|import\|remove` | 고객 명단 관리 |
| `search 이름` | 의사 검색 |
| `hira-counts --sido 부산` | 심평원 신고 의사 수 갱신·변동 기록 |
| `serve` | 대시보드 |

## 동작 원리

1. **탐색** `discover.py` — 홈페이지 링크 중 "의료진 소개/진료의료진/교수진/doctor/staff" 등을 점수화해 상위 후보를 고르고,
   그 안의 진료과별 하위 페이지·페이지네이션·`<select>` 진료과 옵션까지 병원당 최대 40페이지(설정) 따라갑니다.
   한 번 찾은 URL 은 저장되어 다음 주부터 바로 사용합니다. 못 찾으면 대시보드/CLI 로 수동 지정.
2. **수집** `fetch.py` — EUC-KR 등 인코딩 자동 판별, robots.txt 존중, 호스트별 요청 간격, 재시도.
   본문이 거의 비어 있고 SPA 흔적이 있으면 Playwright 로 렌더링(`DOCTOR_WATCH_RENDER=auto`).
3. **추출** `extract.py` — 페이지 텍스트를 Claude 에 넣어 `{name, department, position, specialty}` 를
   JSON 스키마 강제(structured output)로 받습니다. 직함·한자·영문 병기는 정규화(`names.py`).
   동일 내용(해시)은 캐시에서 재사용. API 키가 없거나 실패하면 규칙 기반 추출(표/직함 패턴)로 대체.
4. **비교** `diff.py` — 병원별로 **직전 성공 스냅샷**과 비교. 신규/제외/진료과 변경/직위 변경.
   명단이 절반 이하로 급감하면 사이트 개편 가능성으로 보고 변동 대신 "검토 필요" 처리.
5. **이직 연결** — 최근 8회 실행 범위에서 같은 이름의 `제외`↔`합류` 를 짝지어 `A → B` 로 표시.
6. **보고/발송** `report.py`, `notify.py` — Markdown/HTML/JSON 보고서와 채널 발송.

## 비용·규모 가늠

- 상급종합+종합병원+병원(요양·정신 제외) 은 전국 약 4천 곳, 홈페이지 보유는 그중 일부입니다. 시도 필터로 시작해 넓히는 것을 권합니다.
- Claude 호출은 **내용이 바뀐 페이지에만** 발생합니다. 첫 주(기준선)만 전체 호출이고, 이후엔 실제 변동이 있는 병원만 호출됩니다.
- 첫 주 전체 추출 비용을 낮추려면 `DOCTOR_WATCH_MODEL=claude-haiku-4-5` 로 기준선을 잡고 이후 `claude-opus-5` 로 바꿔도 됩니다(캐시는 해시 기준이라 모델 변경과 무관).

## 알려진 한계

- 홈페이지가 없거나 의료진을 이미지/PDF 로만 게시하는 병원은 잡히지 않습니다 → `hira-counts` 의 신고 의사 수 증감으로 보조.
- 병원이 홈페이지를 늦게 갱신하면 그만큼 탐지가 늦습니다 (이직 연결이 여러 주에 걸쳐 잡히도록 8회 실행까지 거슬러 매칭).
- 심평원 API 응답 필드명은 포털 명세 개정 시 바뀔 수 있습니다 → `hira.py` 의 `FIELD_MAP` 만 수정.
- 수집은 각 사이트의 robots.txt 를 따르며 요청 간격을 둡니다. 개인정보가 아닌 공개된 의료진 소개 정보만 다룹니다.

## 테스트

```bash
pip install pytest
python -m pytest -q tests
```
모의 병원 사이트 2주치(`tests/fixtures`)로 탐색→수집→추출→비교→이직 연결→보고서→대시보드까지 검증합니다.
