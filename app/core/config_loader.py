import yaml
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Any
import logging

logger = logging.getLogger("[ConfigLoader]")

@dataclass
class SyncedFolder:
    host: str
    guest: str

@dataclass
class Provisioner:
    inline: str

@dataclass
class Machine:
    name: str
    box: str
    hostname: str
    cpus: int
    memory: int
    ip_last_octet: int
    synced_folders: List[SyncedFolder] = field(default_factory=list)
    provision: List[Provisioner] = field(default_factory=list)

@dataclass
class LabConfig:
    project_name: str
    lab_dir: str
    provider: str
    network: Dict[str, Any]
    machines: List[Machine]

    @property
    def ip_base(self) -> str:
        return self.network.get("ip_base", "192.168.56.")

    def to_template_ctx(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "machines": [
                {
                    **m.__dict__,
                    "synced_folders": [sf.__dict__ for sf in m.synced_folders],
                    "provision": [p.__dict__ for p in m.provision],
                }
                for m in self.machines
            ],
            "ip_base": self.ip_base,
        }


def load_config(path: Path) -> LabConfig:
    try:
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        machines = []
        for m in raw.get("machines", []):
            sfs = [SyncedFolder(**sf) for sf in m.get("synced_folders", [])]
            prov = [Provisioner(**p) for p in m.get("provision", [])]
            machines.append(
                Machine(
                    name=m["name"], box=m["box"], hostname=m["hostname"],
                    cpus=int(m.get("cpus", 1)), memory=int(m.get("memory", 1024)),
                    ip_last_octet=int(m["ip_last_octet"]), synced_folders=sfs, provision=prov,
                )
            )
        cfg = LabConfig(
            project_name=raw["project_name"],
            lab_dir=raw.get("lab_dir", "lab"),
            provider=raw.get("provider", "virtualbox"),
            network=raw.get("network", {}),
            machines=machines,
        )
        logger.info(f"[ConfigLoader] Config carregada: {cfg.project_name} com {len(cfg.machines)} máquinas")
        return cfg
    except Exception as e:
        logger.error(f"[ConfigLoader] Erro ao carregar config: {e}")
        raise