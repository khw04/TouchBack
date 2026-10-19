"""Diagnose paired coil observations and one microphone event; no device truth input."""
from app.contracts.models import Goal
from app.verification.verifier import verify_intent, verdict

LABEL={"left":"왼쪽","right":"오른쪽"}


def diagnose_pair(goal, baselines, profiles, observations, context):
    zones={};evidence=[];results={}
    for zone in ("left","right"):
        base=baselines.get(zone);profile=profiles.get(zone)
        items=[o for o in observations if o.device_observation.zone_id==zone]
        checked=verify_intent(Goal(zone_id=zone,direction=goal.direction,requested_steps=1),base,items,context,profile)
        results[zone]=checked;evidence.extend(checked.evidence_ids)
        if not items or checked.verification_level!="exact_step" and "no_output_change" not in checked.reason_codes:
            return {"code":"unreliable_observation","message":"양쪽 화구의 관측을 신뢰할 수 없어 오조작 원인을 확정하지 않았습니다.","next_action":"추가 조작을 멈추고 관측 상태를 확인하세요.","source":"mock","action_id":context.action_id,"evidence_ids":list(dict.fromkeys(evidence)),"zones":{},"correction":None},verdict("uncertain","none",None,["paired_observation_unreliable"],evidence)
        delta=items[-1].device_observation.field_summary.rms-base.device_observation.field_summary.rms
        bands=[b.steps for b in profile.step_bands if b.delta_min<=delta<=b.delta_max]
        zones[zone]={"delta":delta,"steps":bands[0] if len(bands)==1 else 0,"beep_count":sum(o.device_observation.beep_count or 0 for o in items)}
    other="left" if goal.zone_id=="right" else "right"
    target=zones[goal.zone_id];outside=zones[other];expected=1 if goal.direction=="increase" else -1
    correction=None
    if outside["steps"]:
        code="other_zone_changed" if not target["steps"] else "multiple_zones_changed"
        message=f"{LABEL[other]} 화구의 출력이 {'증가' if outside['steps']>0 else '감소'}했습니다." if code=="other_zone_changed" else "두 화구에서 출력 변화가 확인돼 현재 목표를 완료하지 않았습니다."
        if code=="other_zone_changed" and outside["steps"] in (-1,1):
            correction={"zone_id":other,"direction":"decrease" if outside["steps"]==1 else "increase"}
        result=verdict("uncertain","none",None,[code],evidence)
    elif target["steps"]==expected:
        code="goal_verified";message=f"{LABEL[goal.zone_id]} 화구의 요청한 한 단계 변화를 모의 보정 범위에서 확인했습니다."
        result=results[goal.zone_id]
    elif target["steps"]:
        code="opposite_direction" if target["steps"]*expected<0 else "different_step_change"
        message="요청과 반대 방향의 출력 변화가 확인됐습니다." if code=="opposite_direction" else "모의 보정 범위에서 한 단계를 넘는 변화가 확인됐습니다. 실제 터치 횟수는 확정하지 않습니다."
        # Do not prescribe a multi-step automatic reversal from magnitude alone.
        if target["steps"] in (-1,1):
            correction={"zone_id":goal.zone_id,"direction":"decrease" if target["steps"]==1 else "increase"}
        result=verdict("uncertain","none",None,[code],evidence)
    else:
        can_change=any(b.steps==expected for b in profiles[goal.zone_id].step_bands)
        code="mock_range_limit" if not can_change else "touch_not_recognized_candidate" if target["beep_count"]==0 else "recognized_without_change"
        message={"mock_range_limit":"모의 보정 범위의 최대 또는 최소 단계여서 요청한 변화를 진행할 수 없습니다.","touch_not_recognized_candidate":"비프음과 출력 변화가 없어 입력 미인식 가능성이 있습니다.","recognized_without_change":"비프음은 관측됐지만 출력 변화가 없습니다. 원인을 단정하지 않습니다."}[code]
        result=results[goal.zone_id] if code=="touch_not_recognized_candidate" else verdict("uncertain","none",None,[code],evidence)
    next_action="안내된 화구의 버튼을 확인하세요. 재조작은 새 안내가 있을 때만 하세요." if code=="touch_not_recognized_candidate" else "추가 조작을 멈추고 두 화구 상태를 확인하세요." if code!="goal_verified" else "요청한 변화의 확인이 끝났습니다."
    result=result.model_copy(update={"evidence_ids":list(dict.fromkeys(evidence))})
    return {"code":code,"message":message,"next_action":next_action,"source":"mock","action_id":context.action_id,"evidence_ids":list(dict.fromkeys(evidence)),"zones":zones,"correction":correction},result
