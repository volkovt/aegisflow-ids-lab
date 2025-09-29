import base64
import os
import shlex
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict
import paramiko
import logging

logger = logging.getLogger("[SSHManager]")

_CONNECT_GATE = threading.BoundedSemaphore(value=2)

LINUX_OS_CMD = r"""
set -e
if [ -r /etc/os-release ]; then
  . /etc/os-release
  printf "Linux: %s %s (id=%s) kernel %s\n" "${{NAME}}" "${{VERSION}}" "${{ID}}" "$(uname -r)"
else
  uname -sr
fi
"""

WIN_OS_CMD = r"""
powershell -NoProfile -Command "$o=Get-CimInstance Win32_OperatingSystem; Write-Output ($o.Caption + ' ' + $o.Version + ' (' + $o.OSArchitecture + ', build ' + $o.BuildNumber + ')')"
"""


def _null_device() -> str:
    return "NUL" if os.name == "nt" else "/dev/null"

def _ensure_shell_preamble(script: str) -> str:
    # Evita duplicar set -e; se já existe, mantém.
    pre = "set -e\n"
    s = script or ""
    s = s.replace("\r\n", "\n")
    return s if s.lstrip().startswith("set -e") else pre + s

def _exec_bash_via_stdin(cli, script: str, timeout: int = 30, want_pty: bool = False, name=""):
    """
    Executa 'script' enviando via STDIN para bash limpo (-se), evitando problemas de quoting.
    """
    try:
        chan_cmd = "bash --noprofile --norc -se"
        stdin, stdout, stderr = cli.exec_command(chan_cmd, get_pty=want_pty, timeout=timeout)
        safe_script = _ensure_shell_preamble(script)
        stdin.write(safe_script)
        try:
            stdin.flush()
        except Exception:
            pass
        try:
            stdin.channel.shutdown_write()
        except Exception:
            pass

        out = stdout.read().decode(errors="ignore")
        err = stderr.read().decode(errors="ignore")
        rc = stdout.channel.recv_exit_status()
        if rc != 0:
            raise RuntimeError(f"Remote exit status {rc}: {err.strip() or out.strip()}")
        return out or ""
    except Exception as e:
        logger.error(f"-----------------------------------------------------")
        logger.error(f"[SSHManager] Executando comando em '{name}': {script}")
        logger.error(f"[SSHManager] _exec_bash_via_stdin falhou: {e}")
        logger.error(f"-----------------------------------------------------")
        raise

class SSHManager:
    def __init__(self, lab_dir: Path):
        self.lab_dir = lab_dir
        self._lock = threading.Lock()
        self._running = {}

        self._pool: Dict[str, paramiko.SSHClient] = {}
        self._pool_meta: Dict[str, dict] = {}
        self._pool_lock = threading.Lock()

        self._chan_sems: Dict[str, threading.BoundedSemaphore] = {}

    def _get_chan_sem(self, name: str) -> threading.BoundedSemaphore:
        try:
            with self._pool_lock:
                sem = self._chan_sems.get(name)
                if sem is None:
                    sem = threading.BoundedSemaphore(value=1)
                    self._chan_sems[name] = sem
            return sem
        except Exception as e:
            logger.error(f"[SSHManager] _get_chan_sem falhou: {e}")
            return threading.BoundedSemaphore(value=1)

    def _purge_client(self, name: str):
        try:
            with self._pool_lock:
                cli = self._pool.pop(name, None)
                self._pool_meta.pop(name, None)
            if cli:
                try:
                    cli.close()
                except Exception:
                    pass
            logger.warn(f"[SSHManager] Pool: conexão de '{name}' removida.")
        except Exception as e:
            logger.error(f"[SSHManager] Falha ao purgar cliente '{name}': {e}")

    def _get_client(self, name: str, timeout: int = 30) -> paramiko.SSHClient:
        """
        Retorna um SSHClient conectado e reutilizável para a VM `name`.
        Reabre se a conexão caiu. Aplica keepalive para manter viva.
        """
        with self._pool_lock:
            cli = self._pool.get(name)
        if cli:
            try:
                tr = cli.get_transport()
                if tr and tr.is_active() and tr.is_authenticated():
                    with self._pool_lock:
                        self._pool_meta[name]["last_used"] = time.time()
                    return cli
            except Exception:
                self._purge_client(name)

        f = self.get_ssh_fields(name)
        host, port, user, key_path = f["HostName"], int(f["Port"]), f["User"], f["IdentityFile"]

        self._wait_port(host, port, wait_secs=min(20, timeout + 5))
        self._wait_ssh_banner(host, port, wait_secs=min(40, timeout + 20))

        with _CONNECT_GATE:
            cli = paramiko.SSHClient()
            cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                cli.connect(
                    hostname=host, port=port, username=user,
                    key_filename=key_path, look_for_keys=False, allow_agent=False,
                    timeout=max(20, timeout + 10)
                )
                tr = cli.get_transport()
                if tr:
                    tr.set_keepalive(15)
                with self._pool_lock:
                    self._pool[name] = cli
                    self._pool_meta[name] = {"created": time.time(), "last_used": time.time()}
                logger.info(f"[SSHManager] Pool: nova conexão aberta para '{name}' ({user}@{host}:{port}).")
                return cli
            except Exception as e:
                try:
                    cli.close()
                except Exception:
                    pass
                logger.error(f"[SSHManager] Falha abrindo conexão persistente em '{name}': {e}")
                raise

    def _register_proc(self, name, proc: subprocess.Popen):
        with self._lock:
            self._running.setdefault(name, []).append(proc)

    def _unregister_proc(self, name, proc: subprocess.Popen):
        with self._lock:
            if name in self._running and proc in self._running[name]:
                self._running[name].remove(proc)

    def cancel_all_running(self):
        with self._lock:
            items = list(self._running.items())
        for name, procs in items:
            for p in list(procs):
                try:
                    logger.error(f"[SSHManager] Matando vagrant ssh ativo em {name} (cancel).")
                    if os.name == "nt":
                        p.send_signal(signal.CTRL_BREAK_EVENT)
                        time.sleep(0.2)
                        p.terminate()
                    else:
                        os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                    p.wait(timeout=5)
                except Exception as e:
                    logger.error(f"[SSHManager] Falha ao matar processo SSH ({name}): {e}")

    def _parse_ssh_config(self, ssh_config: str) -> Dict[str, str]:
        """
        Parser robusto para saída do `vagrant ssh-config`. Não depende de regex frágil.
        Extrai: HostName, Port, User, IdentityFile.
        """
        fields = {}
        wanted = {"HostName", "Port", "User", "IdentityFile"}
        for raw in ssh_config.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or " " not in line:
                continue
            k, v = line.split(None, 1)
            if k in wanted:
                fields[k] = v.strip().strip('"')
        return fields

    def get_ssh_fields(self, name: str, timeout: int = 15) -> Dict[str, str]:
        """
        Lê 'vagrant ssh-config <name>' e retorna um dict sanitizado com:
          - HostName (str)
          - Port (str; numérica)
          - User (str)
          - IdentityFile (str; caminho absoluto, sem aspas, com ~ expandido)

        Pensado para o TCC (detecção de anomalias): mensagens de erro claras e
        campos normalizados para logar endpoints (Host:Port) com rastreabilidade.
        """
        if not name or not isinstance(name, str):
            logger.error("[SSHManager] Nome da VM inválido (vazio ou não-string).")
            raise ValueError("Nome da VM inválido.")

        try:

            proc = subprocess.run(
                ["vagrant", "ssh-config", name],
                cwd=self.lab_dir,
                capture_output=True, text=True, timeout=45
            )
            if proc.returncode != 0 or not proc.stdout.strip():
                for tmo in (12, 24):
                    time.sleep(3)
                    proc = subprocess.run(
                        ["vagrant", "ssh-config", name],
                        cwd=self.lab_dir,
                        capture_output=True, text=True, timeout=45
                    )
                    if proc.returncode == 0 and proc.stdout.strip():
                        break
        except FileNotFoundError as e:
            logger.error("[SSHManager] Vagrant não encontrado no PATH. Instale/configure o Vagrant.", exc_info=True)
            raise RuntimeError(
                "Vagrant não encontrado. Instale-o e/ou adicione ao PATH para executar 'vagrant ssh-config'."
            ) from e
        except subprocess.TimeoutExpired as e:
            logger.error(f"[SSHManager] Timeout ao executar 'vagrant ssh-config {name}' ({timeout}s).", exc_info=True)
            raise RuntimeError(
                f"Timeout ao obter ssh-config da VM '{name}'. Verifique se ela está responsiva: vagrant status {name}"
            ) from e
        except Exception as e:
            logger.error(f"[SSHManager] Falha inesperada ao chamar 'vagrant ssh-config {name}'.", exc_info=True)
            raise RuntimeError(
                f"Erro ao executar 'vagrant ssh-config {name}'. Verifique o ambiente do Vagrant."
            ) from e

        if proc.returncode != 0 or not (proc.stdout or "").strip():
            stdout = (proc.stdout or "").strip()
            stderr = (proc.stderr or "").strip()
            logger.error(
                f"[SSHManager] 'vagrant ssh-config {name}' retornou código {proc.returncode}. "
                f"STDOUT: {stdout or '—'} | STDERR: {stderr or '—'}"
            )
            raise RuntimeError(
                f"Não foi possível obter ssh-config para '{name}'. "
                f"Garanta que a VM existe/está ativa: vagrant up {name}"
            )

        ssh_conf = proc.stdout
        f = self._parse_ssh_config(ssh_conf)

        required = ("HostName", "Port", "User", "IdentityFile")
        missing = [k for k in required if k not in f or str(f[k]).strip() == ""]
        if missing:
            logger.error(f"[SSHManager] ssh-config de '{name}' incompleto. Faltando: {missing}")
            raise RuntimeError(
                f"ssh-config incompleto para '{name}' (faltando: {', '.join(missing)}). "
                f"Execute: vagrant up {name} e depois vagrant ssh-config {name}."
            )

        hostname = str(f["HostName"]).strip().strip('"').strip()
        user = str(f["User"]).strip().strip('"').strip()

        port_raw = str(f["Port"]).strip().strip('"').strip()
        if not port_raw.isdigit():
            logger.error(f"[SSHManager] Porta inválida em ssh-config de '{name}': {port_raw!r}")
            raise RuntimeError(f"Porta inválida no ssh-config de '{name}': {port_raw!r}")
        port = port_raw
        identity_raw = str(f["IdentityFile"]).strip()
        identity_clean = identity_raw.strip().strip('"').strip("'")
        identity_path = str(Path(identity_clean).expanduser())

        result = {
            "HostName": hostname,
            "Port": port,
            "User": user,
            "IdentityFile": identity_path,
        }

        # logger.info(
        #     f"[SSHManager] ssh-config sanitizado para '{name}': "
        #     f"{result['User']}@{result['HostName']}:{result['Port']} key={result['IdentityFile']}"
        # )
        return result

    def open_external_terminal(self, name: str, tmux_session: str | None = None):
        """
        Abre um terminal externo conectado em SSH. Se `tmux_session` for informado,
        a conexão já entra em um tmux persistente no host remoto.
        Retorna o objeto subprocess.Popen do terminal criado (quando aplicável).
        """
        try:
            f = self.get_ssh_fields(name)
            host = f["HostName"]
            port = f["Port"]
            user = f["User"]
            key = f["IdentityFile"]

            known_hosts = _null_device()
            base = (
                f'ssh -p {port} -i "{key}" {user}@{host} '
                f'-o StrictHostKeyChecking=no -o UserKnownHostsFile={known_hosts}'
            )

            if tmux_session:
                # -A: anexa se existir, cria se não existir
                remote = f'"bash -lc \'tmux new-session -A -s {tmux_session}\'"'
                ssh_cmd = f'{base} -t {remote}'
            else:
                ssh_cmd = base

            logger.warning(f"[SSHManager] Comando SSH para terminal externo: {ssh_cmd}")
            logger.warning(f"[SSHManager] Diretório do laboratório: {self.lab_dir}")

            proc = None

            if os.name == "nt":
                creation = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
                wt = shutil.which("wt.exe")
                if wt:
                    args = [wt, "new-tab", "-d", str(self.lab_dir), "cmd.exe", "/k", ssh_cmd]
                    logger.warning(f"[SSHManager] Abrindo terminal externo (Win/WT): {' '.join(args)}")
                    proc = subprocess.Popen(args, creationflags=creation)
                else:
                    args = ["cmd.exe", "/k", ssh_cmd]
                    logger.warning(f"[SSHManager] Abrindo terminal externo (Win/CMD): {' '.join(args)}")
                    proc = subprocess.Popen(args, cwd=str(self.lab_dir), creationflags=creation)
            else:
                term = (shutil.which("x-terminal-emulator")
                        or shutil.which("gnome-terminal")
                        or shutil.which("konsole")
                        or shutil.which("xterm"))
                if term and "gnome-terminal" in term:
                    args = [term, "--", "bash", "-lc", ssh_cmd]
                elif term and "konsole" in term:
                    args = [term, "-e", f"bash -lc '{ssh_cmd}'"]
                elif term:
                    args = [term, "-e", f"bash -lc '{ssh_cmd}'"]
                else:
                    args = ["bash", "-lc", ssh_cmd]
                logger.warning(f"[SSHManager] Abrindo terminal externo (Unix): {' '.join(args)}")
                proc = subprocess.Popen(args, cwd=str(self.lab_dir))

            return proc
        except Exception as e:
            logger.error(f"[SSHManager] Falha ao abrir terminal externo: {e}")
            raise

    def _wait_port(self, host: str, port: int, wait_secs: float = 10.0) -> None:
        deadline = time.time() + max(0.5, wait_secs)
        last_err = None
        while time.time() < deadline:
            try:
                with socket.create_connection((host, port), timeout=2.0):
                    return
            except OSError as e:
                last_err = e
                time.sleep(0.25)
        raise TimeoutError(f"Porta {host}:{port} indisponível: {last_err}")

    def _wait_ssh_banner(self, host: str, port: int, wait_secs: float = 20.0) -> None:
        """
        Abre um socket e tenta ler o prefixo 'SSH-' do banner.
        Só retorna quando o serviço SSH está realmente pronto para handshake.
        """
        deadline = time.time() + max(1.0, wait_secs)
        last_err = None
        while time.time() < deadline:
            try:
                with socket.create_connection((host, port), timeout=3.0) as s:
                    s.settimeout(3.0)
                    data = s.recv(64)
                    if data and data.startswith(b"SSH-"):
                        logger.info(f"[SSHManager] Banner ok em {host}:{port}: {data.decode(errors='ignore').strip()}")
                        return
                    last_err = RuntimeError(f"Banner inválido: {data!r}")
            except Exception as e:
                last_err = e
            time.sleep(0.4)
        raise TimeoutError(f"Banner SSH não disponível em {host}:{port}: {last_err}")

    def run_command_cancellable(self, name: str, cmd: str, timeout_s: int = 300):
        """
        Executa 'vagrant ssh name -c "<wrapped>"' de forma cancelável, usando shell limpo.
        """
        self.run_command(name, cmd, timeout=timeout_s)
        return
        # try:
        #     wrapped = _wrap_no_rc_shell((cmd or "").replace("\r\n", "\n"))
        #     ssh_cmd = ["vagrant", "ssh", name, "-c", wrapped]
        #
        #     creationflags = 0
        #     preexec_fn = None
        #     if os.name != "nt":
        #         preexec_fn = os.setsid
        #     else:
        #         creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        #
        #     proc = subprocess.Popen(
        #         ssh_cmd, cwd=self.lab_dir,
        #         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        #         preexec_fn=preexec_fn, creationflags=creationflags
        #     )
        #     self._register_proc(name, proc)
        #     out, err = proc.communicate(timeout=timeout_s)
        #     rc = proc.returncode
        #     self._unregister_proc(name, proc)
        #     if rc != 0:
        #         raise RuntimeError(f"Remote exit status {rc}: {err.strip() or out.strip()}")
        #     return out
        # except subprocess.TimeoutExpired:
        #     self.cancel_all_running()

    def run_command(self, name: str, command: str, timeout: int = 15, retries: int = 5) -> str:
        """
        Executa 'command' em bash limpo via STDIN, reaproveitando conexão persistente.
        - Serializa abertura de canais por VM para evitar 'Timeout opening channel'.
        - Se o canal falhar, purga o client e reconecta na próxima tentativa.
        """
        raw_cmd = (command or "").replace("\r\n", "\n")
        want_pty = ("<<'__EOF__'" in raw_cmd) or ('<<"__EOF__"' in raw_cmd) or ('<<__EOF__' in raw_cmd)

        last_exc = None
        open_timeout = max(45, timeout + 15)  # abertura de canal um pouco mais folgada

        for attempt in range(1, retries + 1):
            sem = self._get_chan_sem(name)
            with sem:
                try:
                    cli = self._get_client(name, timeout=timeout)
                    out = _exec_bash_via_stdin(
                        cli,
                        raw_cmd,
                        timeout=open_timeout,
                        want_pty=want_pty,
                        name=name
                    )
                    with self._pool_lock:
                        if name in self._pool_meta:
                            self._pool_meta[name]["last_used"] = time.time()
                    return out or ""

                except Exception as e:
                    last_exc = e
                    msg = str(e)
                    # Sinais clássicos de falha no canal/sessão
                    transient = (
                            "Timeout opening channel" in msg or
                            "Channel closed" in msg or
                            "No existing session" in msg or
                            "channel open failure" in msg
                    )

                    if transient:
                        logger.warning(
                            f"[SSHManager] Canal falhou em '{name}' (tentativa {attempt}/{retries}): "
                            f"{msg}. Forçando reconexão antes do retry."
                        )
                        try:
                            self._purge_client(name)
                        except Exception:
                            pass
                    else:
                        logger.warning(
                            f"[SSHManager] Tentativa {attempt}/{retries} falhou em {name}: {msg}"
                        )

                    sleep_s = 0.8 * attempt
                    time.sleep(sleep_s)

        # Fallback via vagrant ssh -c (STDIN base64 → bash -se)
        try:
            logger.info(f"[SSHManager] Fallback via 'vagrant ssh -c' em {name}")
            payload = _ensure_shell_preamble(raw_cmd).encode("utf-8")
            b64 = base64.b64encode(payload).decode("ascii")

            remote = (
                    "bash --noprofile --norc -lc "
                    + shlex.quote(f"echo {b64} | base64 -d | bash --noprofile --norc -se")
            )

            proc = subprocess.run(
                ["vagrant", "ssh", name, "-c", remote],
                cwd=self.lab_dir,
                text=True,
                capture_output=True,
                timeout=max(90, timeout + 60),
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"vagrant ssh retornou {proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}"
                )
            return (proc.stdout or "").rstrip()

        except Exception as e2:
            logger.error(f"[SSHManager] Erro no fallback 'vagrant ssh -c' em {name}: {e2}")
            logger.error(f"[SSHManager] Erro ao executar comando em {name}: {last_exc}")
            raise RuntimeError(f"SSH falhou em {name}: {last_exc}") from e2

    def get_ssh_fields_safe(self, name: str) -> dict:
        try:
            return self.get_ssh_fields(name)
        except Exception as e:
            logger.warning(f"[SSHManager] get_ssh_fields_safe({name}) falhou: {e}")
            return {}

    def probe_os(self, name: str, timeout: int = 20) -> str:
        """
        Detecção de SO focada no lab (Linux), sem usar ${VAR} para evitar colisão com .format(...).
        - Se Linux: lê PRETTY_NAME de /etc/os-release via awk e imprime também o kernel.
        - Fallbacks neutros se /etc/os-release não existir.
        """
        try:
            logger.info(f"[SOProbe] {name}: checando Linux via uname -s")
            script = r"""
                set -e
                # Detecta Linux
                if command -v uname >/dev/null 2>&1 && [ "$(uname -s 2>/dev/null)" = "Linux" ]; then
                    pretty="$(awk -F= '/^PRETTY_NAME=/{gsub(/"/,"",$2);print $2}' /etc/os-release 2>/dev/null || true)"
                    if [ -z "$pretty" ]; then
                        pretty="$(uname -sr 2>/dev/null || echo Linux)"
                    fi
                    printf "Linux: %s kernel %s\n" "$pretty" "$(uname -r)"
                    exit 0
                fi

                # Fallbacks neutros (se um dia tiver Windows, trate fora deste canal bash)
                if [ -f /proc/sys/kernel/ostype ] && grep -qi linux /proc/sys/kernel/ostype 2>/dev/null; then
                    printf "Linux: %s\n" "$(uname -sr 2>/dev/null || cat /proc/version 2>/dev/null || echo 'kernel unknown')"
                    exit 0
                fi
                echo unknown
            """
            out = self.run_command(name, script, timeout=timeout) or ""
            s = out.strip() or "unknown"
            logger.info(f"[SO] {name}: {s}")
            return s
        except Exception as e:
            logger.error(f"[SOProbe] {name}: falha inesperada: {e}", exc_info=True)
            return "error"

    def close_all(self):
        try:
            with self._pool_lock:
                names = list(self._pool.keys())
            for n in names:
                self._purge_client(n)
            logger.info("[SSHManager] Pool: todas as conexões encerradas.")
        except Exception as e:
            logger.error(f"[SSHManager] close_all falhou: {e}")


def _wrap_no_rc_shell(cmd: str) -> str:
    """
    Executa o comando sempre em bash limpo (sem profile/rc), sem fallback para /bin/sh.
    Isso evita erros como: 'set: Illegal option -o pipefail'.
    """
    try:
        c = (cmd or "").replace("\r\n", "\n")
        q = shlex.quote(c)
        return f"/bin/bash --noprofile --norc -lc {q}"
    except Exception as e:
        logger.error(f"[SSHManager] _wrap_no_rc_shell falhou: {e}", exc_info=True)
        return cmd or ""
