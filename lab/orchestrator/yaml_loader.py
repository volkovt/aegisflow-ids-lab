# lab/orchestrator/yaml_loader.py
from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import logging
import yaml

from app.core.logger_setup import setup_logger

logger = setup_logger(Path('.logs'), name="[YamlLoader]")

@dataclass
class TemplateSpec:
    template_id: str
    run_on: str = "attacker"
    cmd_template: str = ""
    label: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

@dataclass
class ProfileSpec:
    id: str
    template: str
    params: Dict[str, Any] = field(default_factory=dict)

@dataclass
class WorkflowStep:
    name: str
    actions: List[Dict[str, Any]] = field(default_factory=list)

@dataclass
class ExperimentSpec:
    experiment: Dict[str, Any]
    network: Dict[str, Any]
    targets: Dict[str, Dict[str, Any]]
    templates: Dict[str, TemplateSpec]
    profiles: Dict[str, ProfileSpec]
    workflow: List[WorkflowStep]
    gvars: Dict[str, Any] = field(default_factory=dict)

def _load_yaml_all(path: Path) -> Dict[str, Any]:
    docs = list(yaml.safe_load_all(Path(path).read_text(encoding="utf-8")))
    return (docs[0] or {}) if docs else {}

def _to_templates(obj: Dict[str, Any]) -> Dict[str, TemplateSpec]:
    out: Dict[str, TemplateSpec] = {}
    for tid, td in (obj or {}).items():
        out[tid] = TemplateSpec(
            template_id=tid,
            run_on=str(td.get("run_on") or "attacker"),
            cmd_template=str(td.get("cmd_template") or ""),
            label=(td.get("label") or None),
            metadata=(td.get("metadata") or {}) or {},
        )
    return out


def _to_profiles(obj: Any) -> Dict[str, ProfileSpec]:
    out: Dict[str, ProfileSpec] = {}
    try:
        if isinstance(obj, dict):
            # aceita profiles como mapa: { brute_small: {id:..., template:..., params:...}, ... }
            for key, val in (obj or {}).items():
                pd = val or {}
                pid = str(pd.get("id") or key)
                if not pid:
                    logger.warning("[YAML] Profile sem 'id' e sem chave, ignorado.")
                    continue
                out[pid] = ProfileSpec(
                    id=pid,
                    template=str(pd.get("template") or ""),
                    params=(pd.get("params") or {}) or {}
                )
        elif isinstance(obj, list):
            for p in (obj or []):
                if not isinstance(p, dict):
                    logger.warning(f"[YAML] Profile inválido (não-dict): {p!r}")
                    continue
                pid = str(p.get("id") or "")
                if not pid:
                    logger.warning("[YAML] Profile sem 'id' ignorado.")
                    continue
                out[pid] = ProfileSpec(
                    id=pid,
                    template=str(p.get("template") or ""),
                    params=(p.get("params") or {}) or {}
                )
        elif obj is None:
            return {}
        else:
            logger.error(f"[YAML] Campo 'profiles' com tipo inesperado: {type(obj).__name__}")
    except Exception as e:
        logger.error(f"[YAML] Falha ao processar 'profiles': {e}")
        raise
    return out

def _to_workflow(lst: List[Dict[str, Any]]) -> List[WorkflowStep]:
    out: List[WorkflowStep] = []
    for step in (lst or []):
        out.append(WorkflowStep(
            name=str(step.get("name") or "step"),
            actions=list(step.get("actions") or [])
        ))
    return out

def _flatten(prefix: str, d: Dict[str, Any]) -> Dict[str, Any]:
    flat = {}
    for k, v in (d or {}).items():
        key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            flat.update(_flatten(key, v))
        else:
            flat[key] = v
    return flat

def _safe_format(s: str, mapping: Dict[str, Any]) -> str:
    try:
        class _Safe(dict):
            def __missing__(self, k): return "{" + k + "}"
        return (s or "").format_map(_Safe(**{k:str(v) for k,v in mapping.items()}))
    except Exception as e:
        logger.warning(f"[YAML] Falha no format de '{s[:50]}...': {e}")
        return s or ""

def load_experiment_from_yaml(path: str | Path) -> ExperimentSpec:
    try:
        p = Path(path)
        y = _load_yaml_all(p)

        experiment = (y.get("experiment") or {}) or {}
        network    = (y.get("network") or {}) or {}
        targets    = (y.get("targets") or {}) or {}
        templates  = _to_templates(y.get("templates") or {})
        profiles   = _to_profiles(y.get("profiles") or [])
        workflow   = _to_workflow(y.get("workflow") or [])
        gvars      = (y.get("global") or {}) or {}

        exp_id = str(experiment.get("id") or p.stem)
        logger.info(f"[YAML] exp_id={exp_id} templates={len(templates)} profiles={len(profiles)} steps={len(workflow)}")

        return ExperimentSpec(
            experiment=experiment, network=network, targets=targets,
            templates=templates, profiles=profiles, workflow=workflow, gvars=gvars
        )
    except Exception as e:
        logger.error(f"[YAML] Erro lendo YAML: {e}")
        raise

def _join_shell_lines(script: str) -> str:
    r"""
    Junta linhas de shell sem corromper estruturas de controle.
    Regras:
      - NÃO insere ';' após abridores: do, then, else, elif, {, (
      - Mantém conectores: &&, ||, |, &, \  (continuação)
      - Normaliza padrões como '& ;' -> '& '
    """
    import logging
    try:
        lines = [ln.strip() for ln in (script or "")
                 .replace("\r\n", "\n").replace("\r", "\n").split("\n")
                 if ln.strip()]
        if not lines:
            return ""

        openers = {"do", "then", "else", "elif", "{", "("}
        connectors = ("&&", "||", "|", "\\")
        res = lines[0]

        for seg in lines[1:]:
            prev = res.rstrip()
            # último token "visível" do acumulado
            tail_tokens = prev.replace(";", " ").split()
            last = (tail_tokens[-1] if tail_tokens else "")

            # regra do separador
            sep = " ; "
            if last in openers:
                sep = " "                  # ex.: '... do ' + 'curl ...'
            elif (prev.endswith('&') and not prev.endswith('&&')) or \
                 any(prev.endswith(t) for t in connectors) or \
                 prev.endswith(("{", "(")):
                sep = " "                  # ex.: 'cmd &&' + 'next', '\' + 'next'

            res = f"{prev}{sep}{seg}"

        # limpeza de artefatos comuns
        res = res.replace("& ;", "& ")
        return res
    except Exception as e:
        logger = logging.getLogger("[YamlLoader]")
        logger.warning(f"[YAML] _join_shell_lines falhou: {e}")
        return script or ""



def resolve_profile_command(spec: ExperimentSpec, profile_id: str, ips: Dict[str, str]) -> tuple[str, str, int]:
    """
    Monta o comando final de um profile:
    - Preenche template com params + gvars + IPs (victim/attacker/sensor)
    - Normaliza em uma única linha
    - Envolve com 'bash -lc' (para built-ins como 'set -e')
    - Aplica 'timeout <duration>' se houver
    Retorna: (run_on, cmd_final, ssh_timeout)
    """
    if profile_id not in spec.profiles:
        raise ValueError(f"Profile '{profile_id}' não encontrado.")

    prof = spec.profiles[profile_id]
    tmpl_id = prof.template
    if tmpl_id not in spec.templates:
        raise ValueError(f"Template '{tmpl_id}' não encontrado para o profile '{profile_id}'.")

    tmpl = spec.templates[tmpl_id]

    # Contexto para format()
    ctx = {}
    ctx.update(spec.gvars or {})
    ctx.update({
        "victim":   (ips or {}).get("victim", ""),
        "attacker": (ips or {}).get("attacker", ""),
        "sensor":   (ips or {}).get("sensor", "")
    })

    # Render do template
    raw_cmd = tmpl.cmd_template or ""
    try:
        rendered = _safe_format(raw_cmd, {**(prof.params or {}), **ctx})
    except Exception as e:
        logger.error(f"[YAML] falha no format do profile '{profile_id}': {e}")
        raise

    # Uma linha para shell
    cmd_one_line = _join_shell_lines(rendered)

    # Executa com shell explícito
    shell_wrapped = f"bash -lc {shlex.quote(cmd_one_line)}"

    # Timeout externo
    duration = None
    try:
        if "duration_s" in (prof.params or {}):
            duration = int(prof.params["duration_s"])
    except Exception:
        duration = None

    if duration and not shell_wrapped.startswith("timeout "):
        shell_wrapped = f"timeout {duration} {shell_wrapped}"

    # ssh_timeout coerente
    ssh_timeout = max(30, int(duration) + 30) if duration else int((spec.gvars or {}).get("max_duration_s") or 900)
    run_on = tmpl.run_on or "attacker"

    logger.info(f"[YAML] profile={profile_id} run_on={run_on} ssh_timeout={ssh_timeout}s cmd_len={len(shell_wrapped)}")
    return run_on, shell_wrapped, ssh_timeout
