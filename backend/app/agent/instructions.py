"""Server-owned physical instructions for the validated P0 goal."""
from app.contracts.models import Goal


def instruction_text(goal: Goal, *, is_recovery: bool = False) -> str:
    if goal.direction not in ("increase","decrease") or goal.requested_steps != 1:
        raise ValueError("P0 조작 안내는 화력 한 단계 증가만 지원합니다.")
    zone_label = {"right": "오른쪽", "left": "왼쪽"}[goal.zone_id]
    press = "다시 한 번" if is_recovery else "한 번"
    button="+" if goal.direction=="increase" else "−"
    return f"{zone_label} 화구의 {button} 버튼을 {press} 눌러 주세요."
