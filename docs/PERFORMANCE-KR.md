# MiroFish 성능 측정 (O4: CPU / 메모리 / 디스크)

이 문서는 리소스 개선 작업(I10) 전후를 같은 조건으로 비교한 결과입니다.

- **BEFORE**: 커밋 `ceb8e32` (개선 전). 임시 git worktree에서 측정했습니다.
- **AFTER**: 현재 작업 트리 (개선 후).
- 측정일: 2026-11-02. 장비: Apple M1 Pro, macOS (kernel 25.6), Python 3.12.11 (`backend/.venv`).
- 네트워크와 LLM 호출은 없습니다. 모든 입력은 시드(`20261102`)로 만든 합성 데이터입니다. 임시 디렉터리는 측정 후 삭제합니다.
- 각 값은 1회 측정입니다. 바이트 수와 호출 횟수는 결정적이지만, 시간 값은 같은 기계에서도 흔들릴 수 있습니다.
- 변화율은 `(after - before) / before`입니다. 원본 출력은 아래 재현 방법의 `bench_o4.py`를 다시 실행하면 같은 형식(`O4RESULT {...}`)으로 나옵니다.

## 재현 방법

벤치 스크립트는 `backend/tests/perf/bench_o4.py`입니다. 파일 이름이 `bench_*.py`이므로 기본 `pytest` 실행에는 포함되지 않습니다.

```bash
# BEFORE checkout (throwaway worktree, reuses AFTER's venv and node_modules)
git worktree add --detach /tmp/mirofish-before ceb8e32
ln -s "$PWD/frontend/node_modules" /tmp/mirofish-before/frontend/node_modules

# one bench per process; run them one at a time
for b in detail runstates reqlog actionlog ipc disk frontend; do
  backend/.venv/bin/python backend/tests/perf/bench_o4.py --root /tmp/mirofish-before $b
  backend/.venv/bin/python backend/tests/perf/bench_o4.py --root "$PWD" $b
done

# cleanup
git worktree remove --force /tmp/mirofish-before && git worktree prune
```

각 실행은 `O4RESULT {...}` 형식의 JSON 한 줄을 출력합니다. 아래 표의 "명령" 열은 `--root <checkout>` 자리에 BEFORE/AFTER 경로를 넣어 실행한 명령입니다.

## 결과 요약

| # | 지표 | BEFORE | AFTER | 변화 | 명령 |
|---|---|---:|---:|---:|---|
| 1a | detail 응답 크기, 전체 형식 (`no after`) | 3,174,494 B | 3,174,510 B | +0.0% | `bench_o4.py --root <checkout> detail` |
| 1b | detail 응답 크기, AFTER `compact=1` (BEFORE 전체 형식 대비) | 3,174,494 B | 1,603,477 B | -49.5% | 같음 |
| 1c | detail 응답 크기, AFTER 커서 `after=0&limit=200` | 3,174,494 B | 73,326 B | -97.7% | 같음 |
| 1d | detail 응답 크기, AFTER 커서 `after=4800&limit=200` | 3,174,494 B | 74,155 B | -97.66% | 같음 |
| 1e | detail 응답 크기, AFTER 새 action 없는 폴링 (`after=5000`) | 3,174,494 B | 784 B | -99.98% | 같음 |
| 1f | detail 서버 시간, 전체 형식 (중앙값, 5회) | 185.62 ms | 44.44 ms | -76.1% | 같음 |
| 1g | detail 서버 시간, AFTER `compact=1` | 185.62 ms | 31.86 ms | -82.8% | 같음 |
| 1h | detail 서버 시간, AFTER 커서 `after=0&limit=200` | 185.62 ms | 1.84 ms | -99.0% | 같음 |
| 1i | detail 서버 시간, AFTER 새 action 없는 폴링 | 185.62 ms | 0.32 ms | -99.83% | 같음 |
| 2a | 완료된 run state 200개 저장 후 메모리에 남은 개수 (TTL 경과 + `get_run_state()` 후) | 200 | 0 | -100% | `bench_o4.py --root <checkout> runstates` |
| 2b | 위 시점의 Python 힙 증가량 (tracemalloc) | 5,579,532 B | 34,165 B | -99.4% | 같음 |
| 2c | 같은 구간의 프로세스 RSS 증가량 (`ps -o rss`) | 18,726,912 B | 17,186,816 B | -8.2% | 같음 (해설 참고) |
| 3a | 요청 로깅 CPU 시간, 50 kB JSON POST 2,000회 | 1.613 s | 0.369 s | -77.1% | `bench_o4.py --root <checkout> reqlog` |
| 3b | 요청당 CPU | 0.8066 ms | 0.1846 ms | -77.1% | 같음 |
| 3c | 같은 2,000회 동안 앱 로그에 쓴 바이트 | 58,784,183 B | 328,000 B | -99.4% | 같음 |
| 4a | action 로거 10,000건 (한 라운드) 소요 시간 | 532.44 ms | 77.1 ms | -85.5% | `bench_o4.py --root <checkout> actionlog` |
| 4b | 같은 경우 `actions.jsonl` `open()` 호출 수 | 10,000 | 200 | -98.0% | 같음 |
| 4c | action 로거 100라운드 x 100건 소요 시간 | 537.53 ms | 85.86 ms | -84.0% | 같음 |
| 4d | 같은 경우 `open()` 호출 수 | 10,200 | 400 | -96.1% | 같음 |
| 5a | IPC 유휴 루프 스캔 수, 가상 시계 60초 | 120 | 32 | -73.3% | `bench_o4.py --root <checkout> ipc` |
| 5b | IPC 유휴 루프 스캔 수, 실제 10초 구간 | 20 | 7 | -65.0% | 같음 |
| 6a | 프론트엔드 dist 전체 (파일 합계) | 1,308,528 B | 834,892 B | -36.2% | `bench_o4.py --root <checkout> frontend` |
| 6b | 프론트엔드 dist 전체 (`du -sk`) | 1,288 KiB | 876 KiB | -32.0% | 같음 |
| 6c | 가장 큰 JS 청크 (재측정, 최종 트리) | 520,165 B | 156,690 B (`vue-vendor`) | -69.9% | `npx vite build` |
| 6d | 한국어 첫 화면(홈)이 실제로 불러오는 JS 합계 (최종 트리) | 520,165 B (gzip 168,420) | 358,705 B (gzip 127,522) | -31.0% (gzip -24.3%) | `vite preview` + 브라우저가 불러온 청크 목록의 파일 크기 합산 |
| 6d | 첫 화면 JS (`index` + `vue-vendor`) | 520,165 B | 328,872 B | -36.8% | 같음 |
| 6e | 로고 이미지 | 600,901 B (JPEG) | 31,572 B (WebP) | -94.7% | 같음 |
| 7 | 시뮬레이션 1회(5,000 actions)가 디스크에 남기는 크기 (action 로그 + `simulation.log` + `run_state.json`) | 1,614,790 B | 1,614,790 B | +0.0% | `bench_o4.py --root <checkout> disk` |

## 지표별 해설

### 1. 시뮬레이션 detail 엔드포인트
- 입력: action 5,000건 (twitter 2,515, reddit 2,485), 50라운드. Flask 테스트 클라이언트로 `GET /api/simulation/<id>/run-status/detail`을 6번 호출했습니다. 시간은 첫 요청을 뺀 5회의 중앙값입니다.
- 전체 형식 응답 크기는 거의 같습니다(AFTER에 `llm_models` 키가 추가되어 16 B 증가). 하지만 로그를 한 번만 읽으므로 서버 시간은 185.62 ms에서 44.44 ms로 줄었습니다.
- BEFORE에는 첫 요청이 93.3 ms, 이후 중앙값이 185.62 ms로 나왔습니다. 첫 요청이 더 빠른 이유는 확인하지 않았습니다. 측정값 그대로 적었습니다.
- 커서 방식에서는 실행 중 폴링 한 번이 3,174,494 B에서 200건당 73,326 B, 새 action이 없으면 784 B로 줄어듭니다. 1b~1e, 1g~1i는 BEFORE에 같은 기능이 없어서 BEFORE 전체 형식과 비교했습니다.

### 2. 백엔드 메모리 (run state 보관)
- 완료된 run state 200개(각 action 50건)를 `_save_run_state`로 저장했습니다. AFTER는 주입 시계를 601초 앞으로 돌린 뒤(TTL 기본값 600초) `get_run_state()`를 호출했습니다. BEFORE는 만료 기능이 없어서 200개가 모두 남습니다.
- tracemalloc 기준으로 남은 Python 객체는 5,579,532 B에서 34,165 B로 줄었습니다.
- RSS는 크게 줄지 않습니다(-8.2%). Python 할당자가 해제된 메모리를 운영체제에 바로 돌려주지 않기 때문입니다. AFTER에서도 만료 직후 RSS는 저장 직후 값과 거의 같습니다(77,774,848 B → 77,807,616 B). 따라서 RSS 값은 절감의 근거가 아닙니다. 장시간 실행 시 힙이 계속 커지지 않는다는 근거는 2a·2b입니다.

### 3. 요청 로깅 CPU
- `LOG_REQUEST_BODIES`를 설정하지 않은 상태(기본값)에서 50,091 B JSON 본문을 2,000번 POST했습니다.
- BEFORE는 매 요청마다 본문을 파싱하고 로그에 기록했습니다. AFTER는 옵트인 전에는 파싱하지 않습니다. CPU는 77.1%, 로그 디스크 쓰기는 99.4% 줄었습니다(58,784,183 B → 328,000 B).

### 4. action 로거
- `builtins.open`을 감싸서 `actions.jsonl`을 연 횟수를 셌습니다.
- AFTER는 50줄 또는 1초 단위로 모아서 씁니다. 라운드 시작/종료 이벤트는 즉시 기록되므로, 라운드가 있는 경우 `open()` 횟수가 400입니다. 파일 내용(줄 수 10,000/10,200, 크기 3,128,000/3,158,370 B)은 BEFORE와 같습니다.

### 5. IPC 유휴 루프
- 가상 시계 60초: BEFORE는 0.5초 고정 대기라서 120회, AFTER는 0.5→1→2초 백오프라서 32회입니다.
- 실제 10초 구간: 20회 → 7회.
- BEFORE 코드는 루프가 `main()` 안에 직접 들어 있어서 함수로 가져올 수 없습니다. 그래서 벤치가 같은 루프를 복제하고, 대기 시간 0.5초는 BEFORE 소스에서 직접 읽어옵니다. AFTER는 실제 `run_command_loop`와 `IdleBackoff`를 사용합니다.

### 6. 프론트엔드 빌드
- `npx vite build`를 임시 출력 디렉터리에 실행했습니다. 첫 화면 JS는 `index.html`이 직접 불러오는 `.js` 파일(script + modulepreload)의 합입니다.
- BEFORE는 JS 청크 1개(520,165 B)입니다. 최종 트리에서는 언어 파일(zh/en/ko 각 47~57 kB)과 AI 연결 패널을 별도 청크로 나눠, 첫 화면에는 현재 언어와 영어만 불러옵니다. 6c·6d는 최종 트리에서 다시 잰 값입니다. 계획의 Q7(메인 청크 < 520 kB, 로고 ≤ 120 kB)을 만족합니다.
- 중간 측정 때 `index` 청크가 173,875 B까지 커졌던 원인은 990개 키짜리 언어 파일 3개를 한꺼번에 묶어 넣은 것이었고, 지연 로딩으로 바꿔 진입 청크는 47,013 B가 됐습니다.

### 7. 시뮬레이션당 디스크 사용량
- 실제 `SimulationLogManager`/`PlatformActionLogger`로 action 5,000건을 50라운드에 걸쳐 기록하고, `SimulationRunner._save_run_state`를 라운드마다 한 번, 완료 시 한 번 호출했습니다.
- 최종 파일 크기는 BEFORE/AFTER가 같습니다(파일 형식이 바뀌지 않았기 때문). 구성: twitter `actions.jsonl` 813,674 B, reddit `actions.jsonl` 776,969 B, `run_state.json` 21,956 B, `simulation.log` 2,191 B.
- **측정하지 않음(UNMEASURED)**: `run_state.json`을 51번 다시 쓰면서 누적된 쓰기 바이트. 이 벤치는 최종 크기만 봅니다. 디스크 쓰기가 실제로 줄어든 부분은 지표 3c(요청 로그)입니다.

## 측정하지 않은 항목
- OASIS 실행 전체(실제 LLM 호출 포함)의 CPU/메모리: 네트워크와 LLM을 쓰지 않는다는 측정 조건 때문에 제외했습니다.
- 그래프 데이터 `ETag`/304, 프론트엔드 렌더링 action 수 제한(≤ 500): 이 벤치 범위 밖입니다.
- 지표 7의 누적 쓰기 바이트: 위 7번 참고.
