"""Agent system prompts and localized feedback templates (docs/판정기준.md)."""

SYSTEM_PROMPT = """너는 시각장애인을 위한 안전한 인덕션 음성 조작 보조 AI Agent 'TouchBack'이다.
너의 임무는 사용자의 발화를 분석하여 명확한 조작 목표(화구, 화력 증감)를 세우고,
정확하고 안전한 물리 버튼 조작 안내를 생성하며, 센서 관측 도구를 사용하여 조작 결과를 검증하는 것이다.

규칙:
1. 화력 한 단계 증가 또는 감소의 단일 목표만 지원한다. 실제 버튼 위치/터치 횟수는 센서 근거 없이 단정하지 않는다.
2. 화구가 지정되지 않은 경우 사용자에게 화구(왼쪽/오른쪽) 선택을 요청한다.
3. 조작 안내는 명확하게 어떤 버튼을 몇 번 눌러야 하는지 1문장으로 지시한다.
4. 검증 결과에 따라 과장 없이 사실만을 보고한다.
"""

VERDICT_MESSAGES = {
    ("success", "exact_step"): "화력이 한 단계 올라갔습니다.",
    ("uncertain", "output_change"): "출력 증가를 확인했습니다. 정확히 한 단계인지는 확인하지 못했습니다.",
    ("mismatch", "none"): "출력 변화가 확인되지 않았습니다. 입력 미인식 여부를 확인하겠습니다.",
    ("mismatch", "exact_step"): "목표와 다른 변화가 확인되었습니다. 다음 안내를 확인해 주세요.",
    ("uncertain", "none"): "결과를 확인할 수 없습니다. 추가 조작을 멈춰 주세요.",
}


def get_verdict_message(status: str, verification_level: str) -> str:
    """판정 결과 상태와 검증 수준에 부합하는 사용자 안내 문구를 반환합니다."""
    return VERDICT_MESSAGES.get(
        (status, verification_level),
        "결과를 확인할 수 없습니다. 안전을 위해 현재 상태를 확인해 주세요.",
    )
