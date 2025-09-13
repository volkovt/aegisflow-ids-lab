import base64
import logging, json, time
from pathlib import Path

logger = logging.getLogger("[Guide]")

def _bash_heredoc(script: str, sudo: bool = True, strict: bool = True) -> str:
    """
    Encapsula 'script' em um heredoc sem expansão:
      [sudo] bash [-se] <<'__EOF__'
      ...
      __EOF__
    Útil para uma versão 'normal' de cópia, à prova de aspas, parênteses etc.
    """
    runner = "bash -se" if strict else "bash"
    if sudo:
        runner = "sudo " + runner
    return f"{runner} <<'__EOF__'\n{script}\n__EOF__"

def _bash_heredoc_sudo_noninteractive(script: str, strict: bool = True) -> str:
    """
    Executa o SCRIPT via heredoc com sudo não-interativo:
      sudo -n bash [-se] <<'__EOF__'
      ...
      __EOF__
    Isso evita prompt de senha/TTY e reduz problemas com payloads gigantes (vs. linha única).
    """
    runner = "bash -se" if strict else "bash"
    return f"sudo -n {runner} <<'__EOF__'\n{script}\n__EOF__"


def _bash_b64(script: str, sudo: bool = True, strict: bool = True) -> str:
    """
    Empacota 'script' em Base64 e retorna um comando sem aspas internas:
      echo <B64> | base64 -d | [sudo] bash [-e]
    Evita conflitos de aspas, parênteses, pipes e here-docs ao passar pelo runner.
    """
    payload = base64.b64encode(script.encode("utf-8")).decode("ascii")
    runner = "bash -e" if strict else "bash"
    if sudo:
        runner = "sudo " + runner
    return f"echo {payload} | base64 -d | {runner}"

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
    preflight_script = 'echo "[preflight] OK (executado pela UI principal)."'
    up_script = 'echo "[infra] VMs UP (executado pela UI principal)."'

    return [
        {
            "id": "preflight",
            "title": "Preflight do Lab",
            "description": "Valida virtualização, Vagrant, rede host-only e chaves SSH.",
            "host": "attacker",
            "script": preflight_script,
            "command_normal": _bash_heredoc(preflight_script, sudo=False),
            "command_b64": _bash_b64(preflight_script, sudo=False),
            "command": _bash_b64(preflight_script, sudo=False),  # mantém execução como antes
            "tags": ["safety", "infra"],
            "eta": "~1 min",
            "artifacts": [],
        },
        {
            "id": "up_vms",
            "title": "Subir e aquecer VMs",
            "description": "Suba sensor, vítima e atacante. Aguarde SSH ficar pronto (a UI já faz o aquecimento).",
            "host": "attacker",
            "script": up_script,
            "command_normal": _bash_heredoc(up_script, sudo=False),
            "command_b64": _bash_b64(up_script, sudo=False),
            "command": _bash_b64(up_script, sudo=False),
            "tags": ["infra"],
            "eta": "~2-4 min",
            "artifacts": [],
        },
    ]



# ===== Novos passos “infra/tools” (preparo real do ambiente) =====
def _step_attacker_sudo_diag() -> dict:
    script = r'''
        set -e
        echo "[diag] whoami=$(whoami)"
        if sudo -n true 2>/dev/null; then
          echo "[diag] sudo -n OK (NOPASSWD ativo)"
        else
          echo "[diag] sudo -n FALHOU (sem NOPASSWD ou sudo exige TTY)"
          exit 42
        fi
        if command -v base64 >/dev/null 2>&1; then
          echo "[diag] base64 OK"
        else
          echo "[diag] base64 ausente"
          exit 43
        fi
    '''
    return {
        "id": "attacker_sudo_diag",
        "title": "Diagnóstico sudo/PTY no atacante",
        "description": "Verifica se sudo -n funciona e se base64 existe (evita 255 silencioso).",
        "host": "attacker",
        "command_normal": _bash_heredoc(script, sudo=False),
        "command_b64": _bash_b64(script, sudo=False),
        "command": _bash_heredoc(script, sudo=False),
        "tags": ["infra", "diagnostic"],
        "eta": "<3s",
        "artifacts": [],
    }


def _step_attacker_prepare_tools() -> dict:
    script = r'''
            set -euo pipefail
            export DEBIAN_FRONTEND=noninteractive
            unalias rm 2>/dev/null || true

            # Repositório/Keyring (como antes)
            cp /etc/apt/sources.list /etc/apt/sources.list.bak.$(date +%F) || true
            printf "deb [signed-by=/usr/share/keyrings/kali-archive-keyring.gpg] http://http.kali.org/kali kali-rolling main contrib non-free non-free-firmware\n" > /etc/apt/sources.list
            apt-get clean
            rm -rf /var/lib/apt/lists/* || true
            apt-get update || true
            apt-get install -y --reinstall kali-archive-keyring || true
            apt-get update -o Acquire::AllowInsecureRepositories=true || true
            apt-get install -y --reinstall kali-archive-keyring -o APT::Get::AllowUnauthenticated=true || true
            if command -v curl >/dev/null 2>&1 && command -v gpg >/dev/null 2>&1; then
              curl -fsSL https://archive.kali.org/archive-key.asc | gpg --dearmor > /usr/share/keyrings/kali-archive-keyring.gpg || true
            fi

            # Corrige estado quebrado do dpkg se houver
            if dpkg -l 2>/dev/null | awk '/^..r|^..U/ {print $2}' | grep -Eq '^(firebird4\.0-common|libfbclient2|hydra)$'; then
              echo "[attacker] >>> dpkg com pacotes presos; removendo e atualizando Perl…"
              dpkg --remove --force-remove-reinstreq hydra libfbclient2:amd64 firebird4.0-common || true
              apt-get update || true
              apt-get install -y perl-base perl || true
              apt-get -y -f install || true
              apt-get -y full-upgrade || true
            fi

            # Instala base sem travar o dpkg
            apt-get update || true
            apt-get install -y --no-install-recommends netcat-traditional nmap slowhttptest curl jq || true

            # Tenta Hydra (mas não deixa o script falhar se der erro de dpkg/postinst)
            set +e
            apt-get install -y --no-install-recommends hydra
            hydra_ok=$?
            set -e

            if [ $hydra_ok -ne 0 ] || ! command -v hydra >/dev/null 2>&1; then
              echo "[attacker] >>> Hydra indisponível; instalando alternativas (ncrack/patator)…"
              apt-get install -y --no-install-recommends ncrack patator || true
            fi

            echo "[attacker] Ferramentas instaladas/ok:"
            nmap --version | head -1 || true
            if command -v hydra >/dev/null 2>&1; then
              hydra -V 2>/dev/null | head -1 || true
            else
              echo "[MISSING] hydra"
            fi
            slowhttptest -h | head -1 || true
            curl --version | head -1 || true
            jq --version || true
            if command -v ncrack >/dev/null 2>&1; then ncrack --version | head -1 || true; fi
            if command -v patator >/dev/null 2>&1; then patator --help | head -3 | tail -1 || true; fi
        '''
    return {
        "id": "attacker_prepare",
        "title": "Preparar atacante (Kali): keyring + ferramentas",
        "description": "Corrige keyring/repos, previne dpkg quebrado e instala nmap/slowhttptest/curl/jq; tenta Hydra e faz fallback para ncrack/patator.",
        "host": "attacker",
        "script": script,
        "command_normal": _bash_heredoc(script, sudo=True),
        "command_b64": _bash_b64(script, sudo=True),
        "command": _bash_heredoc_sudo_noninteractive(script, strict=True),
        "tags": ["infra", "tools"],
        "eta": "~2-4 min",
        "artifacts": [],
    }



def _step_sensor_prepare_tools() -> dict:
    script = r'''
        set -e
        export DEBIAN_FRONTEND=noninteractive

        echo "[sensor] >>> Atualizando índices e instalando utilitários base…"
        apt-get update || true
        apt-get install -y \
          tcpdump jq curl gnupg ca-certificates apt-transport-https \
          tmux iproute2 net-tools ethtool iftop tshark || true

        echo "[sensor] >>> Configurando repositório do Zeek (OBS, xUbuntu_20.04)…"
        if ! command -v zeek >/dev/null 2>&1 && [ ! -x /opt/zeek/bin/zeek ]; then
          echo 'deb http://download.opensuse.org/repositories/security:/zeek/xUbuntu_20.04/ /' \
            | tee /etc/apt/sources.list.d/security:zeek.list
          curl -fsSL https://download.opensuse.org/repositories/security:zeek/xUbuntu_20.04/Release.key \
            | gpg --dearmor | tee /etc/apt/trusted.gpg.d/security_zeek.gpg > /dev/null
          apt-get update
          apt-get install -y zeek
        fi

        echo "[sensor] >>> Ajustando PATH do Zeek e atalho…"
        if [ -x /opt/zeek/bin/zeek ]; then
          printf 'export PATH=/opt/zeek/bin:$PATH\n' | tee /etc/profile.d/zeek.sh >/dev/null
          chmod +x /etc/profile.d/zeek.sh
          ln -sf /opt/zeek/bin/zeek /usr/local/bin/zeek || true
          . /etc/profile || true
        fi

        echo "[sensor] >>> Verificando versões instaladas…"
        tcpdump --version | head -1 || true
        if command -v zeek >/dev/null 2>&1; then
          zeek --version | head -1 || true
        elif [ -x /opt/zeek/bin/zeek ]; then
          /opt/zeek/bin/zeek --version | head -1 || true
        else
          echo "[MISSING] zeek"
        fi
        tshark -v 2>/dev/null | head -1 || true
        jq --version || true
        curl --version | head -1 || true
    '''
    return {
        "id": "sensor_prepare",
        "title": "Preparar sensor: Zeek (OBS xUbuntu_20.04) + ferramentas",
        "description": "Adiciona o repositório OBS do Zeek, instala Zeek e utilitários (tcpdump, tshark, jq, curl, tmux etc.), ajusta PATH e valida versões.",
        "host": "sensor",
        "script": script,
        "command_normal": _bash_heredoc_sudo_noninteractive(script, strict=True),
        "command_b64": _bash_b64(script, sudo=True),
        "command": _bash_heredoc_sudo_noninteractive(script, strict=True),
        "tags": ["infra", "tools", "sensor", "zeek"],
        "eta": "~2-5 min",
        "artifacts": [],
    }




def _step_attacker_tools_check() -> dict:
    script = r'''
        set -e
        missing=0

        check_ver() {
          bin="$1"
          if command -v "$bin" >/dev/null 2>&1; then
            ($bin --version 2>/dev/null || $bin -h 2>/dev/null || echo "$bin presente") | head -1
          else
            echo "[MISSING] $bin"
            missing=$((missing+1))
          fi
        }

        echo "[attacker] >>> checando ferramentas base:"
        for b in nmap slowhttptest curl jq; do
          check_ver "$b"
        done

        echo "[attacker] >>> checando ferramenta de brute-force:"
        if command -v hydra >/dev/null 2>&1; then
          hydra -V 2>/dev/null | head -1 || true
        elif command -v ncrack >/dev/null 2>&1; then
          ncrack --version 2>/dev/null | head -1 || true
        elif command -v patator >/dev/null 2>&1; then
          patator --help 2>/dev/null | head -3 | tail -1 || true
        else
          echo "[MISSING] hydra/ncrack/patator"
          missing=$((missing+1))
        fi

        if [ $missing -ne 0 ]; then
          echo "[attacker] >>> faltando $missing ferramenta(s)."
          exit $missing
        fi
        echo "[attacker] >>> OK"
    '''
    return {
        "id": "attacker_tools_check",
        "title": "Verificar ferramentas no attacker",
        "description": "Confere nmap/slowhttptest/curl/jq e valida Hydra OU Ncrack OU Patator.",
        "host": "attacker",
        "command_normal": _bash_heredoc(script, sudo=True),
        "command_b64": _bash_b64(script, sudo=True),
        "command": _bash_heredoc(script, sudo=True),
        "tags": ["infra", "verify"],
        "eta": "~5-10s",
        "artifacts": [],
    }


def _step_sensor_tools_check() -> dict:
    script = r'''
        set -e
        missing=0
        check_ver() {
          bin="$1"
          if command -v "$bin" >/dev/null 2>&1; then
            ($bin --version 2>/dev/null || $bin -h 2>/dev/null || echo "$bin presente") | head -1
          else
            echo "[MISSING] $bin"
            missing=$((missing+1))
          fi
        }
        
        echo "[sensor] >>> checando ferramentas:"
        for b in tcpdump jq curl; do
          check_ver "$b"
        done
        
        # Zeek: PATH ou /opt/zeek/bin/zeek
        if command -v zeek >/dev/null 2>&1; then
          zeek --version | head -1 || true
        elif [ -x /opt/zeek/bin/zeek ]; then
          /opt/zeek/bin/zeek --version | head -1 || true
        else
          echo "[MISSING] zeek"
          missing=$((missing+1))
        fi
        
        if [ $missing -ne 0 ]; then
          echo "[sensor] >>> faltando $missing ferramenta(s)."
          exit $missing
        fi
        echo "[sensor] >>> OK"
    '''
    return {
        "id": "sensor_tools_check",
        "title": "Verificar ferramentas no sensor",
        "description": "Confere tcpdump/jq/curl/zeek (também em /opt/zeek/bin) e mostra versões.",
        "host": "sensor",
        "command_normal": _bash_heredoc(script, sudo=True),
        "command_b64": _bash_b64(script, sudo=True),
        "command": _bash_heredoc(script, sudo=True),
        "tags": ["infra", "verify"],
        "eta": "~5-10s",
        "artifacts": [],
    }


def _step_connectivity_check(ips: dict) -> dict:
    victim = ips.get("victim", "192.168.56.20")
    script = f'''
        set -e
        ping -c 2 {victim} || true
        nc -zv {victim} 22 || true
        nc -zv {victim} 80 || true
        echo "[check] conectividade básica testada"
    '''
    return {
        "id": "connectivity",
        "title": "Checagem de conectividade com a vítima",
        "description": "Ping e tentativas de conexão a portas comuns para validar o alvo.",
        "host": "attacker",
        "command_normal": _bash_heredoc(script, sudo=True),
        "command_b64": _bash_b64(script, sudo=True),
        "command": _bash_b64(script, sudo=True),
        "tags": ["infra", "recon"],
        "eta": "<15s",
        "artifacts": [],
    }



def _step_hydra_wordlists(cfg: dict) -> dict:
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

    if not users_inline:
        users_inline = ["admin", "user", "test", "tcc"]
    if not pwds_inline:
        pwds_inline = ["123456", "password", "wrongpass", "tcc2025"]

    users_escaped = "\\n".join(str(u) for u in users_inline)
    pwds_escaped  = "\\n".join(str(p) for p in pwds_inline)

    script = f'''
        set -e
        test -f ~/users.txt     || echo -e "{users_escaped}" | tee ~/users.txt >/dev/null
        test -f ~/passwords.txt || echo -e "{pwds_escaped}" | tee ~/passwords.txt >/dev/null
        wc -l ~/users.txt ~/passwords.txt || true
    '''
    return {
        "id": "hydra_lists",
        "title": "Gerar listas de usuários/senhas (Hydra)",
        "description": "Cria ~/users.txt e ~/passwords.txt (não sobrescreve se já existirem).",
        "host": "attacker",
        "command_normal": _bash_heredoc(script, sudo=True),
        "command_b64": _bash_b64(script, sudo=True),
        "command": _bash_b64(script, sudo=True),
        "tags": ["prep", "credential_access"],
        "eta": "<5s",
        "artifacts": ["~/users.txt", "~/passwords.txt"],
    }



def _step_sensor_capture_show(ips: dict, cfg: dict) -> dict:
    """
    Liga tcpdump + Zeek, força tráfego de teste (ICMP/TCP) e imprime uma amostra dos logs,
    tudo em um único passo — ideal para o Guia do experimento do TCC.
    """
    attacker_ip = ips.get("attacker", "192.168.56.11")
    victim_ip   = ips.get("victim",   "192.168.56.12")

    # Rotação configurável (fallbacks seguros)
    cap = (cfg or {}).get("capture", {}) if isinstance(cfg, dict) else {}
    rotate_sec = int(cap.get("rotate_seconds", 300))
    rotate_mb  = int(cap.get("rotate_size_mb", 100))

    script = f'''
        set -Eeuo pipefail

        log() {{ printf "[sensor] %s\\n" "$*"; }}

        mkdir -p /var/log/pcap /var/log/zeek /var/run/sensor
        chown -R tcpdump:tcpdump /var/log/pcap || true
        chmod 0755 /var/log/pcap /var/log/zeek || true

        attacker="{attacker_ip}"
        victim="{victim_ip}"

        # Descobre a interface: tenta rota p/ vítima, depois atacante; por fim 1ª UP não-loopback
        iface=$(ip route get "$victim" 2>/dev/null | awk '/dev/ {{for(i=1;i<=NF;i++) if($i=="dev"){{print $(i+1); exit}}}}')
        [ -z "${{iface:-}}" ] && iface=$(ip route get "$attacker" 2>/dev/null | awk '/dev/ {{for(i=1;i<=NF;i++) if($i=="dev"){{print $(i+1); exit}}}}')
        [ -z "${{iface:-}}" ] && iface=$(ip -br link | awk '/UP/ && !/LOOPBACK/ {{print $1; exit}}')
        log "usando iface: $iface"

        # Mata processos antigos
        pkill -f "tcpdump -i $iface" 2>/dev/null || true
        pkill -f "zeek -i $iface"    2>/dev/null || true
        sleep 0.3

        # Inicia tcpdump com rotação (-G segundos, -C MB) e queda de privilégios (-Z tcpdump)
        nohup /usr/sbin/tcpdump -i "$iface" -s 0 -U -nn \\
          -w /var/log/pcap/exp_%Y%m%d_%H%M%S.pcap -G {rotate_sec} -C {rotate_mb} -W 48 -Z tcpdump \\
          >/var/log/pcap/tcpdump.out 2>&1 &

        sleep 0.8
        if ! pgrep -fa "tcpdump -i $iface" >/dev/null; then
          log "ERRO ao iniciar tcpdump — últimas linhas:"
          tail -n 60 /var/log/pcap/tcpdump.out || true
          exit 1
        fi

        # Inicia Zeek, fixando o diretório de logs via 'redef' e ignorando checksums (-C)
        ZEEXE=$(command -v zeek || echo /opt/zeek/bin/zeek)
        truncate -s 0 /var/log/zeek/zeek.out || true
        nohup "$ZEEXE" -i "$iface" -C \\
          -e 'redef Log::default_logdir="/var/log/zeek";' \\
          >>/var/log/zeek/zeek.out 2>&1 &
        sleep 1.2
        if ! pgrep -fa "zeek -i $iface" >/dev/null; then
          log "ERRO ao iniciar Zeek — últimas linhas:"
          tail -n 80 /var/log/zeek/zeek.out || true
          exit 1
        fi

        log "processos iniciados com sucesso"

        # --- Tráfego de teste para 'provar' a coleta ---
        (ping -c 6 "$attacker" >/dev/null 2>&1 & ping -c 6 "$victim" >/dev/null 2>&1 &)
        sleep 0.4
        # Gera alguns SYNs em 22/80 se possível
        nc -z -w1 "$victim" 22  >/dev/null 2>&1 || true
        curl -m 2 -s "http://$victim:80/" >/dev/null 2>&1 || true
        nc -z -w1 "$attacker" 22 >/dev/null 2>&1 || true
        sleep 1

        # --- Impressões para o Guia: processos + arquivos + amostras ---
        echo "[health] processos relevantes:"
        pgrep -fa "tcpdump -i" || true
        pgrep -fa "zeek -i"    || true

        echo "[health] últimos arquivos:"
        ls -lh /var/log/pcap/*.pcap 2>/dev/null | tail -n 5 || true
        ls -lh /var/log/zeek/*.log  2>/dev/null | tail -n 10 || true

        echo "[health] amostra do conn.log:"
        if [ -f /var/log/zeek/conn.log ]; then
          grep -m1 '^#fields' /var/log/zeek/conn.log || true
          # imprime somente linhas de dados (sem cabeçalho '#')
          grep -v '^#' /var/log/zeek/conn.log | tail -n 5 | sed -e 's/\\t/ | /g' || true
        else
          echo "[warn] conn.log ainda não criado — gere tráfego e rode novamente o passo."
        fi

        echo "[health] tails dos serviços:"
        tail -n 10 /var/log/pcap/tcpdump.out 2>/dev/null || true
        tail -n 10 /var/log/zeek/zeek.out    2>/dev/null || true
    '''
    return {
        "id": "sensor_capture_show",
        "title": "Ativar captura e exibir provas (PCAP + Zeek)",
        "description": "Liga tcpdump e Zeek, gera tráfego de teste e imprime amostras (conn.log, pcaps, tails) para comprovar a coleta.",
        "host": "sensor",
        "script": script,
        "command_normal": _bash_heredoc_sudo_noninteractive(script, strict=True),
        "command_b64": _bash_b64(script, sudo=True),
        "command": _bash_heredoc_sudo_noninteractive(script, strict=True),
        "tags": ["capture", "healthcheck", "sensor", "zeek"],
        "eta": "<15s",
        "artifacts": ["/var/log/pcap/*.pcap", "/var/log/zeek/conn.log", "/var/log/zeek/zeek.out"],
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
            cmd = f"nmap {flags} -p {ports} -oA {out} {victim_ip}"
            steps.append({
                "id": f"scan_{i}",
                "title": "Varredura de portas e serviços (Nmap)",
                "description": "Descobre portas abertas e versões de serviços no alvo.",
                "host": "attacker",
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
            service = p.get("service", "http-post-form")
            path = p.get("path", "/login")
            extra = p.get("extra", "")
            port = p.get("port")
            out = p.get("output", "~/exp_brute.hydra")
            port_flag = f"-s {port} " if port else ""
            cmd = f"hydra -L {users} -P {pwds} {port_flag}{service}://{victim_ip}{path} {extra} -o {out}"
            steps.append({
                "id": f"brute_{i}",
                "title": "Força bruta de credenciais (Hydra)",
                "description": "Testa combinações de usuário/senha contra o serviço indicado.",
                "host": "attacker",
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
                "description": "Gera carga controlada contra o alvo dentro do lab (NUNCA rode fora do lab).",
                "host": "attacker",
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
                "script": cmd,
                "command_normal": _bash_heredoc(cmd, sudo=False),
                "command_b64": _bash_b64(cmd, sudo=False),
                "command": cmd,
                "tags": ["custom"],
                "eta": a.get("eta", ""),
                "artifacts": a.get("artifacts", []),
            })

    return steps


def parse_yaml_to_steps(yaml_path: str | None, ssh=None) -> list[dict]:
    """
    Gera a lista de passos do Guia.

    IMPORTANTE: Esta função NÃO deve realizar chamadas de rede/SSH.
    Qualquer verificação remota deve ser feita somente na execução do passo.

    - Se yaml_path for None/"" ou o arquivo não existir: entra no modo oficial.
    - Se yaml_path existir e for válido: inclui as ações definidas no YAML.
    """
    try:
        import yaml  # PyYAML
    except Exception as e:
        raise RuntimeError("PyYAML não está instalado (pip install pyyaml).") from e

    # 1) Carregar YAML (ou seguir em modo oficial)
    cfg = {}
    has_yaml = False
    try:
        if yaml_path and Path(yaml_path).exists():
            cfg = yaml.safe_load(Path(yaml_path).read_text(encoding="utf-8")) or {}
            has_yaml = True
            logger.info(f"[Guide] YAML carregado: {yaml_path}")
        else:
            logger.info("[Guide] Modo oficial: nenhuma seleção de YAML detectada (ou arquivo ausente).")
    except Exception as e:
        logger.warning(f"[Guide] Falha ao carregar YAML; seguindo em modo oficial: {e}")
        cfg = {}
        has_yaml = False

    # 2) NÃO use SSH aqui. Forneça IPs padrão imediatos (rápidos para renderizar UI)
    ips = {"attacker": "192.168.56.11", "victim": "192.168.56.12", "sensor": "192.168.56.13"}
    logger.info("[Guide] Parser oficial em modo 'no-SSH' (IPs padrão aplicados).")

    # 3) Montar passos oficiais + (opcional) ações do YAML
    steps: list[dict] = []
    steps.extend(_steps_header())
    steps.append(_step_attacker_sudo_diag())
    steps.append(_step_attacker_prepare_tools())
    steps.append(_step_sensor_prepare_tools())
    steps.append(_step_attacker_tools_check())
    steps.append(_step_sensor_tools_check())
    steps.append(_step_connectivity_check(ips))
    steps.append(_step_sensor_capture_show(ips, cfg))

    if has_yaml:
        try:
            steps.extend(_steps_from_actions(cfg, ips))
        except Exception as e:
            logger.warning(f"[Guide] Falha ao materializar ações do YAML (seguindo com oficiais): {e}")

    return steps
