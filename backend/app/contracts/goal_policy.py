"""Conservative P0 input gate shared by API and Agent; never infer other actions."""
import re

from app.contracts.models import Goal, Zone

# A complete, single power-increase request. Unknown clauses require clarification.
_PATTERN = re.compile(
    r"(?:인덕션\s*)?(?:(오른쪽|우측|왼쪽|좌측)\s*(?:화구(?:의)?\s*)?)?"
    r"(?:화력|출력)(?:을|를)?\s*(?:좀\s*)?"
    r"(?:(?:한\s*단계|한\s*단|1\s*단계|1\s*단)\s*)?"
    r"((?:올려(?:줘|주세요)?|높여(?:줘|주세요)?|증가(?:시켜(?:줘|주세요)?|시켜\s*주세요|해(?:줘|주세요)?)?|키워(?:줘|주세요)?)"
    r"|내려(?:줘|주세요)?|낮춰(?:줘|주세요)?|줄여(?:줘|주세요)?|감소시켜(?:줘|주세요)?)"
    r"[.!]?"
)


def interpret_p0_goal(user_input: str, selected_zone: Zone | None) -> tuple[Goal | None, str | None]:
    match = _PATTERN.fullmatch(user_input.strip())
    if not match:
        return None, "지원하지 않거나 준비되지 않은 목표입니다. 화력을 한 단계 올리거나 내리는 단일 요청인지 확인해 주세요."
    mentioned = match.group(1)
    zone = "right" if mentioned in ("오른쪽", "우측") else "left" if mentioned else None
    if zone and selected_zone and zone != selected_zone:
        return None, "입력한 화구와 선택한 화구가 다릅니다. 화구를 확인해 주세요."
    zone = selected_zone or zone
    if zone is None:
        return None, "화구가 지정되지 않았습니다. 조작할 화구(왼쪽/오른쪽)를 선택해 주세요."
    return Goal(zone_id=zone, direction="decrease" if match.group(2).startswith(("내려","낮춰","줄여","감소")) else "increase", requested_steps=1), None
