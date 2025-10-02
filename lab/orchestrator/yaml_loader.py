# lab/orchestrator/yaml_loader.py
from __future__ import annotations
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

def _to_profiles(lst: List[Dict[str, Any]]) -> Dict[str, ProfileSpec]:
    out: Dict[str, ProfileSpec] = {}
    for p in (lst or []):
        pid = str(p.get("id"))
        if not pid:
            logger.warning("[YAML] Profile sem 'id' ignorado.")
            continue
        out[pid] = ProfileSpec(
            id=pid,
            template=str(p.get("template") or ""),
            params=(p.get("params") or {}) or {}
        )
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

def resolve_profile_command(
    spec: ExperimentSpec,
    profile_id: str,
    ips: Dict[str, str],
) -> Tuple[str, str, int]:
    prof = spec.profiles.get(profile_id)
    if not prof:
        raise ValueError(f"Profile '{profile_id}' não encontrado.")
    tmpl = spec.templates.get(prof.template)
    if not tmpl:
        raise ValueError(f"Template '{prof.template}' (do profile '{profile_id}') não encontrado.")

    ctx: Dict[str, Any] = {}
    ctx.update(spec.gvars or {})
    ctx.update(_flatten("experiment", spec.experiment or {}))
    ctx.update(prof.params or {})
    ctx.update({
        "victim":   ips.get("victim", ""),
        "attacker": ips.get("attacker", ""),
        "sensor":   ips.get("sensor", ""),
    })

    cmd = _safe_format(tmpl.cmd_template, ctx).strip()
    # normaliza CRLF -> LF para evitar surpresas no host remoto
    cmd = cmd.replace("\r\n", "\n").replace("\r", "\n")

    duration = None
    try:
        if "duration_s" in prof.params:
            duration = int(prof.params["duration_s"])
    except Exception:
        duration = None

    if duration and "{duration_s}" not in tmpl.cmd_template and not cmd.startswith("timeout "):
        cmd = f"timeout {duration} {cmd}"

    ssh_timeout = max(30, int(duration) + 30) if duration else int(spec.gvars.get("max_duration_s") or 900)
    run_on = tmpl.run_on or "attacker"

    logger.info(f"[YAML] profile={profile_id} run_on={run_on} ssh_timeout={ssh_timeout}s cmd_len={len(cmd)}")
    return run_on, cmd, ssh_timeout
