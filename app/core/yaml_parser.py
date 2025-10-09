import base64
from pathlib import Path

from app.core.logger_setup import setup_logger

logger = setup_logger(Path('.logs'), name="[YamlParser]")

OFFICIAL_YAML_PATH = "app/templates/official_steps.yaml"
#OFFICIAL_YAML_PATH = "app/templates/hydra_attack.yaml"

def _bash_heredoc(script: str, sudo: bool = True, strict: bool = True) -> str:
    runner = "bash -se" if strict else "bash"
    if sudo:
        runner = "sudo " + runner
    return f"{runner} <<'__EOF__'\n{script}\n__EOF__"


def _bash_heredoc_sudo_noninteractive(script: str, strict: bool = True) -> str:
    runner = "bash -se" if strict else "bash"
    return f"sudo -n {runner} <<'__EOF__'\n{script}\n__EOF__"


def _bash_b64(script: str, sudo: bool = True, strict: bool = True) -> str:
    payload = base64.b64encode(script.encode("utf-8")).decode("ascii")
    runner = "bash -e" if strict else "bash"
    if sudo:
        runner = "sudo " + runner
    return f"echo {payload} | base64 -d | {runner}"


def _safe_load_yaml(yaml_path: str) -> dict:
    try:
        import yaml  # PyYAML
    except Exception as e:
        raise RuntimeError("PyYAML não está instalado (pip install pyyaml).") from e

    try:
        data = yaml.safe_load(Path(yaml_path).read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError("YAML não é um objeto mapeável (dict).")
        return data
    except Exception as e:
        logger.error(f"[Guide] Falha ao ler YAML: {e}")
        raise RuntimeError(f"Falha ao ler YAML: {e}") from e

def resolve_guest_ips(ssh) -> dict:
    out = {}
    for name in ("attacker", "victim", "sensor"):
        try:
            cmd = "ip -4 -br addr | awk '/192\\.168\\.56\\./{print $3}' | cut -d/ -f1 | head -n1"
            ip = ssh.run_command(name, cmd, timeout=5).strip()
            out[name] = ip or "192.168.56.10"
        except Exception as e:
            logger.warning(f"[Guide] IP de {name} indisponível: {e}")
            out[name] = "192.168.56.10"
    return out

def substitute_vars(command: str, ips: dict) -> str:
    if not command:
        return command
    for k, v in ips.items():
        command = command.replace(f"{{{k}_ip}}", v)
    return command

# ===== Ações (scan/brute/dos/custom) =====
def _steps_from_actions(cfg: dict, ips: dict) -> list[dict]:
    steps = []
    actions = cfg.get("actions") or []
    victim_ip = (cfg.get("targets") or {}).get("victim_ip") or ips.get("victim")

    for i, a in enumerate(actions, start=1):
        name = (a.get("name") or "").lower()
        p = a.get("params") or {}

        if name in ("nmap_scan", "nmap", "scan"):
            flags = p.get("flags", "-sS -sV -T4")
            ports = p.get("ports", "1-1024")
            out = p.get("output", "~/exp_scan")
            cmd = f"nmap {flags} -p {ports} -oA {out} {victim_ip}"
            steps.append({
                "id": f"scan_{i}",
                "title": "Varredura de portas e serviços (Nmap)",
                "description": "Descobre portas abertas e versões de serviços no alvo.",
                "host": "attacker",
                "timeout": a.get("timeout", 300),
                "script": cmd,
                "command_normal": _bash_heredoc(cmd, sudo=False),
                "command_b64": _bash_b64(cmd, sudo=False),
                "command": cmd,
                "tags": ["recon", "MITRE:T1046"],
                "eta": "~1-3 min",
                "artifacts": [f"{out}.nmap", f"{out}.gnmap", f"{out}.xml"],
            })

        elif name in ("hydra_brute", "brute", "hydra"):
            users = p.get("user") or p.get("userlist") or "~/users.txt"
            pwds = p.get("pass_list") or p.get("passlist") or "~/passwords.txt"
            service = (p.get("service") or "ssh").lower()
            path = p.get("path", "/login")
            extra = p.get("extra", "")
            port = p.get("port")
            out = p.get("output", "~/exp_brute.hydra")
            if service in ("http-post-form", "http-get-form", "http-head", "http-get", "http-post"):
                port_flag = f":{port}" if port else ""
                cmd = f"hydra -L {users} -P {pwds} {service}://{victim_ip}{port_flag}{path} {extra} -o {out}"
            else:
                port_flag = f"-s {port} " if port else ""
                cmd = f"hydra -L {users} -P {pwds} {port_flag}{service}://{victim_ip} {extra} -o {out}"
            steps.append({
                "id": f"brute_{i}",
                "title": "Força bruta de credenciais (Hydra)",
                "description": "Testa combinações de usuário/senha contra o serviço indicado.",
                "host": "attacker",
                "timeout": a.get("timeout", 500),
                "script": cmd,
                "command_normal": _bash_heredoc(cmd, sudo=False),
                "command_b64": _bash_b64(cmd, sudo=False),
                "command": cmd,
                "tags": ["credential_access", "MITRE:T1110"],
                "eta": "varia",
                "artifacts": [out],
            })

        elif name in ("slowhttp_dos", "dos", "hping3_dos"):
            tool = "slowhttptest" if name != "hping3_dos" else "hping3"
            outprefix = p.get("output_prefix", "~/exp_dos")
            if tool == "slowhttptest":
                duration = int(p.get("duration_s", 60))
                conc = int(p.get("concurrency", 400))
                rate = int(p.get("rate", 50))
                port = int(p.get("port", 80))
                cmd = (
                    f"slowhttptest -X -c {conc} -r {rate} -l {duration} "
                    f"-u http://{victim_ip}:{port}/ -g -o {outprefix}"
                )
            else:
                params = p.get("params", "-S -p 80 --flood")
                cmd = f"hping3 {params} {victim_ip}"
            steps.append({
                "id": f"dos_{i}",
                "title": "Degradação/DoS controlado",
                "description": "Gera carga controlada dentro do lab (NUNCA rode fora do lab).",
                "host": "attacker",
                "timeout": p.get("timeout", 120),
                "script": cmd,
                "command_normal": _bash_heredoc(cmd, sudo=False),
                "command_b64": _bash_b64(cmd, sudo=False),
                "command": cmd,
                "tags": ["impact", "safety"],
                "eta": "~1-2 min",
                "artifacts": [f"{outprefix}*"],
            })

        elif name in ("custom",):
            cmd = a.get("command", "")
            steps.append({
                "id": f"custom_{i}",
                "title": a.get("title", "Ação customizada"),
                "description": a.get("description", ""),
                "host": a.get("host", "attacker"),
                "timeout": a.get("timeout", 300),
                "script": cmd,
                "command_normal": _bash_heredoc(cmd, sudo=False),
                "command_b64": _bash_b64(cmd, sudo=False),
                "command": cmd,
                "tags": a.get("tags", ["custom"]),
                "eta": a.get("eta", ""),
                "artifacts": a.get("artifacts", []),
            })
        else:
            cmd = a.get("command", "")
            steps.append({
                "id": f"action_{i}",
                "title": f"Ação: {name or 'desconhecida'}",
                "description": "Passo gerado do YAML (ação não padronizada).",
                "host": a.get("host", "attacker"),
                "timeout": a.get("timeout", 300),
                "script": cmd,
                "command_normal": _bash_heredoc(cmd, sudo=False),
                "command_b64": _bash_b64(cmd, sudo=False),
                "command": cmd,
                "tags": ["custom"],
                "eta": a.get("eta", ""),
                "artifacts": a.get("artifacts", []),
            })

    return steps


# ===== Helpers: materialização de steps a partir do YAML oficial/custom =====
def _generate_commands_for_step(script: str, sudo: bool = True, sudo_mode: str | None = None) -> dict:
    """
    Gera as variantes de comando a partir de um script (heredoc).
    Corrige bug de concatenação 'bashbash' e aplica sudo/noninteractive quando solicitado.
    """
    try:
        footer = '\necho "[guide] step_done_$$"\n'
        script = (script or "") + footer
        heredoc = f"<<'__EOF__'\n{script}\n__EOF__"

        if sudo:
            # sudo_mode: noninteractive => -n para nunca pedir senha
            if (sudo_mode or "").lower() in ("noninteractive", "non-interactive", "no-prompt", "noprompt"):
                cmd = f"sudo -n bash -se {heredoc}"
            else:
                cmd = f"sudo bash -se {heredoc}"
        else:
            cmd = f"bash -se {heredoc}"

        # versão Base64 (útil para copiar/colar)
        import base64
        b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
        cmd_b64 = "bash -se <<'__EOF__'\n" + f"echo '{b64}' | base64 -d | bash -se\n" + "__EOF__"

        return {
            "command": cmd,
            "command_normal": cmd,
            "command_b64": cmd_b64,
        }
    except Exception as e:
        logger.error(f"[Guide] _generate_commands_for_step falhou: {e}")
        # fallback mínimo (evita deixar vazio na UI)
        return {
            "command": script,
            "command_normal": script,
            "command_b64": script,
        }

def _render_with_context(script: str, ctx: dict) -> str:
    """
    Renderiza placeholders {chave} usando substituição SELETIVA por regex:
      - Só substitui padrões {identificador} onde 'identificador' ∈ ctx
      - NÃO quebra blocos do awk/tmux/shell com chaves literais (ex.: {for(...)} )
      - Suporta placeholders aninhados (ex.: pcap_bpf => "host {victim_ip} or host {attacker_ip}")
      - Mantém ${VAR} do shell intacto
    """
    import re

    try:
        text = script or ""
        safe_ctx = {k: str(v) for k, v in (ctx or {}).items()}

        # casa somente {identificador}, onde identificador = [A-Za-z_][A-Za-z0-9_]*
        pattern = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

        def repl(m):
            key = m.group(1)
            # só substitui se a chave existir no contexto; caso contrário, preserva
            return safe_ctx.get(key, m.group(0))

        # Faz múltiplas passadas para resolver placeholders aninhados (até 4 por segurança)
        out = text
        for _ in range(4):
            new_out = pattern.sub(repl, out)
            if new_out == out:
                break
            out = new_out

        return out

    except Exception as e:
        logger.warning(f"[Guide] _render_with_context falhou: {e}")
        logger.error(f"[Guide] SCRIPT: {script} | CONTEXT: {ctx}")
        logger.warning("----------------------------------------")
        return script or ""


def _apply_params_placeholders(script: str, params: dict) -> str:
    """
    Mantém tratamento especial de blocos em linha (compatibilidade com versões anteriores):
      - {users_block}, {pwds_block} montados a partir de listas users_inline/pass_inline
    (Demais chaves simples são tratadas no _render_with_context)
    """
    if not params:
        return script

    users_inline = params.get("users_inline")
    pass_inline = params.get("pass_inline")

    if users_inline and isinstance(users_inline, (list, tuple)):
        block = "\n".join(str(u) for u in users_inline)
        script = script.replace("{users_block}", block)

    if pass_inline and isinstance(pass_inline, (list, tuple)):
        block = "\n".join(str(p) for p in pass_inline)
        script = script.replace("{pwds_block}", block)

    return script

def _materialize_steps_from_yaml(cfg: dict, ips: dict) -> list[dict]:
    """
    Converte 'steps' do YAML em passos com commands prontos.
    Agora também aplica:
      - vars globais de cfg['vars'] no script
      - params do step
      - IPs (attacker_ip/victim_ip/sensor_ip)
    """
    steps = []
    gvars = (cfg.get("vars") or {}) if isinstance(cfg, dict) else {}

    for raw in (cfg.get("steps") or []):
        sid = raw.get("id")
        if not sid:
            logger.warning("[Guide] Step sem 'id' no YAML — ignorado.")
            continue

        base_script = raw.get("script", "") or ""
        params = (raw.get("params") or {}) if isinstance(raw.get("params"), dict) else {}

        # 1) Tratamento especial (blocos inline antigos)
        script = _apply_params_placeholders(base_script, params)

        # 2) Monta contexto unificado para renderização segura
        ctx = {
            # IPs como placeholders explícitos
            "attacker_ip": ips.get("attacker", ""),
            "victim_ip": ips.get("victim", ""),
            "sensor_ip": ips.get("sensor", ""),
        }
        # Vars globais do YAML
        if isinstance(gvars, dict):
            ctx.update(gvars)
        # Params do step (sobrescrevem gvars quando houver colisão)
        if isinstance(params, dict):
            for k, v in params.items():
                if isinstance(v, (str, int, float)):
                    ctx[k] = v

        # 3) Render final do script com contexto
        script = _render_with_context(script, ctx)

        # 4) Regras de sudo
        sudo = raw.get("sudo")
        if sudo is None:
            sudo = True  # padrão
        sudo_mode = (raw.get("sudo_mode") or "").strip().lower() or None

        cmds = _generate_commands_for_step(script, sudo=sudo, sudo_mode=sudo_mode)

        step = {
            "id": sid,
            "title": raw.get("title", sid),
            "description": raw.get("description", ""),
            "host": raw.get("host", "attacker"),
            "timeout": raw.get("timeout", 300),
            "script": script,
            "tags": raw.get("tags", []),
            "eta": raw.get("eta", ""),
            "artifacts": raw.get("artifacts", []),
            **cmds,
        }
        steps.append(step)

    return steps


def parse_yaml_to_steps(yaml_path: str | None, ssh=None) -> list[dict]:
    """
    Lê um YAML (custom ou oficial) e materializa a lista de passos do guia.
    - Se 'yaml_path' for None ou inexistente, carrega OFFICIAL_YAML_PATH.
    - Gera automaticamente command_normal/command_b64/command a partir do 'script' de cada step.
    - Substitui placeholders {attacker_ip}, {victim_ip}, {sensor_ip}, vars globais (cfg['vars']) e params do step.
    - Acrescenta passos derivados de 'actions' (nmap/hydra/dos/custom) se existirem.
    """
    import time
    start_ts = time.time()
    cfg = {}
    source = None

    # 1) IPs: modo fast-load (sem depender de SSH aqui)
    ips = {"attacker": "192.168.56.11", "victim": "192.168.56.12", "sensor": "192.168.56.13"}
    logger.info("[Guide] Parser em modo fast-load (no-SSH): IPs padrão aplicados.")

    # 1) Resolver IPs (via SSH quando disponível)
    # try:
    #     if ssh is not None:
    #         ips = resolve_guest_ips(ssh)
    #         logger.info(f"[Guide] IPs via SSH: {ips}")
    #     else:
    #         ips = {"attacker": "192.168.56.11", "victim": "192.168.56.12", "sensor": "192.168.56.13"}
    #         logger.info("[Guide] No SSH: usando IPs padrão (192.168.56.11/12/13).")
    # except Exception as e:
    #     logger.warning(f"[Guide] Falha ao resolver IPs via SSH: {e} — aplicando defaults.")
    #     ips = {"attacker": "192.168.56.11", "victim": "192.168.56.12", "sensor": "192.168.56.13"}

    # 2) Carregar YAML (custom ou oficial)
    try:
        from pathlib import Path
        sel_path = None
        if yaml_path and Path(yaml_path).exists():
            sel_path = str(yaml_path)
            logger.info(f"[Guide] YAML custom detectado: {sel_path}")
        else:
            sel_path = OFFICIAL_YAML_PATH
            logger.info(f"[Guide] YAML oficial selecionado: {sel_path}")

        cfg = _safe_load_yaml(sel_path) or {}
        source = sel_path
    except Exception as e:
        logger.error(f"[Guide] Erro ao carregar YAML: {e}")
        raise

    # 3) Steps declarados no YAML (+ vars globais)
    steps: list[dict] = []
    try:
        mat = _materialize_steps_from_yaml(cfg, ips)
        steps.extend(mat)
        logger.info(f"[Guide] Steps materializados do YAML: {len(mat)}")
    except Exception as e:
        logger.error(f"[Guide] Falha ao materializar steps do YAML '{source}': {e}")
        raise

    # 4) 'actions' (compatível) — também renderiza com vars/ips quando houver 'command'
    try:
        act_steps = _steps_from_actions(cfg, ips)
        if act_steps:
            # aplica render com vars globais caso o author use {cap_dir}, etc. em 'command'
            gvars = (cfg.get("vars") or {}) if isinstance(cfg, dict) else {}
            for s in act_steps:
                sc = s.get("script", "") or ""
                ctx = {
                    "attacker_ip": ips.get("attacker", ""),
                    "victim_ip": ips.get("victim", ""),
                    "sensor_ip": ips.get("sensor", ""),
                    **({k: v for k, v in (gvars or {}).items()}),
                }
                s["script"] = _render_with_context(sc, ctx)
                # re-gerar commands com sudo padrão=false (actions normalmente rodam no attacker sem sudo)
                sudo = (s.get("host", "attacker") != "attacker")  # heurística leve
                cmds = _generate_commands_for_step(s["script"], sudo=sudo, sudo_mode=None)
                s.update(cmds)

            steps.extend(act_steps)
            logger.info(f"[Guide] Passos derivados de 'actions' adicionados: {len(act_steps)}")
    except Exception as e:
        logger.warning(f"[Guide] Falha ao materializar 'actions': {e}")

    if not steps:
        logger.error(f"[Guide] Nenhum step encontrado após ler '{source}'. Verifique o arquivo YAML.")
        raise RuntimeError("YAML não contém 'steps' nem 'actions' válidos.")

    logger.info(
        f"[Guide] parse_yaml_to_steps concluído em {time.time() - start_ts:.2f}s — total de passos: {len(steps)}")
    logger.info(f"[Guide] Passos: {[s.get('id') for s in steps]}")
    return steps