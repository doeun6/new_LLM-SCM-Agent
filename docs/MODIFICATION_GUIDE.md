# 공동연구자를 위한 C3·M2·M3 수정 안내

> **현재 적용:** C3에는 과거 9기의 주문 통계가 포함되고, M3는 다음 두 기간의 자기 계획 발주량을 정수 두 개로 전달합니다. 변경 전후 내용은 [변경 기록](CHANGELOG_C3_M3.md)을 참고하세요. 아래 본문은 현재 코드를 기준으로 작성했습니다.

이 문서는 **현재 구현을 설명하고, 설계를 변경할 때 함께 수정해야 하는 코드와 검증 항목을 안내**합니다. M2의 이유 항목 선택형처럼 아직 구현하지 않은 대안은 따로 표시합니다. 전체 시스템과 실행 방법은 [README](../README.md)를 먼저 읽어 주세요.

현재 등록은 C 4수준 × M 5수준 × T 5수준의 100조합에 독립 기준 A, D_cost, D_bullwhip을 더한 **103조건**입니다. A는 C0와 당기 수주량만으로 최종 발주를 결정하며 T0 조합과 다릅니다.

## 1. 무엇이 확정되어 있고 무엇을 논의 중인가

| 항목 | 현재 동작 | 적용 상태 |
|---|---|---|
| C3 | 당기 D·I·B·P_pre + 당기 포함 10기 상태 통계 + 과거 9기 Q_base·Q_final·Q_final−Q_base 통계. 각 통계는 평균·표본 표준편차·최솟값·최댓값 | 적용 완료. 같은 원천 자료의 요약과 주문 차이의 파생 요약을 함께 제공 |
| M2 | 이미 확정한 당기 주문의 이유를 정성적 자유문장 하나로 전달. 숫자와 미래 행동 계획은 허용하지 않는 지시 | 기존 방식 유지. 정해진 이유 항목을 LLM이 선택하는 대안은 미구현 |
| M3 | `plan_t_plus_1`, `plan_t_plus_2`에 다음 두 기간의 **자기 계획 발주량**을 각각 0부터 수량 상한까지의 정수로 전달 | 적용 완료. 기본 상한 100, 잘못된 수량은 보정하지 않고 공통 재시도 후 실패 시 null 메시지 |

M2는 당기 판단의 이유이고 M3는 미래 발주 의도입니다. M3의 자료형·수량 범위는 검사하지만, 그 계획이 실제로 실행되거나 최적이라는 보장은 하지 않습니다. 실제 미래 수요나 미래 상태를 제공하는 설계는 아닙니다.

## 2. 수정 위치를 찾는 지도

문서의 파일 링크는 이 문서가 있는 `docs/` 폴더를 기준으로 작성했습니다. 줄 번호는 이후 편집에 따라 바뀌므로 함수·상수 이름으로 검색하는 편이 안전합니다.

| 파일 | 찾아볼 이름 | 담당 기능 |
|---|---|---|
| [scm/harness.py](../scm/harness.py) | `STATE_FIELDS`, `WINDOWS`, `build_C` | 자신의 현재 상태와 이력을 C 수준별로 가공 |
| [scm/harness.py](../scm/harness.py) | `ORDER_STAT_FIELDS`, `ORDER_STAT_DEFINITIONS`, `_summary`, `_order_statistics` | C3 주문 통계 항목·뜻·계산·null 처리 |
| [scm/harness.py](../scm/harness.py) | `MESSAGE_SCHEMA`, `PLAN_FIELDS`, `plan_schema`, `_validate_message` | 메시지 JSON 형식과 추가 내용 검사 |
| [scm/harness.py](../scm/harness.py) | `Harness.decide` | C 구성, T 처리, 최종 발주, 확정 후 메시지 생성 및 전달용 값 구성 |
| [scm/prompts.py](../scm/prompts.py) | `message_instructions` | 송신자가 M2·M3·M4에 무엇을 쓸 수 있는지 지시 |
| [scm/prompts.py](../scm/prompts.py) | `selection_instructions`, `order_instructions` | 수신자가 M을 도구 선택과 최종 판단에서 어떻게 해석할지 지시 |
| [scm/provider.py](../scm/provider.py) | `validate_schema`, `OpenRouterProvider.ask` | JSON 검사, 추가 검증 함수 실행, 공통 재시도 및 호출 기록 |
| [scm/provider.py](../scm/provider.py) | `MockProvider.ask` | API 없이 실행하는 테스트용 응답 생성 |
| [scm/runner.py](../scm/runner.py) | `run_episode` | 같은 기간에 하류 메시지를 다음 상류에게 전달하고 중간 상태 저장 |
| [scm/registry.py](../scm/registry.py) | `Condition`, `registry`, `get_condition` | 실행할 수 있는 조건 이름과 C·M·T 배정 |
| [scm/config.py](../scm/config.py) | `SimulationConfig`, `APIConfig` | 수량 상한, API 모델·단가·재시도·출력 토큰 제한 |
| [configs/registered_conditions.json](../configs/registered_conditions.json) | 전체 파일 | 등록 조건의 공유용 목록. 실행 시 실제로 읽는 설정 파일은 아님 |

M3는 이미 정수형입니다. 이후 필드·기간·허용 범위를 변경할 때도 **프롬프트 한 곳만 바꾸면 안 됩니다.** 스키마, 검사, 모의 응답, 수신 측 해석, 테스트를 함께 검토해야 합니다.

## 3. C3를 바꾸는 방법과 비교의 의미

### 3.1 현재 C0와 C3의 차이

`build_C`는 당기 상태를 `current`에 항상 넣습니다. 여기의 `P_pre`는 입고량을 차감한 뒤, 자기 당기 발주를 더하기 **전**의 미입고 주문량입니다.

- C0: D·I·B·P_pre의 `t-9`부터 `t`까지 원자료와 `t-9`부터 `t-1`까지 과거 기준·최종 주문량 9개.
- C3: 당기 상태와 D·I·B·P_pre의 `t-9`부터 `t`까지 통계. 여기에 `t-9`부터 `t-1`까지 Q_base·Q_final 및 같은 기간의 차이 `Q_final−Q_base` 통계를 추가합니다. 과거 기간별 원자료 행은 제공하지 않습니다.

따라서 과거 주문 자료를 아예 제외하던 이전 C3와 달리, **현재 C3는 C0와 같은 원천 변수·기간을 사용하면서 통계 요약과 주문 차이의 파생 요약을 제공**합니다. `Q_final−Q_base`는 C0의 원자료에서도 계산할 수 있지만, C3는 그 계산 결과까지 환경이 요약해 줍니다. 통계에서는 시간적 순서와 개별 관측값도 사라집니다. 따라서 “정보가 완전히 같고 표시 형식만 바뀐다”거나 “순수한 표현 형식의 효과만 측정한다”고 설명하지 않습니다.

### 3.2 현재 통계 필드와 수정 위치

| 필드 | 현재 내용 | 수정할 위치 |
|---|---|---|
| `current` | 당기 `t`, `D`, `I`, `B`, `P_pre` | `build_C`의 공통 현재 값 구성 |
| `statistics` | 10기 D·I·B·P_pre 통계 | `build_C`의 C3 분기와 `_summary` |
| `order_statistics` | 과거 9기 Q_base·Q_final·Q_final−Q_base 통계 | `ORDER_STAT_FIELDS`, `_order_statistics` |
| `order_definitions` | 각 주문 통계가 뜻하는 바를 LLM에 전달 | `ORDER_STAT_DEFINITIONS` |
| `order_window`, `order_period_start`, `order_period_end` | 주문 자료 개수 9, 시작 `t-9`, 끝 `t-1` | `build_C`의 C3 분기 |

`_summary`는 각 계열의 `mean`, `standard_deviation`, `minimum`, `maximum`을 반환합니다. 차이는 같은 기간의 **실제 실행량 Q_final에서 보정된 기준량 Q_base를 뺀 부호 있는 값**입니다. 절댓값이나 보정 전 `_raw` 값이 아닙니다. 준비기간은 Q_base와 Q_final이 같으므로 해당 기간의 차이 0도 포함합니다.

과거 Q_base 중 하나라도 `null`이면 Q_base 통계와 차이 통계 전체가 `null`이고 Q_final 통계만 계산합니다. `null`을 0으로 바꾸거나 일부 기간만 골라 계산하지 않습니다. 현재 등록된 C3 조건에는 기준량이 존재하므로 이 처리는 정의 확장 시에도 의미를 유지하기 위한 방어 규칙입니다.

### 3.3 코드에서 주의할 점

1. `build_C`의 C3 분기를 수정합니다. 공통 `STATE_FIELDS`를 바꾸면 C0부터 C2까지의 정보까지 바뀌므로, C3만 변경하려는 경우 공통 변수 변경을 피합니다.
2. C3의 통계는 현재 당기를 포함하고 `statistics.stdev`의 **표본 표준편차**를 사용합니다. 원하는 기간·통계 정의와 실제 코드가 일치하는지 확인합니다.
3. C3에는 `window`, `period_start`, `period_end`에 10기 정의가 직접 들어 있습니다. 기간을 바꾸면 `WINDOWS` 한 곳만 바꿔서는 부족합니다.
4. C의 변경은 최종 주문 호출뿐 아니라 **T3/T4의 방법 선택 호출과 M2/M3/M4 생성 호출의 C 입력**에도 반영됩니다. 특정 단계에서만 다른 C를 보여주려면 그것도 별도의 실험 설계입니다.
5. T의 수주 이력은 C와 별개인 고정 30기입니다. C3를 수정한다고 `history_30`을 C에 넣거나 예측 창을 바꾸지 않습니다.
6. 주문 통계에는 당기 미확정 Q를 포함하지 않습니다. `t-1`까지만 제공하고 `Q_base=null`은 0으로 바꾸지 않습니다. 독립 A는 C0를 쓰며 본 실험 기간의 기준 주문량이 존재하지 않습니다.
7. 주문 통계 항목을 변경할 때 `ORDER_STAT_FIELDS`, `ORDER_STAT_DEFINITIONS`, `_order_statistics`의 계산을 함께 맞춥니다. 평균·표준편차 등 공통 통계 종류를 바꾸면 `_summary`를 사용하는 상태 통계와 주문 통계가 모두 바뀝니다.

## 4. M2를 바꾸는 방법

### 4.1 현재 자유문장 방식

현재 M2 출력은 다음 구조입니다.

```json
{"message": "The current order accounts for outstanding replenishment."}
```

`message_instructions`는 현재 주문의 정성적 근거만 허용합니다. `_validate_message`는 빈 문자열, 숫자 문자, 일부 영어 미래 행동 표현을 검사합니다. **검사가 자연어 의미 전체를 이해하는 것은 아닙니다.** 숫자를 단어로 쓰거나 다른 표현으로 계획을 전달하는 경우까지 완전히 차단하지는 못합니다. 또한 최종 판단 뒤에 작성하는 설명이므로, 실제 판단 이유를 정확하게 증명하는 자료로 보아서는 안 됩니다.

자유문장을 유지한다면 지시 문구를 다듬고, 원문을 보존하며, 별도의 표본 검토로 M2와 M3의 내용이 얼마나 잘 분리됐는지 확인하는 방법이 있습니다. 검토 결과는 측정하되 실행 중 임의로 문장을 재작성하면 추가 하네스 처리가 됩니다.

### 4.2 이유 항목 선택형으로 바꾸는 대안

정해진 이유 코드 중 LLM이 한두 개를 고르는 형식은 내용 범위를 더 좁힐 수 있습니다. 다음은 **미구현 예시**입니다. 코드 목록은 연구자가 먼저 확정해야 합니다.

```json
{"reason_codes": ["account_for_outstanding_supply", "limit_order_variation"]}
```

가능한 수정 순서는 다음과 같습니다.

1. M2 전용 스키마를 만듭니다. M4도 사용하는 `MESSAGE_SCHEMA` 자체를 바꾸면 M4가 함께 바뀌므로 분리합니다.
2. 코드별 의미를 `message_instructions`에 명시하고, 선택 가능한 개수와 중복 허용 여부를 정합니다.
3. `_validate_message`에서 허용 코드, 개수, 중복을 검사합니다. 현재의 문자열 `.strip()` 검사와 숫자 정규식은 이 형식에 그대로 쓸 수 없습니다.
4. `Harness.decide`의 메시지 스키마 선택과 전달용 `outgoing` 구성을 바꿉니다. 현재 M2는 `output["message"]`만 꺼내므로 `reason_codes`로 바꾸면 이 부분도 수정해야 합니다.
5. 상류의 방법 선택·최종 주문 프롬프트에도 각 코드의 의미를 알려줍니다. 코드명을 영문으로 정하는 것만으로 의미가 충분히 전달된다고 가정하지 않습니다.
6. `MockProvider.ask`가 M2에서 새 형식의 응답을 만들도록 바꾸고 관련 테스트를 수정합니다.

현재 로컬 `validate_schema`는 배열의 각 원소 형식과 `enum`은 검사하지만 `minItems`, `maxItems`, `uniqueItems`는 검사하지 않습니다. 스키마에 이러한 키를 넣기만 하지 말고, 검증기를 확장하거나 M2 전용 검사에서 실제로 강제해야 합니다.

이 대안에서는 자연어 설명의 풍부함이 줄고 전송 길이도 바뀝니다. 따라서 기존 자유문장 M2와 동일한 처리로 간주하지 않고 변경된 처리를 기록합니다. 이유 코드가 허용된 코드라는 사실과 그 이유가 실제 상태에 부합한다는 사실도 별개입니다.

## 5. 현재 수치형 M3와 수정 방법

### 5.1 현재 출력과 의미

현재 M3는 다음처럼 두 **정수**만 받습니다. 문자열 계획은 이전 버전의 정의입니다.

```json
{"plan_t_plus_1": 8, "plan_t_plus_2": 10}
```

두 값의 뜻은 **송신자 자신의 `t+1`, `t+2` 계획 발주량**입니다. 미래 수주량 D의 예측값이나 다음 기간의 기준 주문량 Q_base가 아닙니다.

LLM은 이미 확정한 당기 주문 뒤에 허용된 C·수신 M·당기 Q_base·Q_final·판단 이유·발주 후 P_post를 보고 계획량을 자율적으로 정합니다. 별도의 계획 계산 공식, 추가 30기 이력, 실제 미래 수요를 제공하지 않습니다. 환경은 형식과 수량 범위를 검사하며 계획의 최적성을 보장하지 않습니다.

### 5.2 현재 구현 위치와 변경 시 확인할 부분

| 순서 | 수정 지점 | 현재 동작과 확인할 내용 |
|---|---|---|
| 1 | `PLAN_FIELDS`, `plan_schema(order_cap)` | 정수 필드 두 개를 필수로 지정하고 추가 필드를 금지. 최소 0, 최대 설정 상한을 스키마에 포함 |
| 2 | `_validate_message(value, level, order_cap=None)` | M3 분기에서 `type(quantity) is int`와 실제 수량 범위를 검사. M3 검사에는 상한 전달이 필수 |
| 3 | `Harness.decide` | `self.config.order_cap`을 `plan_schema`, `message_instructions`, 추가 검증 함수에 함께 전달 |
| 4 | `message_instructions(role, level, order_cap)` | 두 값은 자기 미래 발주 계획이고 문장·이유·현재 상태 보고를 넣지 않도록 지시 |
| 5 | 수신 프롬프트 | `selection_instructions`, `order_instructions`가 두 수치의 기간·송신자·비구속적 의도를 설명. 보장된 수요로 해석하거나 당기 주문에 더하지 않도록 명시 |
| 6 | `MockProvider.ask` | 흐름 확인용으로 당기 Q_final을 두 계획량에 복사. 실제 LLM도 이 규칙을 쓰도록 강제하는 것은 아님 |
| 7 | 전달 및 기록 | `outgoing=deepcopy(output)`가 객체를 보존. 상류의 M·결과 행·체크포인트에도 두 정수를 전달·저장 |
| 8 | 테스트·문서 | 형식·범위·기간 정의를 변경하면 해당 검증과 처리 정의·출력 예시를 함께 갱신 |

현재 `plan_schema(100)`이 만드는 스키마는 아래와 같습니다. 실제 호출에는 고정 숫자 대신 `self.config.order_cap`을 전달합니다.

```json
{
  "type": "object",
  "properties": {
    "plan_t_plus_1": {"type": "integer", "minimum": 0, "maximum": 100},
    "plan_t_plus_2": {"type": "integer", "minimum": 0, "maximum": 100}
  },
  "required": ["plan_t_plus_1", "plan_t_plus_2"],
  "additionalProperties": false
}
```

**로컬 `validate_schema`는 `minimum`·`maximum`을 검사하지 않으므로 현재 `_validate_message`의 M3 분기가 범위를 직접 검사합니다.** 스키마만 고치고 이 검사를 제거하면 음수나 상한 초과를 차단하지 못합니다. 정수 검사에는 JSON `true`·`false`, 문자열 `"10"`, 소수 `10.5`와 `10.0` 모두 통과하지 못합니다. 0과 상한 자체는 허용합니다.

현재 계획량에는 `normalize_order`를 적용하지 않습니다. 잘못된 수량·형식은 **형식 실패로 기록하고 공통 재시도 1회**를 사용합니다. 재시도 후에도 실패하면 `outgoing_message=null`로 전달하며 이미 확정한 Q_final과 P_post는 유지합니다. 추후 계획량을 자동 반올림·상한 보정하도록 바꾸면 이는 별도의 처리 변경입니다.

### 5.3 계획을 전달하는 것과 실행하는 것은 다릅니다

- 자기 당기 Q_final을 확정한 뒤 계획 메시지를 만듭니다. 그 계획은 **같은 기간에 바로 다음 상류 에이전트**가 받습니다.
- 수신자는 T3/T4 방법 선택과 최종 주문 판단에서 해당 M을 사용할 수 있습니다. 메시지를 생성한 송신자는 이미 당기 주문을 끝냈습니다.
- 다음 기간의 실제 발주량은 새로운 C·M·T 정보로 다시 결정합니다. M3 계획은 자동 주문으로 실행되지 않습니다.
- 과거에 자신이 보낸 계획은 로그에 보존되지만, 현재 `build_C`가 다음 기간 자신의 프롬프트에 다시 넣지 않습니다.
- 이전 계획을 자신의 기억에 추가하거나 계획 준수를 강제하면 정보 제공·행동 제약도 함께 바뀝니다. 단순 M3 출력 형식 변경과 구분해 별도 설계로 다룹니다.
- 메시지에는 송신자의 두 계획량만 들어가더라도 결과 행의 `t`와 `agent`로 대상을 식별할 수 있습니다. 수신 프롬프트에 현재 M이 당기 즉시 하류가 보낸 것임을 명확히 설명합니다.

선택적인 추가 평가지표는 계획과 이후 실제 발주의 차이입니다. 예를 들어 `t`에 보낸 `plan_t_plus_1`과 **같은 송신자**의 `t+1` Q_final을 비교합니다. 평가 종료 뒤 실현값이 없는 계획은 누락으로 처리하고 0으로 채우지 않습니다. 현재 요약 지표에 이 계획 오차는 구현되어 있지 않습니다.

## 6. 공통으로 유지할 정보 경계와 실패 처리

1. 하류 → 상류의 한 방향 흐름을 유지합니다. `run_episode`는 `outgoing_message`를 같은 기간의 다음 에이전트에게 넘깁니다. 소매상은 공급망 내 하류 메시지가 없고, 공장은 메시지를 보낼 추가 상류 에이전트가 없습니다.
2. 모든 조건에서 하류의 실제 당기 발주량은 상류의 수주량으로 별도 전달됩니다. M0라고 수주량까지 없어지는 것은 아닙니다.
3. 메시지 생성에 제공하는 정보는 `C`, 받은 `M`, `Q_base`, `Q_final`, `rationale`, `P_post`입니다. 전체 환경 이력이나 별도의 수주 30기를 추가하지 않습니다.
4. M1은 환경이 계산한 확정 상태와 통계를 전달하는 현재 규칙을 유지합니다. M2/M3 형식을 바꾸는 과정에서 M1을 LLM 생성으로 변경하지 않습니다.
5. 형식 오류와 추가 내용 검사 실패는 `OpenRouterProvider.ask`의 **같은 1회 재시도 예산**을 사용합니다. 검사를 추가했다고 검사별 재시도를 별도로 늘리지 않습니다.
6. 메시지 호출이 재시도 후에도 실패하면 빈 추가정보(`None`/JSON `null`)로 전달합니다. 이미 확정한 Q_final·P_post를 되돌리거나 주문 호출을 다시 수행하지 않습니다. 메시지 실패·재시도 기록은 남깁니다.
7. 현재 모든 LLM 호출의 출력 상한은 300토큰입니다. 형식 변경과 함께 이 상한을 바꾸면 G 설정도 달라지므로 따로 기록합니다.

## 7. 변경 후 확인할 테스트

테스트는 프로젝트 최상위 폴더에서 실행합니다. 아래 명령은 실제 모델 API를 호출하지 않습니다.

```powershell
python -m unittest discover -s tests -v
```

기존 테스트를 실제 새 정의에 맞게 수정하고, 새 경계·오류 사례를 추가합니다. 통과시키기 위해 정보 경계 확인을 삭제하지 않습니다.

| 테스트 파일 | 현재 테스트 이름 | 변경 시 유지할 확인 |
|---|---|---|
| [tests/test_harness.py](../tests/test_harness.py) | `test_summary_gives_order_statistics_without_raw_rows` | C3 상태 10기·주문 9기, 표본 SD, 현재 값, 원자료 행 제외 및 준비기간 차이 0 |
| [tests/test_harness.py](../tests/test_harness.py) | `test_summary_order_statistics_use_executed_signed_gaps` | 실제 실행량과 기준량의 부호 있는 차이, Q_base가 없는 경우의 null 처리 |
| [tests/test_harness.py](../tests/test_harness.py) | `test_raw_context_levels_are_unchanged_by_c3_order_statistics` | C3 주문 통계 변경이 C0·C1·C2에 추가 정보를 섞지 않음 |
| [tests/test_harness.py](../tests/test_harness.py) | `test_context_windows_and_pre_order_timestamps` | 원자료 창과 P_pre의 측정 시점, 과거 주문은 t-1까지만 포함 |
| [tests/test_harness.py](../tests/test_harness.py) | `test_generated_messages_use_only_permitted_information_after_order` | 새 출력 형식, 허용된 입력만 사용, 발주 후 P_post, 당기 주문 확정 후 생성 |
| [tests/test_harness.py](../tests/test_harness.py) | `test_m2_semantic_checks_are_narrow` | 현행 문장 검사의 범위 또는 새 코드 목록·개수·중복 검사 |
| [tests/test_harness.py](../tests/test_harness.py) | `test_message_failure_preserves_committed_order` | M2/M3에도 잘못된 출력과 재시도 소진 사례 추가. 당기 발주 유지 확인 |
| [tests/test_harness.py](../tests/test_harness.py) | `test_m3_accepts_only_integer_plans_within_order_cap` | 정수 경계값 허용, 잘못된 자료형·범위·누락·추가 필드 거절 |
| [tests/test_harness.py](../tests/test_harness.py) | `test_m3_numeric_plan_reaches_upstream_selection_and_order` | 송신 계획이 상류 선택·최종 주문 호출에 같은 값으로 전달됨 |
| [tests/test_harness.py](../tests/test_harness.py) | `test_m3_invalid_plan_exhausts_to_empty_message_but_keeps_order` | 메시지 실패 시 null 대체 및 확정한 당기 발주 유지 |
| [tests/test_harness.py](../tests/test_harness.py) | `test_factory_does_not_generate_a_message_to_nonexistent_agent` | 마지막 공장에는 메시지 생성 호출이 없고, 소매상부터 같은 기간에 전달되는지 확인 |
| [tests/test_provider.py](../tests/test_provider.py) | `test_semantic_failure_shares_retry_budget`, `test_schema_retry_accounted_and_journal_reused` | 새 형식 오류도 재시도 1회와 형식 실패 집계에 포함되는지 확인 |
| [tests/test_provider.py](../tests/test_provider.py) | `test_m3_out_of_range_plan_shares_retry_budget_then_succeeds` | 범위 오류가 실제 Provider 검증 경로에서 공통 재시도를 사용하고 수정 응답을 수용 |
| [tests/test_integration.py](../tests/test_integration.py) | `test_all_103_registered_conditions_complete_sequentially` | 현재 등록 조건 모두 모의 실행. 조건을 추가하면 개수·구성 기대값도 변경 |
| [tests/test_integration.py](../tests/test_integration.py) | `test_explicit_batch_resume_and_warmup_pairing` | 동일 설정 재개, 동일 seed 준비기간 복제, 다른 실행 서명 거부 |

M3를 다시 수정할 때도 0·상한·음수·상한 초과·소수·문자열·불리언·필드 누락·추가 필드와 **선택 호출(T3/T4)·주문 호출에 대한 전달**을 점검합니다. 현재 상류 전달 테스트는 T3 사례를 사용하며 T4에도 같은 M 입력 경로가 적용됩니다.

모의 실행 예시는 다음과 같습니다. 출력 폴더는 새로 지정합니다.

```powershell
python -m scm run --conditions C3M0T2 C0M2T3 C0M3T4 --seeds 123 --backend mock --evaluation-periods 2 --output results/design_check_v2
```

`mock` 결과는 정보 전달과 코드 흐름을 확인하는 자료입니다. LLM 성능, 메시지의 의미 품질, 개선의 통계적 유의성을 보여주는 결과로 사용하지 않습니다. 실제 API 실험은 설계와 분석 계획을 정한 다음 별도로 실행합니다.

## 8. 등록·문서·기존 결과와의 호환성

### 등록 목록 갱신

C3의 내부 정의만 변경하면 이름과 103조건 수를 유지할 수 있지만, **이전 버전의 C3와 다른 처리**가 되므로 프로토콜 버전과 변경 내용을 남겨야 합니다. 보조조건이나 C 수준을 추가한다면 아래를 함께 확인합니다.

- `registry()`의 조건 배정, `get_condition()`의 안내 문구.
- `WINDOWS`와 `build_C`의 해당 처리. 새 이름을 만들기만 해서는 실행되지 않습니다.
- `scm/__main__.py`의 목록 안내·예상 호출 수 계산, 해당 CLI 테스트.
- `configs/registered_conditions.json`, README의 조건 수·정의, 통합 테스트의 개수·구성.

현재 JSON 목록은 아래 명령으로 코드의 실제 등록과 맞춰 다시 만들 수 있습니다. 프로젝트 최상위 폴더에서 실행하며, **이 명령은 해당 목록 파일을 덮어씁니다.**

```powershell
python -c "import json; from pathlib import Path; from dataclasses import asdict; from scm.registry import registry; Path('configs/registered_conditions.json').write_text(json.dumps([asdict(c) for c in registry().values()], ensure_ascii=False, indent=2) + '\n', encoding='utf-8')"
```

과거 문서·감사 기록은 그 당시 정의를 담을 수 있습니다. [과거 예비실험 계획](pilot_plan.json)을 현재 설계로 조용히 덮어쓰기보다, 새 변경 기록에 적용 시점·조건 정의·코드 버전과 해당 문서의 관계를 남깁니다. 원래 작업 환경의 `docs/agreed_specification.json`은 로컬 설계 기록으로 보존되며 GitHub 공유에는 포함하지 않습니다. 공유된 현재 정의는 README와 이 설명서를 기준으로 확인합니다.

### 소스 변경 후에는 새 출력 폴더 사용

`source_fingerprint()`는 `scm/` 바로 아래 모든 `.py` 파일의 내용을 해시합니다. 따라서 계산 코드뿐 아니라 **프롬프트나 이 파일들의 주석만 바꿔도** 소스 해시가 바뀔 수 있습니다. 실행 서명에는 코드, 조건, seed, 시뮬레이션·API 설정, backend가 포함됩니다.

기존 결과 폴더에서 `--resume`은 **같은 실행을 이어 가는 기능**입니다. 다른 C3/M2/M3 정의를 기존 실험 중간에 섞는 기능이 아닙니다. 소스나 설정을 바꾼 뒤에는 새 출력 폴더를 사용합니다. 기존 manifest·체크포인트의 해시를 직접 고쳐 검사를 우회하지 않습니다.

권장 협업 순서는 다음과 같습니다.

1. 변경할 처리와 유지할 정보 범위를 문장·예시 JSON으로 합의합니다.
2. 별도 Git 브랜치에서 코드·프롬프트·검사·MockProvider·테스트를 함께 수정합니다.
3. 오프라인 테스트와 소규모 모의 실행으로 실제 전송 payload를 확인합니다.
4. 변경 전후 정의, 이유, 적용 조건, 커밋, 검증 결과를 새 변경 기록에 남깁니다.
5. 새 출력 폴더에 명시적으로 선택한 조건과 seed를 실행합니다. 비교하려는 조건은 동일한 외부 수요 경로와 공통 준비기간을 사용합니다.
6. 실제 모델의 메시지 품질과 실패율을 점검한 뒤, 사전에 정한 반복수·분석 계획에 따라 본 실험을 수행합니다.

동일 수요 seed는 같은 외부 고객 수요를 생성하는 장치입니다. **새로 API를 호출하는 LLM의 발주·메시지까지 동일하게 만드는 보장은 아닙니다.** 하류 발주가 달라지면 상류의 수주 이력도 달라집니다. 반면 같은 실행을 `--resume`하며 이미 성공한 호출을 저널에서 재사용하는 것은 저장된 응답을 읽는 동작이므로, 새 API 호출의 결정성을 증명하지 않습니다.

### 과거 예비실험과 새 설계 분석

과거 예비실험의 A는 예측과 Order-up-to 정책이 포함된 조건이었습니다. 현재 독립 A는 예측·기준 주문량 없이 최종 주문을 판단합니다. 이번 C3 주문 통계와 M3 정수형 계획은 이전 C3·M3와 다른 처리이며, M2는 기존 정의를 유지합니다. 공통 선택·최종 주문 프롬프트의 M3 설명도 변경되므로 M3 이외 조건에도 프롬프트 변경이 적용됩니다.

현재 `python -m scm analyze`는 **고정된 과거 4조건 × 8개 seed 예비실험 전용**이며 당시 소스와 계획을 검사합니다. 새 103조건이나 새 C3/M2/M3 결과를 위한 일반 요인실험 분석기가 아닙니다. 새로운 결과에는 별도의 분석 계획과 분석 코드를 마련해야 하며, 기존 검사에서 요구하는 소스 해시를 변경해서 새 결과를 통과시키지 않습니다.
