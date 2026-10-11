# 설치와 실행 안내

이 문서는 새 공동연구자가 저장소를 내려받아 현재 코드를 실행하는 순서입니다. 시스템 정의는 [시스템 설명](SYSTEM_GUIDE.md), C3·M2·M3의 변경 지점은 [수정 가이드](MODIFICATION_GUIDE.md)를 참고하세요.

## 1. 코드와 Python 준비

Python 3.11 이상과 Git이 필요합니다. GitHub의 Code → Download ZIP으로 내려받으면 Git 없이도 실행할 수 있습니다. 실행과 테스트는 Python 표준 라이브러리만 사용하므로 외부 패키지 설치가 필요하지 않습니다.

```powershell
git clone https://github.com/doeun6/new_LLM-SCM-Agent.git
cd new_LLM-SCM-Agent
python --version
python -m scm list
```

기존 작업 폴더 이름이 `SCM_LLM_Agent`여도 동작은 같습니다. 모든 명령은 `scm/`, `tests/`, `README.md`가 있는 저장소 최상위 폴더에서 실행합니다. `python` 명령을 찾지 못하면 Python 설치와 PATH를 확인하거나, 설치되어 있는 Windows Python Launcher의 `py -3`를 대신 사용할 수 있습니다.

`python -m scm list`는 등록된 조건을 출력하며 API 키와 네트워크가 필요하지 않습니다. 현재 조건은 전체 C4×M5×T5=100조합, 별도 A, D_cost, D_bullwhip의 총 103개입니다. 파일을 열거나 실행 버튼만 눌러 전체 실험이 시작되지는 않습니다.

## 2. 먼저 API 없는 점검

```powershell
python -m unittest discover -s tests -v
python -m scm run --conditions A C0M0T0 C3M1T2 --seeds 123 --backend mock --evaluation-periods 2 --output results/local_check
python status.py results/local_check
```

첫 명령은 테스트를 실행합니다. 두 번째 명령은 공통 준비기간 30기 이후 평가 2기를 모의 공급자로 실행하며 3개 조건의 결과 파일을 만듭니다. `mock`은 물류·정보 흐름·저장 동작을 확인하기 위한 규칙적 가짜 응답입니다. **LLM 성능 결과로 해석하지 않습니다.** 정식 평가기간은 기본 100기이고 2기는 소프트웨어 점검용입니다.

예시 seed 123은 자유롭게 바꿀 수 있으며 코드에 연구용 seed 범위를 고정하지 않았습니다. 같은 폴더에 명령을 다시 실행하면 기존 배치가 있다는 오류가 납니다. 새 점검에는 새 출력 폴더를, 동일 실행 재개에는 `--resume`을 사용합니다.

## 3. API 키 설정

실제 호출은 OpenRouter를 사용합니다. 코드의 환경변수 이름은 `OPENROUTER_API_KEY`입니다. Gemini API에 직접 연결하는 방식이 아니므로 해당 변수에는 OpenRouter 키를 설정합니다.

PowerShell에서 키가 화면과 명령 기록에 직접 나타나지 않도록 입력하려면 다음을 실행하세요.

```powershell
$scmSecret = Read-Host 'OpenRouter API key' -AsSecureString
$env:OPENROUTER_API_KEY = [System.Net.NetworkCredential]::new('', $scmSecret).Password
Remove-Variable scmSecret
```

현재 터미널 세션과 그 터미널에서 실행하는 Python에 적용됩니다. 키를 소스·README·결과 파일에 붙여 넣지 않습니다. `.env` 파일을 자동으로 읽는 기능은 없으므로 `.env`에만 적으면 연결되지 않습니다. API 키 자체는 코드의 결과 기록에 저장하지 않습니다.

현재 모델·라우트 설정은 [APIConfig](../scm/config.py)에 있습니다: `google/gemini-2.5-flash-lite`, `google-ai-studio/flex`, `service_tier=flex`, temperature 0.1, 출력 최대 300토큰, 재시도 최대 1회입니다. 이것은 코드의 고정 설정이며 향후 서비스 제공 여부나 요금의 보장은 아닙니다. 다른 모델로 바꾸는 경우 공급자 응답 검증도 함께 확인해야 합니다.

## 4. 실행 전 조건과 예상 호출 수 확인

```powershell
python -m scm run --conditions A C0M0T0 C3M1T2 --seeds 123 --backend openrouter --objective cost --output results/cost_seed123 --budget-usd 1 --dry-run
```

`--dry-run`은 실제 API 요청이나 실험을 수행하지 않고 조건·목적·seed·예상 호출 수를 출력합니다. API 연결 성공을 확인하는 명령은 아닙니다. 출력의 비용 추정치는 호출당 입력 2,000토큰·출력 200토큰 및 코드에 적힌 단가를 가정한 값으로, 실제 프롬프트 길이를 측정한 견적이 아닙니다.

재시도 없는 100기·4단계·seed 하나 기준, A는 주문 호출 400회이고 C0M0T0는 예측+주문 800회입니다. M2/M3/M4는 내부 연결 세 곳의 메시지 작성 호출이 기간마다 추가됩니다. 모든 조건의 준비기간 30기는 API 호출이 없습니다.

## 5. 원하는 조합과 목적만 실행

다음 명령부터는 실제 API를 사용하며 비용이 발생합니다.

```powershell
# 한 조건, 한 seed, 비용 감소 목적
python -m scm run --conditions C3M1T2 --seeds 123 --backend openrouter --objective cost --output results/cost_c3m1t2_seed123 --budget-usd 1

# 같은 외부 고객 수요로 여러 조건 비교
python -m scm run --conditions A C0M0T0 C3M1T2 --seeds 123 456 --backend openrouter --objective cost --workers 2 --output results/cost_comparison --budget-usd 2

# 채찍효과 완화 목적
python -m scm run --conditions A C3M1T2 --seeds 123 --backend openrouter --objective bullwhip --output results/bullwhip_comparison --budget-usd 1

# D 두 조건은 각자의 고정 목적을 사용하므로 objective를 생략
python -m scm run --conditions D_cost D_bullwhip --seeds 123 --backend openrouter --output results/d_references --budget-usd 1
```

| 인자 | 의미 |
|---|---|
| `--conditions` | 실행할 조건 이름. 여러 개를 공백으로 구분. A는 C0M0T0와 별개 |
| `--seeds` | 외부 고객 수요 생성에 사용할 정수 seed. 여러 개를 공백으로 구분 |
| `--backend` | `mock`은 API 없는 모의 응답, `openrouter`는 실제 호출 |
| `--objective` | A·CMT 조건에서 `cost` 또는 `bullwhip`; 생략 시 cost. D는 이름대로 고정 |
| `--output` | 배치 결과 폴더. 코드·설정·조건·목적·seed를 바꿀 때 새 폴더 사용 |
| `--workers` | 서로 다른 조건×seed 실행의 병렬 개수, 기본 1, 허용 1부터 16까지. 한 실행 내부의 에이전트 순서는 유지 |
| `--budget-usd` | 배치 전체가 공유하는 API 예산. Python CLI 기본 8달러이며 예시에서는 직접 지정 |
| `--evaluation-periods` | 기본 100. 다른 값은 점검용이며 정식 100기 성능 결과와 분리 |
| `--resume` | 같은 배치의 저장된 결과·응답·체크포인트로 재개 |
| `--dry-run` | 실행 없이 선택 내용과 단순 호출·비용 추정 확인 |

예산은 각 seed나 각 조건에 따로 부여되는 것이 아닙니다. 기록된 비용과 진행 중 호출 예약액을 합산해 새 요청을 제어합니다. 실제 응답 비용이나 미보고 비용의 추정은 오차가 있을 수 있으므로 외부 결제액을 정확히 보장하는 상한으로 해석하지 않습니다. 예산 제한으로 중단되면 미완료로 기록합니다.

Windows 실행 스크립트도 같은 기능을 제공합니다. 이 스크립트의 기본 예산은 2달러이고, 배치 목적은 `-Objective`로 지정합니다.

```powershell
.\Run-Experiment.ps1 -Conditions A,C3M1T2 -Seeds 123,456 -Backend openrouter -Objective cost -BudgetUsd 2 -Workers 2 -Output results/ps_comparison -DryRun
```

실제 실행 시 `-DryRun`을 빼면 됩니다. PowerShell 스크립트 실행이 컴퓨터 정책으로 막혀 있으면 위의 `python -m scm ...` 명령으로 실행할 수 있습니다. Python 프로그램의 `--evaluation-periods`는 현재 PowerShell 스크립트의 별도 옵션으로 노출되어 있지 않습니다.

### 전체 103조건을 선택하여 실행

PowerShell에서 저장소의 등록 목록을 읽어 모든 조건을 명시적으로 선택할 수 있습니다. 현재 등록 목록은 `registry.py`와 일치하는지 테스트로 확인합니다. 조건을 추가·삭제했다면 [수정 가이드](MODIFICATION_GUIDE.md)의 목록 갱신 절차도 수행해야 합니다.

```powershell
$scmConditions = @((Get-Content .\configs\registered_conditions.json -Raw | ConvertFrom-Json).id)
$scmConditions.Count  # 현재 103

# API 없는 확인
.\Run-Experiment.ps1 -Conditions $scmConditions -Seeds 123 -Backend openrouter -BudgetUsd 20 -Output results/all_seed123 -DryRun

# 실제 API 실행
.\Run-Experiment.ps1 -Conditions $scmConditions -Seeds 123 -Backend openrouter -BudgetUsd 20 -Output results/all_seed123
```

seed 1개는 103회, seed 2개는 206회입니다. 각 회는 공통 준비기간 30기와 평가기간 100기를 포함하며, 준비기간에는 API 호출이 없습니다. `-Seeds 123,456`처럼 바꿔 여러 seed를 선택할 수 있습니다. 예시의 20달러는 **전체 배치에 지정한 예산 상한이며 예상 청구액이 아닙니다.** 자신의 예산으로 바꿉니다. 목적이 섞인 이 배치에는 `-Objective`를 지정하지 않습니다. A와 일반 100조합의 기본 목적은 cost이고, D 두 조건은 각자의 고정 목적을 사용합니다.

**A·일반 100조합을 모두 bullwhip 목적으로 실행**하려면 아래처럼 D를 제외한 101조건과 D 두 조건을 분리합니다. 예시 명령은 우선 `-DryRun`으로 확인하고, 실제 실행 시 해당 옵션을 제거합니다.

```powershell
$scmMainConditions = @($scmConditions | Where-Object { $_ -ne 'D_cost' -and $_ -ne 'D_bullwhip' })
.\Run-Experiment.ps1 -Conditions $scmMainConditions -Seeds 123 -Backend openrouter -Objective bullwhip -BudgetUsd 20 -Output results/all_bullwhip_seed123 -DryRun
.\Run-Experiment.ps1 -Conditions D_cost,D_bullwhip -Seeds 123 -Backend openrouter -BudgetUsd 1 -Output results/d_seed123 -DryRun
```

분리한 두 명령은 서로 다른 배치이므로 예산도 각각 적용됩니다. D_cost의 목적까지 bullwhip으로 바꾸는 것은 현재 등록 규칙에서 허용하지 않습니다. `-Workers`를 생략하면 한 번에 한 조건×seed를 실행합니다. 진행은 터미널에 완료된 회차별로 표시됩니다.

## 6. 결과 읽기

```powershell
python status.py results/cost_comparison
```

이 명령은 완료된 실행의 결과만 읽습니다. `completed` 목록에서 조건·seed별 전체 공급망 누적 비용 `total_cost`, 기간당 평균 비용 `cost_per_period`, 평가기간 수 `periods_evaluated`를 확인합니다. 이전 요약 파일에 해당 값이 없으면 0으로 대신 표시하지 않고 `null`로 표시합니다. API 비용 합계는 별도의 `completed_episodes_reported_cost_usd`입니다. 실행 중인 에피소드의 실시간 진행률과 지출을 포함하지 않으며, 전체 완료 여부는 `batch_status.json`을 함께 확인합니다.

```text
results/<배치>/
  batch_manifest.json       선택한 조건·설정·소스 해시
  batch_status.json         전체 완료 여부·완료 개수·오류
  results.json              완료된 실행들의 요약 목록
  warmup/                   seed별 공통 준비기간 상태와 수요
  episodes/<조건>__<목적>__seed-<값>/
    manifest.json           실행 설정
    api_calls.jsonl         실제 호출별 입력·출력·사용량·오류
    checkpoint.json        마지막으로 저장한 진행 상태
    periods.json/csv        평가기간 상태·결정·메시지·진단
    summary.json           완료된 평가기간의 성과지표
    failure.json           중단이 발생한 경우의 이력
```

**전체 공급망 누적 비용 `total_cost`를 주 지표 `cost_per_period`와 함께 확인합니다.** `total_cost`는 한 조건·seed에서 평가기간 동안 네 단계의 재고 유지비·백로그 비용·수량 비례 발주비·고정 발주비를 모두 합한 값입니다. 기본 평가기간은 31기부터 130기까지 100기이며 준비기간 비용은 제외합니다. `cost_per_period = total_cost / periods_evaluated`이므로, 기본 설정에서 누적 비용 12,000은 기간당 비용 120에 해당합니다. 두 지표는 각 `summary.json`의 `metrics` 안에 있고 배치 `results.json`에도 저장됩니다. 평가기간이 동일하면 두 지표의 순위도 같습니다.

공급망 운영비용과 API 달러 비용은 따로 해석합니다. 충족률·채찍효과·토큰·지연·실패 지표의 정의는 [시스템 설명](SYSTEM_GUIDE.md)을 참고하세요. 보정 전후 수량은 [README의 raw 용어](../README.md#결과-파일의-raw와-수량-용어)를 함께 확인합니다.

`summary.json`이 없거나 `complete`가 참이 아닌 실행은 완주 성능으로 취급하지 않습니다. 재개 후 완료됐더라도 이전 중단 기록인 `failure.json`은 남아 있을 수 있습니다.

## 7. 중단 후 재개와 반복 실험 구분

```powershell
python -m scm run --conditions C3M1T2 --seeds 123 --backend openrouter --objective cost --output results/cost_c3m1t2_seed123 --budget-usd 2 --resume
```

재개할 때 조건·seed·backend·목적·과학적 설정·소스가 원래 배치와 같아야 합니다. 예산 증액은 가능하며 변경 기록을 남깁니다. 현재 소스 해시는 `scm/*.py`의 파일 내용으로 계산되므로 주석이나 프롬프트 수정도 소스 변경으로 판정합니다. 변경 후에는 새 출력 폴더에서 시작합니다.

동일한 수요 seed를 사용하면 동일 코드·설정·환경에서 외부 고객 수요 경로와 준비기간 상태가 재현됩니다. **LLM은 같은 입력에도 다른 답을 낼 수 있어 새 실행의 최종 주문과 성과까지 동일하다고 보장할 수 없습니다.** 상류 수주도 하류의 실제 발주에 따라 달라집니다. 저장된 API 응답을 재사용하는 `--resume`은 새 API 호출로 같은 실험을 반복하는 것과 다릅니다.

## 8. 자주 만나는 문제

| 현상 | 확인 방법 |
|---|---|
| `No module named scm` | 현재 위치가 저장소 최상위인지 확인 |
| API key missing | Python을 실행하는 현재 터미널에 환경변수가 설정됐는지 확인 |
| 인증·결제·라우트 오류 | `failure.json` 확인. 다른 모델·일반 요금으로 자동 전환하지 않음 |
| Output folder belongs to another frozen batch | 다른 코드·설정의 출력 폴더와 충돌. 새 폴더 지정 |
| Batch exists | 동일 실행 재개는 `--resume`, 새 반복은 다른 출력 폴더 |
| Budget exceeded | 완료 여부를 확인한 뒤 같은 실행을 예산 증액하여 재개할 수 있음 |
| `.running.lock`이 남음 | 해당 실행 프로세스가 종료됐는지 먼저 확인. 실행 중인 프로세스의 잠금을 삭제하지 않음 |
| 새 결과를 `analyze`에 넣으면 거부됨 | 현재 analyze는 과거 4조건×8개 seed 예비실험 전용. 새 연구에는 새 사전 분석 계획 필요 |

GitHub에는 `results/`를 포함하지 않으므로 과거 자료를 대상으로 하는 재분석 명령은 별도로 보관된 원본 자료가 있어야 실행할 수 있습니다. 현재 103조건의 새 결과를 분석하려면 별도의 분석 계획과 분석 코드를 준비합니다.
