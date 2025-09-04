import logging, yaml
from pathlib import Path
from typing import Any, Dict

from lab.actions.brute import HydraBruteAction
from lab.actions.scan import NmapScanAction
from lab.actions.dos import SlowHTTPDoSAction
from lab.actions.hping3_syn import Hping3SynFloodAction
from lab.actions.brute_http import HydraHttpPostBruteAction
from lab.orchestrator.experiment import Experiment, CapturePlan

logger = logging.getLogger("[YAMLLoader]")

_ACTIONS_REGISTRY = {
    "nmap_scan": NmapScanAction,
    "hydra_brute": HydraBruteAction,
    "slowhttp_dos": SlowHTTPDoSAction,
    "hping3_syn": Hping3SynFloodAction,
    "hydra_http_post": HydraHttpPostBruteAction,
}

def load_experiment_from_yaml(path: str) -> Experiment:
    try:
        cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        exp_id = cfg.get("exp_id", "EXP")
        victim_ip = cfg["targets"]["victim_ip"]
        exp = Experiment.with_defaults(exp_id=exp_id, victim_ip=victim_ip)

        cap = cfg.get("capture", {})
        exp.capture_plan = CapturePlan(
            rotate_seconds=cap.get("rotate_seconds", 300),
            rotate_size_mb=cap.get("rotate_size_mb", 100),
            zeek_rotate_seconds=cap.get("zeek_rotate_seconds", 3600),
        )

        for a in cfg.get("actions", []):
            name = a["name"]
            params: Dict[str, Any] = a.get("params", {})
            cls = _ACTIONS_REGISTRY.get(name)
            if not cls:
                logger.warn(f"[YAMLLoader] Ação desconhecida: {name} — ignorando.")
                continue
            exp.actions.append(cls(**params))
        return exp
    except Exception as e:
        logger.error(f"[YAMLLoader] Falha ao carregar YAML: {e}")
        raise
