---
name: sensor-recalibration
description: 오류 뒤 기존 보정을 폐기하고 새 보정과 기준으로 재개한다.
---

# sensor-recalibration

실행 진입점: `ZoneState.invalidate → fit_candidate → activate_mock → observe`. 코드 위치는 backend/app/calibration/fitting.py, backend/app/calibration/cli.py, backend/app/observations/state.py다.

기존 숫자나 상태를 자동 수정하지 않는다. 새로운 후보와 유효 기준이 모두 필요하며 그 전에는 검증 결과를 uncertain으로 유지한다.

현재 구현·실행 검증은 Mock 범위다. 실제 장비 보정/정확도/본선 목표 완료와 LLM Tool 등록은 미완료다. 인덕션을 자동 조작하지 않는다.
입력·출력·명령·운영 기본값은 [보정과상태](../../docs/보정과상태.md)를 따른다. 독립 평가 정답과 개발 자료는 Agent에 전달하지 않는다.
검증은 backend 폴더에서 `..\.venv\Scripts\python.exe -m pytest -q tests/test_calibration_state.py`로 실행한다.
