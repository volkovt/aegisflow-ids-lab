import logging, yaml
from pathlib import Path
from typing import Any, Dict, List

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
    """
    Carrega um experimento a partir de YAML de forma **imutável**:
    - Cria CapturePlan primeiro
    - Constrói a lista de ações
    - Instancia Experiment com tudo pronto (sem reatribuições)
    """
    try:
        cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        exp_id = cfg.get("exp_id", "EXP")
        victim_ip = cfg["targets"]["victim_ip"]

        # 1) CapturePlan (imutável, definido já no construtor)
        cap = cfg.get("capture", {}) or {}
        try:
            capture_plan = CapturePlan(
                rotate_seconds=cap.get("rotate_seconds", 300),
                rotate_size_mb=cap.get("rotate_size_mb", 100),
                zeek_rotate_seconds=cap.get("zeek_rotate_seconds", 3600),
            )
        except Exception as e:
            logger.error(f"[YAMLLoader] CapturePlan inválido no YAML: {e}")
            raise

        # 2) Ações (construir todas antes; depois passamos como tuple)
        actions_bag: List[Any] = []
        for a in cfg.get("actions", []):
            try:
                name = a["name"]
                params: Dict[str, Any] = a.get("params", {})
                cls = _ACTIONS_REGISTRY.get(name)
                if not cls:
                    logger.warning(f"[YAMLLoader] Ação desconhecida: {name} — ignorando.")
                    continue
                actions_bag.append(cls(**params))
            except Exception as e:
                logger.error(f"[YAMLLoader] Falha ao criar ação '{a}': {e}")
                raise

        # 3) Targets (explícito e rastreável)
        targets = {
            "victim_ip": victim_ip,
            "sensor": cfg.get("targets", {}).get("sensor", "sensor"),
            "victim": cfg.get("targets", {}).get("victim", "victim"),
            "attacker": cfg.get("targets", {}).get("attacker", "attacker"),
        }

        # 4) Experimento imutável pronto para execução
        try:
            exp = Experiment(
                exp_id=exp_id,
                name=cfg.get("name", exp_id),
                targets=targets,
                actions=tuple(actions_bag),
                capture_plan=capture_plan
            )
            logger.info(f"[YAMLLoader] Experimento carregado: {exp.exp_id} com {len(actions_bag)} ação(ões).")
            return exp
        except Exception as e:
            logger.error(f"[YAMLLoader] Falha ao instanciar Experiment: {e}")
            raise

    except Exception as e:
        logger.error(f"[YAMLLoader] Falha ao carregar YAML ({path}): {e}")
        raise
