from __future__ import annotations
from typing import Callable
from pathlib import Path
import logging


class VagrantContextService:
    """
    Constrói o contexto do Vagrantfile a partir do config + YAML de experimento.
    """

    def __init__(self, *, cfg, append_log: Callable[[str], None], logger: logging.Logger):
        self.cfg = cfg
        self.append_log = append_log
        self.logger = logger

    def build(self, yaml_path: Path | None) -> dict:
        try:
            base_ctx = self.cfg.to_template_ctx()
        except Exception as e:
            self.append_log(f"[Vagrant] Falha ao obter ctx do config: {e}")
            raise

        ctx = dict(base_ctx)
        try:
            machines = [dict(m) for m in (base_ctx.get("machines") or [])]
        except Exception:
            machines = []

        if not machines:
            try:
                machines = []
                for m in self.cfg.machines:
                    machines.append({
                        "name": m.name,
                        "box": m.box,
                        "ip_last_octet": m.ip_last_octet,
                        "memory": getattr(m, "memory", None),
                        "cpus": getattr(m, "cpus", None),
                    })
            except Exception as e:
                self.append_log(f"[Vagrant] Falha ao normalizar máquinas: {e}")

        victim_ip = None
        yaml_p = yaml_path
        if yaml_p:
            try:
                from app.core.yaml_parser import _safe_load_yaml
                data = _safe_load_yaml(str(yaml_p)) or {}
                victim_ip = ((data.get("targets") or {}).get("victim_ip") or "").strip() or None
            except Exception as e:
                self.append_log(f"[Guide] falha ao carregar YAML: {e}")

        try:
            if victim_ip:
                ip_base = getattr(self.cfg, "ip_base", ctx.get("ip_base"))
                if ip_base and victim_ip.startswith(ip_base):
                    try:
                        last_octet = int(victim_ip.split(".")[-1])
                    except Exception:
                        last_octet = None

                    if last_octet is not None:
                        for m in machines:
                            if (m.get("name") or "").lower() == "victim":
                                old = m.get("ip_last_octet")
                                m["ip_last_octet"] = last_octet
                                self.append_log(
                                    f"[Vagrant] Ajustando victim ip_last_octet {old}→{last_octet} (do YAML)."
                                )
                                break
                    else:
                        self.append_log(f"[WARN] victim_ip inválido no YAML: {victim_ip}")
                else:
                    self.append_log(
                        f"[WARN] victim_ip do YAML ({victim_ip}) não corresponde ao ip_base do lab ({getattr(self.cfg, 'ip_base', 'desconhecido')}). Mantendo config."
                    )
        except Exception as e:
            self.append_log(f"[WARN] Não foi possível ajustar IP da vítima: {e}")

        ctx["machines"] = machines
        ctx["ip_base"] = getattr(self.cfg, "ip_base", ctx.get("ip_base"))
        return ctx