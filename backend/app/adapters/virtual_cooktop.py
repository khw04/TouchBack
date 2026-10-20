"""Local operator simulator. Internal levels/scenario are never Agent inputs."""
from threading import RLock
from copy import deepcopy
from app.adapters.mock import MockAdapter, ScheduledObservation, VirtualClock
from app.contracts.models import DeviceObservation, FieldSummary, Quality

class VirtualCooktopAdapter(MockAdapter):
    def __init__(self, scenario, zone_id):
        self.guard = RLock()
        self.scenario = scenario
        self.target_zone = zone_id
        self.levels = {"left": 5, "right": 5}
        self.presses = {}
        self.receipts = {}
        self.baselines = {}
        self.next_seq = 5
        initial = [self.frame(i,i*100,(i+1)*100,(i+1)*100,100.0,0) for i in range(5)]
        super().__init__(initial,VirtualClock(now_ms=500))

    def frame(self,seq,start,end,arrival,rms,beeps,fault=False):
        return ScheduledObservation(arrival,DeviceObservation(
            schema_version="0.2",device_id="mock-device-01",session_id="mock-session-01",seq=seq,
            source="mock",zone_id=self.target_zone,window_start_ms=start,window_end_ms=end,
            beep_count=None if fault else beeps,
            field_summary=None if fault else FieldSummary(rms=float(rms),unit="adc_count",sample_count=end-start,sample_rate_hz=1000,method="ac_rms_v1"),
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
            if not missed:
                self.levels[zone]=max(0,min(9,before+(1 if operation=="increase" else -1)))
            recognized=not missed
            self.presses[action_id]={"action_id":action_id,"zone":zone,"operation":operation,"recognized":recognized}
            if self.scenario=="missing":
                return
            start=window_start+500;end=start+500
            rms=float(self.levels[self.target_zone]*20)
            arrival=end+3000 if self.scenario=="delayed" else end
            frame=self.frame(self.next_seq,start,end,arrival,rms,int(recognized),self.scenario=="sensor_error")
            self.next_seq+=1
            self._pending.append(frame)
            if self.scenario=="duplicate":
                self._pending.append(ScheduledObservation(arrival+100,frame.observation.model_copy(deep=True)))
            # A fresh independently timed baseline after the original action window.
            # It is not returned as evidence for the original action, and does not
            # authorize another action until existing recovery policy permits it.
            if rms==100.0 and self.scenario in ("normal","recovery","recovery_exhausted","duplicate"):
                self._pending.append(self.frame(self.next_seq,window_end+500,window_end+1000,window_end+1000,rms,0))
                self.next_seq+=1
            self._pending.sort(key=lambda item:item.arrival_ms)

    def read_baseline(self,zone_id):
        with self.guard:return super().read_baseline(zone_id)

    def check_sensor_integrity(self):
        with self.guard:return super().check_sensor_integrity()

    def await_touch_result(self,**kwargs):
        with self.guard:
            items=super().await_touch_result(**kwargs)
            for item in items:self.receipts[item.observation_id]=item.model_copy(deep=True)
            return items

    def operator_view(self):
        with self.guard:
            return {"kind":"virtual_cooktop","levels":deepcopy(self.levels), "baselines":deepcopy(self.baselines),
                    "last_press":deepcopy(list(self.presses.values())[-1]) if self.presses else None,
                    "observations":[item.model_dump(mode="json") for item in self.receipts.values()]}
