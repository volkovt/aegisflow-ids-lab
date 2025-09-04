import re
import socket
import subprocess
import time
from pathlib import Path
from typing import Optional, Dict
import paramiko
import logging

logger = logging.getLogger("[SSHManager]")

class SSHManager:
    def __init__(self, lab_dir: Path):
        self.lab_dir = lab_dir

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

    def open_external_terminal(self, name: str) -> None:
        try:
            f = self.get_ssh_fields(name)
            cmd = [
                "cmd.exe", "/c",
                f"start cmd.exe /k ssh -p {f['Port']} -i {f['IdentityFile']} "
                f"{f['User']}@{f['HostName']} -o StrictHostKeyChecking=no"
            ]
            logging.getLogger("[SSHManager]").info(f"Abrindo terminal externo: {' '.join(cmd)}")
            subprocess.Popen(cmd, cwd=self.lab_dir)
        except Exception as e:
            logger.error(f"[SSHManager] Falha ao abrir terminal externo: {e}")
            raise

    def _wait_port(self, host: str, port: int, wait_secs: float = 6.0) -> None:
        """
        Aguarda o socket aceitar conexão (sem fazer handshake SSH ainda).
        """
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

    def run_command(
        self,
        name: str,
        command: str,
        timeout: int = 12,
        retries: int = 3,
        backoff: float = 0.8
    ) -> str:
        """
        Executa um comando via SSH com retentativas. Mitiga erro:
        'unpack requires a buffer of 4 bytes' (handshake incompleto).
        """
        f = self.get_ssh_fields(name)
        host = f.get("HostName")
        port = int(f.get("Port", 22))
        user = f.get("User", "vagrant")
        key_path = f.get("IdentityFile")

        last_exc = None
        for attempt in range(1, retries + 1):
            try:
                self._wait_port(host, port, wait_secs=min(6.0, timeout))

                pkey = None
                try:
                    pkey = paramiko.RSAKey.from_private_key_file(key_path)
                except Exception as e_key:
                    logger.warning(f"[SSHManager] Falha lendo chave RSA: {e_key}; tentando Ed25519…")
                    try:
                        pkey = paramiko.Ed25519Key.from_private_key_file(key_path)
                    except Exception as e_ed:
                        logger.error(f"[SSHManager] Falha chave SSH: {e_ed}")
                        raise

                client = paramiko.SSHClient()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

                client.connect(
                    hostname=host,
                    port=port,
                    username=user,
                    pkey=pkey,
                    look_for_keys=False,
                    allow_agent=False,
                    timeout=6,            # TCP connect timeout
                    banner_timeout=8,     # banner SSH
                    auth_timeout=8        # auth timeout
                )

                try:
                    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
                    out = (stdout.read() or b"").decode(errors="replace") + \
                          (stderr.read() or b"").decode(errors="replace")
                    logger.info(f"[SSHManager] Comando executado em {name}: {command}")
                    return out
                finally:
                    client.close()

            except Exception as e:
                last_exc = e
                logger.warning(
                    f"[SSHManager] Tentativa {attempt}/{retries} falhou em {name}: {e}"
                )
                time.sleep(backoff * attempt)

        logger.error(f"[SSHManager] Erro ao executar comando em {name}: {last_exc}")
        raise RuntimeError(f"SSH falhou em {name}: {last_exc}")

    def get_ssh_fields_safe(self, name: str) -> Dict[str, str]:
        """Helper usado pela UI para exibir Host:Port mesmo que Paramiko falhe."""
        return self.get_ssh_fields(name)