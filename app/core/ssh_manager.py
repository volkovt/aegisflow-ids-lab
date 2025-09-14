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

def _null_device() -> str:
    return "NUL" if os.name == "nt" else "/dev/null"

class SSHManager:
    def __init__(self, lab_dir: Path):
        self.lab_dir = lab_dir
        self._lock = threading.Lock()
        self._running = {}

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
                text=True,
                capture_output=True,
                timeout=timeout
            )
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

        logger.info(
            f"[SSHManager] ssh-config sanitizado para '{name}': "
            f"{result['User']}@{result['HostName']}:{result['Port']} key={result['IdentityFile']}"
        )
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
        Executa 'vagrant ssh name -c "bash -lc <cmd>"' de forma cancelável.
        """
        try:
            payload = base64.b64encode(cmd.encode("utf-8")).decode("ascii")
            wrapped = f'bash -lc "eval \\\"$(echo {payload} | base64 -d)\\\""'
            ssh_cmd = ["vagrant", "ssh", name, "-c", wrapped]
            creationflags = 0
            preexec_fn = None
            if os.name != "nt":
                preexec_fn = os.setsid
            else:
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

            proc = subprocess.Popen(
                ssh_cmd, cwd=self.lab_dir,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                preexec_fn=preexec_fn, creationflags=creationflags
            )
            self._register_proc(name, proc)
            out, err = proc.communicate(timeout=timeout_s)
            rc = proc.returncode
            self._unregister_proc(name, proc)
            if rc != 0:
                raise RuntimeError(f"Remote exit status {rc}: {err.strip() or out.strip()}")
            return out
        except subprocess.TimeoutExpired:
            self.cancel_all_running()

    def run_command(self, name: str, command: str, timeout: int = 15, retries: int = 5) -> str:
        """
        Executa 'command' via SSH (Paramiko) checando código de saída.
        Se esgotar as tentativas, cai em fallback 'vagrant ssh -c' (somente se não for heredoc).
        """
        f = self.get_ssh_fields(name)
        host, port, user, key_path = f["HostName"], int(f["Port"]), f["User"], f["IdentityFile"]

        def _needs_pty(cmd: str) -> bool:
            t = cmd or ""
            return ("<<'__EOF__'" in t) or ('<<"__EOF__"' in t) or ('<<__EOF__' in t) or ("\n__EOF__" in t) or (
                        len(t) > 8000)

        last = None
        cmd = (command or "").replace("\r\n", "\n")  # normaliza CRLF do Windows
        want_pty = _needs_pty(cmd)

        for attempt in range(1, retries + 1):
            try:
                self._wait_port(host, port, wait_secs=min(20, timeout + 5))
                self._wait_ssh_banner(host, port, wait_secs=min(40, timeout + 20))

                with _CONNECT_GATE:
                    client = paramiko.SSHClient()
                    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    client.connect(
                        hostname=host, port=port, username=user,
                        key_filename=key_path, look_for_keys=False, allow_agent=False,
                        timeout=max(20, timeout + 10)
                    )

                    # IMPORTANTE: heredoc/longos pedem PTY para não fechar o canal (evita 255)
                    stdin, stdout, stderr = client.exec_command(cmd, get_pty=want_pty, timeout=timeout)

                    out = stdout.read().decode(errors="ignore")
                    err = stderr.read().decode(errors="ignore")
                    rc = stdout.channel.recv_exit_status()

                    try:
                        client.close()
                    except Exception:
                        pass

                    if rc != 0:
                        raise RuntimeError(f"Remote exit status {rc}: {err.strip() or out.strip()}")
                    return out
            except Exception as e:
                last = e
                sleep_s = 0.8 * attempt
                logger.warning(
                    f"[SSHManager] Tentativa {attempt}/{retries} falhou em {name}: {e}. Retry em {sleep_s:.1f}s")
                time.sleep(sleep_s)

        # Fallback só é seguro quando NÃO é heredoc (evita a ‘sopa de aspas’ no Windows)
        if not _needs_pty(cmd):
            try:
                logger.info(f"[SSHManager] Fallback via 'vagrant ssh -c' em {name}")
                proc = subprocess.run(
                    ["vagrant", "ssh", name, "-c", cmd],
                    cwd=self.lab_dir, text=True, capture_output=True, timeout=max(60, timeout + 30)
                )
                if proc.stdout:
                    logger.info(proc.stdout.strip())
                if proc.returncode != 0:
                    raise RuntimeError(
                        f"vagrant ssh retornou {proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}")
                return (proc.stdout or "").rstrip()
            except Exception as e2:
                logger.error(f"[SSHManager] Erro no fallback 'vagrant ssh -c' em {name}: {e2}")

        # Se chegou aqui, reporte a falha original (Paramiko)
        logger.error(f"[SSHManager] Erro ao executar comando em {name}: {last}")
        raise RuntimeError(f"SSH falhou em {name}: {last}")

    def get_ssh_fields_safe(self, name: str) -> dict:
        try:
            return self.get_ssh_fields(name)
        except Exception as e:
            logger.warning(f"[SSHManager] get_ssh_fields_safe({name}) falhou: {e}")
            return {}