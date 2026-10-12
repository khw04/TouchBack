---
name: auto-calibration
description: 개발용 표시 단계와 관측으로 보정 후보를 생성한다.
---

# auto-calibration

실행 진입점: `fit_candidate 및 app.calibration.cli`. 코드 위치는 backend/app/calibration/fitting.py, backend/app/calibration/cli.py, backend/app/observations/state.py다.

독립 표시 단계·개발 split·설치/기종/냄비 조건이 필요하다. 평가 데이터·품질 이상·구간 겹침은 후보 생성을 거부한다. 실제 후보는 draft이며 자동 활성화하지 않는다.

현재 구현·실행 검증은 Mock 범위다. 실제 장비 보정/정확도/본선 목표 완료와 LLM Tool 등록은 미완료다. 인덕션을 자동 조작하지 않는다.
입력·출력·명령·운영 기본값은 [보정과상태](../../docs/보정과상태.md)를 따른다. 독립 평가 정답과 개발 자료는 Agent에 전달하지 않는다.
검증은 backend 폴더에서 `..\.venv\Scripts\python.exe -m pytest -q tests/test_calibration_state.py`로 실행한다.
