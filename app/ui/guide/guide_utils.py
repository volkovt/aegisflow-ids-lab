# -*- coding: utf-8 -*-
import base64, logging, re, shlex
from pathlib import Path

logger = logging.getLogger("[GuideUtils]")
if not hasattr(logger, "warn"):
    logger.warn = logger.warning


def _safe(obj):
    try:
        return repr(obj)
    except Exception:
        return f"<{type(obj).__name__}>"


def _is_heredoc(cmd: str) -> bool:
    t = cmd or ""
    return ("<<'__EOF__'" in t) or ('<<"__EOF__"' in t) or ('<<__EOF__' in t) or ("\n__EOF__" in t)


def wrap_b64_for_copy(cmd: str) -> str:
    """Encapsula um comando em transporte base64 seguro (com sudo/nohup/&)"""
    try:
        text = (cmd or "").strip()
        if not text:
            return ""
        if "| base64 -d |" in text:
            return text  # já blindado

        has_nohup = "nohup " in text
        ends_bg = text.rstrip().endswith("&")
        wants_sudo = text.startswith("sudo ") or " sudo " in text

        inner_script = None
        m = re.search(r"""bash\s+-l?c\s+(['"])(?P<body>.*)\1\s*$""", text, re.DOTALL)
        if m:
            inner_script = m.group("body")

        runner = "bash -se"
        if wants_sudo:
            runner = "sudo " + runner

        payload = inner_script if inner_script else text
        b64 = base64.b64encode(payload.encode("utf-8")).decode("ascii")
        core = f"echo {b64} | base64 -d | {runner}"

        if has_nohup or ends_bg:
            return f'nohup sh -c {shlex.quote(core)} >/dev/null 2>&1 &'
        return core
    except Exception as e:
        logger.error(f"[b64] falha: {e}")
        return cmd


def build_copy_payloads(step: dict):
    normal = step.get("command_normal") or step.get("command") or ""
    legacy = step.get("command") or ""
    try:
        b64 = step.get("command_b64") or (wrap_b64_for_copy(normal or legacy) if (normal or legacy) else "")
    except Exception as e:
        logger.error(f"[b64] gerar: {e}")
        b64 = step.get("command_b64") or ""
    script_text = step.get("script", "")
    return normal, b64, script_text


def naive_yaml_quick_parse(yaml_path: str) -> list[dict]:
    """Fallback local rápido quando o loader oficial demora."""
    try:
        p = Path(yaml_path) if yaml_path else None
        if (not yaml_path) or (p and (not p.exists() or p.is_dir())):
            return [{
                "id": "fallback_official",
                "title": "Modo oficial (sem YAML)",
                "description": "Selecione um YAML para ver ações específicas (scan/brute/DoS).",
                "command": "",
                "host": "attacker",
                "tags": ["fallback", "official"],
                "eta": "",
                "artifacts": []
            }]
    except Exception:
        pass

    try:
        import yaml
        data = yaml.safe_load(Path(yaml_path).read_text(encoding="utf-8")) or {}
    except Exception:
        txt = Path(yaml_path).read_text(encoding="utf-8")
        data = {}
        if "actions:" in txt:
            data["actions"] = [{"name": "executar_experimento", "params": {}}]
        else:
            data["actions"] = []

    steps = []
    actions = data.get("actions") or []
    for i, act in enumerate(actions, start=1):
        name = (act.get("name") or f"acao_{i}").strip()
        params = act.get("params") or {}
        host = params.get("host") or ("attacker" if any(k in name.lower() for k in ("scan", "brute", "dos")) else "sensor")
        cmd_hint = f"# execute '{name}' com params={params}"
        steps.append({
            "id": f"fallback_{i}",
            "title": name,
            "description": "Fallback simples: parsing local sem consultas remotas.",
            "command": cmd_hint,
            "host": host,
            "tags": ["fallback"],
            "eta": "",
            "artifacts": []
        })
    if not steps:
        steps = [{
            "id": "fallback_empty",
            "title": "Sem ações no YAML",
            "description": "O arquivo não tem 'actions'.",
            "command": "",
            "host": "attacker",
            "tags": ["fallback"],
            "eta": "",
            "artifacts": []
        }]
    return steps
