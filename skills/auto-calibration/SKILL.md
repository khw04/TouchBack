---
name: auto-calibration
description: 개발용 표시 단계와 관측으로 보정 후보를 생성한다.
---

# auto-calibration

실행 진입점: `fit_candidate 및 app.calibration.cli`. 코드 위치는 backend/app/calibration/fitting.py, backend/app/calibration/cli.py, backend/app/observations/state.py다.

독립 표시 단계·개발 split·설치/기종/냄비 조건이 필요하다. 평가 데이터·품질 이상·구간 겹침은 후보 생성을 거부한다. 실제 후보는 draft이며 자동 활성화하지 않는다.

현재 구현·실행 검증은 Mock 범위다. 실제 장비 보정/정확도/본선 목표 완료는 미완료다. 인덕션을 자동 조작하지 않는다.
입력·출력·명령·운영 기본값은 [보정과상태](../../docs/보정과상태.md)를 따른다. 독립 평가 정답과 개발 자료는 Agent에 전달하지 않는다.
검증은 backend 폴더에서 `..\.venv\Scripts\python.exe -m pytest -q tests/test_calibration_state.py`로 실행한다.

가상 시연의 Agent Tool `auto_calibration`는 backend/app/tools/mock_state_tools.py에서 실행한다. 명시적 합성 개발 표본과 fit_candidate로 생성한 Mock 프로필 및 유효한 수신 기준만 사용한다. 입력 인수는 없으며 source=mock과 좌우 상태/관측 ID를 반환한다. 불량·낡은·누락 관측은 실패하며 실측 정답/내부 가상 화력은 모델에 넘기지 않는다.
