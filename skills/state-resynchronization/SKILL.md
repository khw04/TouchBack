---
name: state-resynchronization
description: 유효한 새 관측으로 화구별 상태를 다시 확보한다.
---

# state-resynchronization

실행 진입점: `ZoneState.observe`. 코드 위치는 backend/app/calibration/fitting.py, backend/app/calibration/cli.py, backend/app/observations/state.py다.

Mock 후보를 activate_mock로 선택한 뒤 새 유효 기준을 수집한다. 품질/시계/세션/설정/신선함 문제가 있으면 기존 상태를 폐기하고 level=null을 반환한다.

현재 구현·실행 검증은 Mock 범위다. 실제 장비 보정/정확도/본선 목표 완료와 LLM Tool 등록은 미완료다. 인덕션을 자동 조작하지 않는다.
입력·출력·명령·운영 기본값은 [보정과상태](../../docs/보정과상태.md)를 따른다. 독립 평가 정답과 개발 자료는 Agent에 전달하지 않는다.
검증은 backend 폴더에서 `..\.venv\Scripts\python.exe -m pytest -q tests/test_calibration_state.py`로 실행한다.
