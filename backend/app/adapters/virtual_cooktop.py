"""Local operator simulator. Internal levels/scenario are never Agent inputs."""
from threading import RLock
from copy import deepcopy
import uuid
from app.calibration.fitting import DevelopmentSample, fit_candidate
from app.observations.state import StateTracker
from app.adapters.mock import MockAdapter, ScheduledObservation, VirtualClock
from app.contracts.models import DeviceObservation, FieldSummary, Quality

class VirtualCooktopAdapter(MockAdapter):
    def __init__(self, scenario, zone_id, initial_levels=None):
        self.guard = RLock()
        self.scenario = scenario
        self.target_zone = zone_id
        self.levels = deepcopy(initial_levels if initial_levels is not None else {"left": 5, "right": 5})
        if set(self.levels)!={"left","right"} or any(type(v) is not int or not 0<=v<=9 for v in self.levels.values()):
            raise ValueError("invalid virtual device levels")
        self.session_id="mock-session-"+uuid.uuid4().hex[:10]
        self.tracker=StateTracker()
        self.mock_profiles={}
        for zone in ("left","right"):
            samples=[DevelopmentSample(trial_id=f"synthetic-{zone}-{level}-{trial}",split="development",reference_source="mock_fixture",displayed_level=level,
                cooktop_model="virtual-only",cookware_id="virtual-only",installation_id="virtual-only",
                observation=self.frame(level*3+trial,0,500,500,float(level*20),0,zone=zone).observation) for level in range(10) for trial in range(3)]
            candidate=fit_candidate(samples,calibration_id="mock-test-v1",margin=2.0)
            self.mock_profiles[zone]=candidate
            self.tracker.zones[zone].activate_mock(candidate,installation_id="virtual-only")
        self.presses = {}
        self.receipts = {}
        self.baselines = {}
        self.next_seq = 10
        initial = [self.frame(i*2+j,i*100,(i+1)*100,(i+1)*100,float(self.levels[zone]*20),0,zone=zone) for i in range(5) for j,zone in enumerate(("left","right"))]
        super().__init__(initial,VirtualClock(now_ms=500))

    def frame(self,seq,start,end,arrival,rms,beeps,fault=False,zone=None):
        return ScheduledObservation(arrival,DeviceObservation(
            schema_version="0.2",device_id="mock-device-01",session_id=self.session_id,seq=seq,
            source="mock",zone_id=zone or self.target_zone,window_start_ms=start,window_end_ms=end,
            beep_count=None if fault else beeps,
            field_summary=None if fault else FieldSummary(rms=float(rms),unit="adc_count",sample_count=(end-start)//5,sample_rate_hz=200,method="envelope_rms_v1"),
            quality=Quality(status="invalid",reasons=["disconnected"]) if fault else Quality(status="valid",reasons=[]),
            calibration_id="mock-test-v1"))

    def press(self,action_id,zone,operation,window_start,window_end):
        with self.guard:
            previous=self.presses.get(action_id)
            if previous:
                if previous["zone"]!=zone or previous["operation"]!=operation:
                    raise ValueError("same action has a different press")
                return
            baseline=self.read_baseline(self.target_zone)
            self.baselines[action_id]=baseline.model_dump(mode="json") if baseline else None
            missed=self.scenario=="recovery_exhausted" or self.scenario=="no_change" or self.scenario=="recovery" and not self.presses
            before=self.levels[zone]
            target_before=self.levels[self.target_zone]
            if not missed:
                self.levels[zone]=max(0,min(9,before+(1 if operation=="increase" else -1)*(2 if self.scenario=="double_input" else 1)))
            recognized=not missed
            self.presses[action_id]={"action_id":action_id,"zone":zone,"operation":operation,"recognized":recognized}
            if self.scenario=="missing":
                return
            start=window_start+500;end=start+500
            rms=float(self.levels[self.target_zone]*20)
            arrival=end+3000 if self.scenario=="delayed" else end
            for observed_zone in (self.target_zone,"left" if self.target_zone=="right" else "right"):
                # One microphone event belongs to the target observation; do not double count it.
                beeps=(2 if self.scenario=="double_input" else 1)*int(recognized) if observed_zone==self.target_zone else None
                frame=self.frame(self.next_seq,start,end,arrival,float(self.levels[observed_zone]*20),beeps,self.scenario=="sensor_error",zone=observed_zone)
                self.next_seq+=1
                self._pending.append(frame)
                if self.scenario=="duplicate":
                    self._pending.append(ScheduledObservation(arrival+100,frame.observation.model_copy(deep=True)))
            if self.levels[self.target_zone]==target_before and self.scenario in ("normal","recovery","recovery_exhausted","duplicate"):
                for observed_zone in ("left","right"):
                    self._pending.append(self.frame(self.next_seq,window_end+500,window_end+1000,window_end+1000,float(self.levels[observed_zone]*20),0,zone=observed_zone))
                    self.next_seq+=1
            self._pending.sort(key=lambda item:item.arrival_ms)

    def calibration_for_zone(self, zone_id):
        with self.guard:
            base=super().read_baseline(zone_id)
            state=self.tracker.zones[zone_id]
            if base is None or not state.observe(base,now_ms=self.clock.now_ms,max_age_ms=500):
                return None
            return state.calibration()

    def read_baseline(self,zone_id):
        with self.guard:return super().read_baseline(zone_id)

    def check_sensor_integrity(self):
        with self.guard:return super().check_sensor_integrity()

    def await_zone_pair(self,**kwargs):
        with self.guard:
            items=self.await_touch_result(**kwargs)
            other=dict(kwargs,zone_id="left" if kwargs["zone_id"]=="right" else "right",timeout_ms=0)
            return items+self.await_touch_result(**other)

    def await_touch_result(self,**kwargs):
        with self.guard:
            items=super().await_touch_result(**kwargs)
            for item in items:self.receipts[item.observation_id]=item.model_copy(deep=True)
            return items

    def operator_view(self):
        with self.guard:
            return {"kind":"virtual_cooktop","levels":deepcopy(self.levels), "target_zone":self.target_zone, "calibration_source":"mock",
                    "sensor_states":{zone:state.snapshot() for zone,state in self.tracker.zones.items()}, "baselines":deepcopy(self.baselines),
                    "last_press":deepcopy(list(self.presses.values())[-1]) if self.presses else None,
                    "observations":[item.model_dump(mode="json") for item in self.receipts.values()]}
