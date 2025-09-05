import logging
from dataclasses import dataclass, field
from typing import List, Dict, Any, Tuple

logger = logging.getLogger("[Experiment]")

@dataclass(frozen=True)
class CapturePlan:
    rotate_seconds: int = 300
    rotate_size_mb: int = 100
    zeek_rotate_seconds: int = 3600

    @classmethod
    def with_defaults(cls):
        try:
            return cls()
        except Exception as e:
            logger.error(f"[Experiment] Falha ao criar CapturePlan padrão: {e}")
            raise

@dataclass(frozen=True)
class Experiment:
    exp_id: str
    name: str
    targets: Dict[str, Any]  # {"victim_ip":"192.168.56.20", "sensor":"sensor", "victim":"victim", "attacker":"attacker"}
    actions: Tuple[Any, ...] = field(default_factory=tuple)  # imutável
    capture_plan: CapturePlan = field(default_factory=CapturePlan.with_defaults)

    @classmethod
    def with_defaults(cls, exp_id: str, victim_ip: str):
        """
        Gera um experimento mínimo com targets padrão e CapturePlan default.
        Ideal para cenários ad-hoc, mas para execução vinda de YAML
        prefira construir com ações e capture_plan explícitos no loader.
        """
        try:
            targets = {
                "victim_ip": victim_ip,
                "sensor": "sensor",
                "victim": "victim",
                "attacker": "attacker"
            }
            return cls(exp_id=exp_id, name=exp_id, targets=targets)
        except Exception as e:
            logger.error(f"[Experiment] with_defaults falhou: {e}")
            raise
