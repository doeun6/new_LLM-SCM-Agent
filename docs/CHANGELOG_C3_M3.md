# 변경 기록: C3 주문 통계 추가, M3 수치형 계획

- 기준 커밋: `f4770df` (main)
- 작성: 김도연, 2026-10-11
- 적용 범위: 조건 이름과 103조건 구성은 그대로입니다. C3를 쓰는 조건과 M3를 쓰는 조건의 처리 정의가 바뀌었습니다. 공통 주문·선택 프롬프트의 M3 설명 문구도 바뀌었으므로 **모든 조건의 소스 해시가 바뀝니다. 기존 결과 폴더를 `--resume`으로 이어 쓰지 마세요.**

## 1. C3: 과거 주문량 통계 추가

**변경 전:** 현재 D/I/B/P_pre + 현재 포함 10기 상태 통계. 과거 주문 정보 없음.

**변경 후:** 위 내용에 더해 t-9부터 t-1까지 9기(C0의 `past_orders`와 같은 기간)의 아래 세 항목에 대해 평균·표본 표준편차·최소·최대를 제공합니다.

| 항목 | 뜻 |
|---|---|
| `Q_base` | 그 기간에 코드가 계산한 기준 주문량 |
| `Q_final` | 그 기간에 실제로 상류에 보낸 주문량 |
| `Q_final_minus_Q_base` | 같은 기간의 Q_final − Q_base. 양수면 기준보다 더 주문함 |

- 추가 필드: `order_window`(9), `order_period_start`, `order_period_end`, `order_definitions`, `order_statistics`
- 항목 설명은 C3 데이터의 `order_definitions` 안에 넣었습니다. 그래서 C3 전용으로 프롬프트를 따로 만들지 않았습니다.
- 당기 주문은 아직 결정 전이므로 t-1까지만 사용합니다.
- 과거 Q_base에 `null`이 있으면 Q_base와 차이 통계는 `null`이고 Q_final 통계만 제공합니다. 현재 등록 조건에서 C3는 항상 기준량이 있으므로 실제로는 생기지 않습니다.
- **준비기간 처리:** 준비기간에는 기준량을 그대로 실행하므로 Q_base = Q_final입니다. 그래서 31기의 차이 통계는 평균 0, SD 0이고, 평가기간이 쌓이면서 실제 조정이 반영됩니다. C0의 원자료도 같은 0을 보여주므로 그대로 포함했습니다.
- 의도: C0와 C3가 같은 변수·기간을 **원자료로 주느냐, 통계로 주느냐**만 다르게 만들어, 기존 비교에 섞여 있던 '주문 이력 제거' 효과를 없앱니다.

예시 (31기 C3의 추가 부분, 모의 실행 seed 101 도매상):

```json
"order_statistics": {
  "Q_base":  {"mean": 9.44, "standard_deviation": 4.61, "minimum": 3, "maximum": 16},
  "Q_final": {"mean": 9.44, "standard_deviation": 4.61, "minimum": 3, "maximum": 16},
  "Q_final_minus_Q_base": {"mean": 0, "standard_deviation": 0.0, "minimum": 0, "maximum": 0}
}
```

## 2. M3: 문자열 계획 → 자기 계획 발주량 정수 2개

**변경 전:** `plan_t_plus_1`, `plan_t_plus_2` 문자열. 정성·정량 모두 허용, 빈 문자열만 검사.

**변경 후:**

```json
{"plan_t_plus_1": 12, "plan_t_plus_2": 10}
```

- 뜻: 송신자 **자신이** t+1, t+2기에 상류에 보낼 계획 발주량. 수요 예측값이나 기준 주문량이 아니며, 실제 주문으로 자동 실행되지 않습니다.
- 스키마: `plan_schema(order_cap)`가 정수, 0~상한(현재 100)을 지정합니다.
- 검사: `validate_schema`는 정수 자료형만 검사하고 `minimum`/`maximum`은 검사하지 않으므로, `_validate_message(value, "M3", order_cap)`에서 정수·범위를 직접 검사합니다. 소수(10.5), 문자열("10"), 불리언, 음수, 상한 초과는 모두 실패입니다.
- 실패 처리: 조용히 보정하지 않습니다. 형식 실패로 기록하고 **공통 재시도 1회**를 쓰며, 그래도 실패하면 빈 메시지(`null`)로 대체합니다. 이미 확정한 Q_final은 바뀌지 않습니다.
- 프롬프트:
  - 송신(`message_instructions`): 0~상한 정수, 자기 계획 발주량, 문장·이유·상태 보고 금지
  - 수신(`selection_instructions`, `order_instructions`): 두 값이 하류 송신자의 다음 두 기간 계획 발주량이며 보장된 수요가 아니고 당기 주문에 더하지 않는다는 설명 추가
  - `message_instructions`에 `order_cap` 인자가 추가되었습니다.
- `MockProvider`: M3에서 당기 Q_final을 두 값으로 돌려줍니다 (흐름 확인용).
- 계획 대비 실제 발주 오차 지표는 아직 구현하지 않았습니다. 원문 계획은 `outgoing_message`로 로그에 남으므로 사후 분석에서 계산할 수 있습니다.

## 3. M2

변경 없음. 기존 숫자·미래 행동 표현 검사와 재시도를 유지합니다. 실제 실행 후 표본 검토로 M2/M3 분리 정도를 확인할 계획입니다.

## 4. 수정 파일

| 파일 | 내용 |
|---|---|
| `scm/harness.py` | `ORDER_STAT_FIELDS`, `_summary`, `_order_statistics`, `build_C`의 C3 분기, `plan_schema`, `_validate_message`, `Harness.decide` 메시지 분기 |
| `scm/prompts.py` | M3 송신·수신 문구, `message_instructions(role, level, order_cap)` |
| `scm/provider.py` | `MockProvider`의 M3 응답 |
| `tests/test_harness.py` | C3 주문 통계 3개, M3 정수·범위, 상류 전달, 실패 시 주문 유지 테스트 |
| `tests/test_provider.py` | M3 범위 초과 → 공통 재시도 → 성공 테스트 |
| `README.md`, `docs/SYSTEM_GUIDE.md`, `docs/MODIFICATION_GUIDE.md` | 현재 정의 갱신 |

## 5. 확인 결과

- `python -m unittest discover -s tests`: 86개 통과 (기존 80개 + 신규 6개, 기존 C3 주문 제외 테스트는 새 정의로 교체), 1개 skip은 기존과 동일
- 모의 실행: `python -m scm run --conditions C3M0T2 C3M3T4 C0M3T3 C3M2T3 --seeds 101 --backend mock --evaluation-periods 2 --output results/design_check_c3m3` 정상 종료. 소매상의 M3 `{"plan_t_plus_1": 9, "plan_t_plus_2": 9}`가 도매상의 선택·주문 호출에 같은 값으로 전달됨을 확인
- 모의 실행은 정보 전달과 코드 흐름 확인용이며, LLM 성능 근거가 아닙니다.
