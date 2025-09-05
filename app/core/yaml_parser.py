import logging, json, time
from pathlib import Path

logger = logging.getLogger("[Guide]")

def _safe_load_yaml(yaml_path: str) -> dict:
    """
    Lê YAML como dict usando PyYAML (safe_load). Não depende do manage_lab.
    """
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


def _steps_header() -> list[dict]:
    # Passos didáticos iniciais para o modo "playbook"
    return [
        {
            "id": "preflight",
            "title": "Preflight do Lab",
            "description": "Valida virtualização, Vagrant, rede host-only e chaves SSH.",
            "host": "attacker",
            "command": "echo Preflight OK (executado pela UI principal).",
            "tags": ["safety", "infra"],
            "eta": "~1 min",
            "artifacts": [],
        },
        {
            "id": "up_vms",
            "title": "Subir e aquecer VMs",
            "description": "Suba sensor, vítima e atacante. Aguarde SSH ficar pronto (a UI já faz o aquecimento).",
            "host": "attacker",
            "command": "echo 'VMs UP (executado pela UI principal).'",
            "tags": ["infra"],
            "eta": "~2-4 min",
            "artifacts": [],
        },
    ]


# ===== Novos passos “infra/tools” (preparo real do ambiente) =====

def _step_attacker_prepare_tools() -> dict:
    """
    Kali (attacker): repara keyring e instala ferramentas de ataque.
    """
    cmd = r"""bash -lc '
set -e
sudo apt-get update || true
sudo apt-get install -y --reinstall kali-archive-keyring || true
sudo apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y nmap hydra slowhttptest curl jq
echo "[attacker] ferramentas instaladas"
'"""
    return {
        "id": "attacker_prepare",
        "title": "Preparar atacante (Kali): keyring + ferramentas",
        "description": "Reinstala keyring do Kali, atualiza índices e instala nmap/hydra/slowhttptest.",
        "host": "attacker",
        "command": cmd,
        "tags": ["infra", "tools"],
        "eta": "~1-2 min",
        "artifacts": [],
    }


def _step_sensor_prepare_tools() -> dict:
    """
    Sensor: garante tcpdump e tenta instalar Zeek se houver Candidate disponível.
    """
    cmd = r"""bash -lc '
set -e
sudo DEBIAN_FRONTEND=noninteractive apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y tcpdump
cand=$(apt-cache policy zeek | awk "/Candidate:/ {print $2}")
if [ -n "$cand" ] && [ "$cand" != "(none)" ]; then
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y zeek || true
  command -v zeek >/dev/null 2>&1 && echo "ZEEK_OK" || echo "ZEEK_SKIP"
else
  echo "ZEEK_SKIP"
fi
'"""
    return {
        "id": "sensor_prepare",
        "title": "Preparar sensor: tcpdump (+Zeek se disponível)",
        "description": "Instala tcpdump e tenta Zeek; segue PCAP-only se Zeek não estiver no repo.",
        "host": "sensor",
        "command": cmd,
        "tags": ["infra", "tools"],
        "eta": "~1-2 min",
        "artifacts": [],
    }


def _step_connectivity_check(ips: dict) -> dict:
    """
    Confirma conectividade attacker→victim e portas básicas (ex.: 80/22).
    """
    victim = ips.get("victim", "192.168.56.20")
    cmd = rf"""bash -lc '
set -e
ping -c 2 {victim} || true
nc -zv {victim} 22 || true
nc -zv {victim} 80 || true
echo "[check] conectividade básica testada"
'"""
    return {
        "id": "connectivity",
        "title": "Checagem de conectividade com a vítima",
        "description": "Ping e tentativas de conexão a portas comuns para validar o alvo.",
        "host": "attacker",
        "command": cmd,
        "tags": ["infra", "recon"],
        "eta": "<15s",
        "artifacts": [],
    }


def _step_hydra_wordlists(cfg: dict) -> dict:
    """
    Gera/normaliza listas padrão para Hydra se não existirem.
    Caso o YAML traga listas embutidas, elas serão gravadas também.
    """
    # extrai listas embutidas, quando presentes (actions.hydra_brute / legacy.brute)
    users_inline = []
    pwds_inline = []
    try:
        acts = cfg.get("actions") or []
        for a in acts:
            if (a.get("name","").lower() in ("hydra_brute", "hydra", "brute")):
                p = a.get("params") or {}
                users_inline = p.get("users_inline", []) or users_inline
                pwds_inline  = p.get("pass_inline",  []) or pwds_inline
    except Exception:
        pass
    try:
        if "brute" in cfg:
            b = cfg["brute"]
            users_inline = b.get("users_inline", users_inline)
            pwds_inline  = b.get("pass_inline",  pwds_inline)
    except Exception:
        pass

    # conteúdo padrão caso nada seja informado
    if not users_inline:
        users_inline = ["admin", "user", "test", "tcc"]
    if not pwds_inline:
        pwds_inline = ["123456", "password", "wrongpass", "tcc2025"]

    users_escaped = "\\n".join(str(u) for u in users_inline)
    pwds_escaped  = "\\n".join(str(p) for p in pwds_inline)

    cmd = rf"""bash -lc '
set -e
test -f ~/users.txt    || echo -e "{users_escaped}" | tee ~/users.txt >/dev/null
test -f ~/passwords.txt|| echo -e "{pwds_escaped}" | tee ~/passwords.txt >/dev/null
wc -l ~/users.txt ~/passwords.txt || true
'"""
    return {
        "id": "hydra_lists",
        "title": "Gerar listas de usuários/senhas (Hydra)",
        "description": "Cria ~/users.txt e ~/passwords.txt (não sobrescreve se já existirem).",
        "host": "attacker",
        "command": cmd,
        "tags": ["prep", "credential_access"],
        "eta": "<5s",
        "artifacts": ["~/users.txt", "~/passwords.txt"],
    }


def _step_sensor_capture(cfg: dict) -> dict:
    cap = cfg.get("capture", {}) or {}
    rotate_sec = cap.get("rotate_seconds", 300)
    rotate_mb = cap.get("rotate_size_mb", 100)
    return {
        "id": "sensor_capture",
        "title": "Ativar captura no Sensor",
        "description": "Liga tcpdump (e Zeek se disponível). Gera PCAP e logs em /var/log/{pcap,zeek}.",
        "host": "sensor",
        "command": (
            "nohup bash -lc 'sudo mkdir -p /var/log/pcap /var/log/zeek; "
            "ip -br addr; "
            "tcpdump -i $(ip -br addr | awk \"/192\\.168\\.56\\./{print $1; exit}\") "
            f"-w /var/log/pcap/exp_%Y%m%d_%H%M%S.pcap -G {rotate_sec} -C {rotate_mb} -n' >/dev/null 2>&1 &"
        ),
        "tags": ["capture", "pcap"],
        "eta": "contínuo",
        "artifacts": ["/var/log/pcap/*.pcap", "/var/log/zeek/*.log"],
    }


def _step_sensor_health() -> dict:
    cmd = r"""bash -lc '
echo "[health] processos relevantes:"
pgrep -fa "tcpdump -i" || true
pgrep -fa "zeek -i" || true
echo "[health] últimos arquivos:"
ls -lh /var/log/pcap/*.pcap 2>/dev/null | tail -n 5 || true
ls -lh /var/log/zeek/*.log  2>/dev/null | tail -n 5 || true
'"""
    return {
        "id": "sensor_health",
        "title": "Verificar saúde da captura (Sensor)",
        "description": "Lista processos e últimos PCAP/Zeek para confirmar coleta.",
        "host": "sensor",
        "command": cmd,
        "tags": ["capture", "healthcheck"],
        "eta": "<5s",
        "artifacts": ["/var/log/pcap/*.pcap", "/var/log/zeek/*.log"],
    }


def _step_sensor_stop() -> dict:
    cmd = r"""bash -lc '
for p in /var/run/tcc_*.pid; do [ -f "$p" ] && sudo kill "$(cat "$p")" 2>/dev/null || true; done
sudo pkill -f "tcpdump -i" || true
sudo pkill -f "zeek -i"    || true
echo "[sensor] captura desligada"
'"""
    return {
        "id": "sensor_stop",
        "title": "Desligar captura no Sensor (opcional)",
        "description": "Encerra tcpdump/zeek; normalmente pare apenas após empacotar o dataset.",
        "host": "sensor",
        "command": cmd,
        "tags": ["capture", "cleanup"],
        "eta": "<5s",
        "artifacts": [],
    }


# ===== Ações (scan/brute/dos/custom) — preserva compatibilidade =====

def _steps_from_actions(cfg: dict, ips: dict) -> list[dict]:
    """
    Converte o formato NOVO com 'actions' em passos.
    Ação -> nome e params:
      - nmap_scan: flags, ports, output
      - hydra_brute: user, pass_list, service, path, extra, port, output
      - slowhttp_dos: port, duration_s, concurrency, rate, output_prefix
      - custom: host, title, command, artifacts, tags, eta
    """
    steps = []
    actions = cfg.get("actions") or []
    victim_ip = (cfg.get("targets") or {}).get("victim_ip") or ips.get("victim")

    for i, a in enumerate(actions, start=1):
        name = (a.get("name") or "").lower()
        p = a.get("params") or {}

        if name in ("nmap_scan", "nmap", "scan"):
            flags = p.get("flags", "-sS -sV -T4")
            ports = p.get("ports", "1-1024")
            out = p.get("output", "~/exp_scan.nmap")
            steps.append({
                "id": f"scan_{i}",
                "title": "Varredura de portas e serviços (Nmap)",
                "description": "Descobre portas abertas e versões de serviços no alvo.",
                "host": "attacker",
                "command": f"nmap {flags} -p {ports} -oA {out} {victim_ip}",
                "tags": ["recon", "MITRE:T1046"],
                "eta": "~1-3 min",
                "artifacts": [f"{out}.nmap", f"{out}.gnmap", f"{out}.xml"],
            })

        elif name in ("hydra_brute", "brute", "hydra"):
            users = p.get("user") or p.get("userlist") or "~/users.txt"
            pwds = p.get("pass_list") or p.get("passlist") or "~/passwords.txt"
            service = p.get("service", "http-post-form")
            path = p.get("path", "/login")
            extra = p.get("extra", "")
            port = p.get("port")
            out = p.get("output", "~/exp_brute.hydra")
            port_flag = f"-s {port} " if port else ""
            steps.append({
                "id": f"brute_{i}",
                "title": "Força bruta de credenciais (Hydra)",
                "description": "Testa combinações de usuário/senha contra o serviço indicado.",
                "host": "attacker",
                "command": f"hydra -L {users} -P {pwds} {port_flag}{service}://{victim_ip}{path} {extra} -o {out}",
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
                "description": "Gera carga controlada contra o alvo dentro do lab (NUNCA rode fora do lab).",
                "host": "attacker",
                "command": cmd,
                "tags": ["impact", "safety"],
                "eta": "~1-2 min",
                "artifacts": [f"{outprefix}*"],
            })

        elif name in ("custom",):
            steps.append({
                "id": f"custom_{i}",
                "title": a.get("title", "Ação customizada"),
                "description": a.get("description", ""),
                "host": a.get("host", "attacker"),
                "command": a.get("command", ""),
                "tags": a.get("tags", ["custom"]),
                "eta": a.get("eta", ""),
                "artifacts": a.get("artifacts", []),
            })

        else:
            steps.append({
                "id": f"action_{i}",
                "title": f"Ação: {name or 'desconhecida'}",
                "description": "Passo gerado do YAML (ação não padronizada).",
                "host": a.get("host", "attacker"),
                "command": a.get("command", ""),
                "tags": ["custom"],
                "eta": a.get("eta", ""),
                "artifacts": a.get("artifacts", []),
            })

    return steps


def _steps_from_legacy(cfg: dict, ips: dict) -> list[dict]:
    """
    Converte o formato CLÁSSICO (sensor/scan/brute/dos/custom) em passos.
    """
    steps = []

    # 1) Sensor
    if (cfg.get("sensor") or {}).get("enable", True) or "capture" in cfg:
        steps.append(_step_sensor_capture(cfg))

    # 2) Prep
    for i, p in enumerate(cfg.get("prep", []), start=1):
        steps.append({
            "id": f"prep_{i}",
            "title": f"Preparação: {p.get('title','comando')}",
            "description": p.get("description", "Ajuste do ambiente."),
            "host": p.get("host", "victim"),
            "command": substitute_vars(p.get("command", ""), ips),
            "tags": ["prep"],
            "eta": "<30s",
            "artifacts": [],
        })

    # 3) Scan
    if "scan" in cfg:
        scan = cfg["scan"]
        target_host = scan.get("target", "victim")
        target_ip = ips.get(target_host, scan.get("target_ip", "127.0.0.1"))
        ports = scan.get("ports", "1-1024")
        flags = scan.get("flags", "-sS -sV -T4")
        out = scan.get("output", "~/exp_scan.nmap")
        steps.append({
            "id": "scan",
            "title": "Varredura de portas e serviços (Nmap)",
            "description": "Descobre portas abertas e versões de serviços no alvo.",
            "host": "attacker",
            "command": f"nmap {flags} -p {ports} -oA {out} {target_ip}",
            "tags": ["recon", "MITRE:T1046"],
            "eta": "~1-3 min",
            "artifacts": [f"{out}.nmap", f"{out}.gnmap", f"{out}.xml"],
        })

    # 4) Brute force
    if "brute" in cfg:
        b = cfg["brute"]
        target_host = b.get("target", "victim")
        target_ip = ips.get(target_host, b.get("target_ip", "127.0.0.1"))
        service = b.get("service", "http-post-form")
        path = b.get("path", "/login")
        users = b.get("userlist", "~/users.txt")
        pwds = b.get("passlist", "~/passwords.txt")
        extra = b.get("extra", "")
        port = b.get("port", "")
        out = b.get("output", "~/exp_brute.hydra")
        port_flag = f"-s {port} " if port else ""
        steps.append({
            "id": "brute",
            "title": "Força bruta de credenciais (Hydra)",
            "description": "Testa combinações de usuário/senha contra o serviço indicado.",
            "host": "attacker",
            "command": f"hydra -L {users} -P {pwds} {port_flag}{service}://{target_ip}{path} {extra} -o {out}",
            "tags": ["credential_access", "MITRE:T1110"],
            "eta": "varia",
            "artifacts": [out],
        })

    # 5) DoS
    if "dos" in cfg:
        d = cfg["dos"]
        target_host = d.get("target", "victim")
        target_ip = ips.get(target_host, d.get("target_ip", "127.0.0.1"))
        tool = d.get("tool", "slowhttptest")
        params = d.get("params", "-X -c 400 -r 50 -l 60")
        out = d.get("output", "~/exp_dos")
        if tool == "slowhttptest":
            cmd = f"slowhttptest {params} -g -o {out} -u http://{target_ip}/"
        elif tool == "hping3":
            cmd = f"hping3 -S -p 80 --flood {params} {target_ip}"
        else:
            cmd = f"{tool} {params}"
        steps.append({
            "id": "dos",
            "title": "Degradação/DoS controlado",
            "description": "Gera carga controlada contra o alvo dentro do lab (NUNCA rode fora do lab).",
            "host": "attacker",
            "command": cmd,
            "tags": ["impact", "safety"],
            "eta": "~1-2 min",
            "artifacts": [f"{out}*"],
        })

    # 6) Custom
    for i, c in enumerate(cfg.get("custom", []), start=1):
        steps.append({
            "id": f"custom_{i}",
            "title": c.get("title", "Ação customizada"),
            "description": c.get("description", ""),
            "host": c.get("host", "attacker"),
            "command": substitute_vars(c.get("command", ""), ips),
            "tags": c.get("tags", ["custom"]),
            "eta": c.get("eta", ""),
            "artifacts": c.get("artifacts", []),
        })

    return steps


def parse_yaml_to_steps(yaml_path: str, ssh, vagrant) -> list[dict]:
    """
    Lê o YAML e traduz em passos executáveis para o Guia do Experimento.
    - Suporta formato novo com 'actions' (nmap_scan/hydra_brute/slowhttp_dos).
    - Suporta formato clássico (sensor/scan/brute/dos/custom).
    - Enriquecido com preparo de ferramentas, checagens e saúde da captura.
    """
    cfg = _safe_load_yaml(yaml_path)
    ips = resolve_guest_ips(ssh)

    steps: list[dict] = []
    steps.extend(_steps_header())

    # Infra mínima: preparar tools e validadores
    steps.append(_step_attacker_prepare_tools())
    steps.append(_step_sensor_prepare_tools())

    # Captura viva no sensor (default: ligado se houver 'capture' ou 'sensor.enable')
    if "capture" in cfg or (cfg.get("sensor", {}).get("enable", True)):
        steps.append(_step_sensor_capture(cfg))

    # Conectividade antes dos ataques
    steps.append(_step_connectivity_check(ips))

    # Listas para Hydra (sempre cria se não existir)
    steps.append(_step_hydra_wordlists(cfg))

    # Preferir 'actions' se existir, senão legacy
    if "actions" in cfg:
        steps.extend(_steps_from_actions(cfg, ips))
    else:
        steps.extend(_steps_from_legacy(cfg, ips))

    # Saúde da captura (rápido)
    steps.append(_step_sensor_health())

    # Passo final “coleta+zip” (UI tem botão dedicado; aqui só informa)
    steps.append({
        "id": "harvest",
        "title": "Coletar artefatos e empacotar dataset",
        "description": "Puxa PCAP/Zeek e artefatos do atacante. Use o botão 'Gerar dataset (Runner)' abaixo.",
        "host": "attacker",
        "command": "echo 'Use o botão Gerar dataset (Runner) na UI.'",
        "tags": ["dataset"],
        "eta": "~30-90s",
        "artifacts": ["data/<exp_id>.zip"],
    })

    # Opcional: passo de desligar captura (se desejar encerrar manualmente)
    steps.append(_step_sensor_stop())

    return steps
