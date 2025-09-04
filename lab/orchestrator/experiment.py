import logging
from dataclasses import dataclass, field
from typing import List, Dict, Any

logger = logging.getLogger("[Experiment]")

@dataclass(frozen=True)
class CapturePlan:
    rotate_seconds: int = 300
    rotate_size_mb: int = 100
    zeek_rotate_seconds: int = 3600

    @classmethod
    def with_defaults(cls):
        return cls()

@dataclass(frozen=True)
class Experiment:
    exp_id: str
    name: str
    targets: Dict[str, Any]  # {"victim_ip":"192.168.56.20", "sensor":"sensor", "victim":"victim", "attacker":"attacker"}
    actions: List[Any] = field(default_factory=list)  # lista de Action plugins
    capture_plan: CapturePlan = field(default_factory=CapturePlan.with_defaults)

    @classmethod
    def with_defaults(cls, exp_id: str, victim_ip: str):
        targets = {"victim_ip": victim_ip, "sensor": "sensor", "victim": "victim", "attacker": "attacker"}
        return cls(exp_id=exp_id, name=exp_id, targets=targets)
