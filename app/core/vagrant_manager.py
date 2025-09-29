import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable, Optional
from jinja2 import Environment, FileSystemLoader
import logging

logger = logging.getLogger("[VagrantManager]")

class VagrantManager:
    def __init__(self, project_root: Path, lab_dir: Path):
        self.project_root = project_root
        self.lab_dir = lab_dir
        self.lab_dir.mkdir(parents=True, exist_ok=True)

        self._ssh_ready_until: dict[str, float] = {}

    def _run(self, args: list[str]) -> Iterable[str]:
        """Executa comando do Vagrant emitindo logs por linha."""
        try:
            logger.info(f"[Vagrant] Executando: {' '.join(args)} (cwd={self.lab_dir})")
            proc = subprocess.Popen(
                args,
                cwd=self.lab_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                yield line.rstrip()
            proc.wait()
            if proc.returncode != 0:
                raise RuntimeError(f"Comando falhou (rc={proc.returncode}): {' '.join(args)}")
            logger.info("[Vagrant] Comando finalizado com sucesso")
        except Exception as e:
            logger.error(f"[Vagrant] Erro: {e}")
            raise

    def ensure_vagrantfile(self, template_dir: Path, ctx: dict, force: bool = False) -> Path:
        """
        Garante que o Vagrantfile exista.
        - Se não existir OU force=True -> (re)gera a partir do template Jinja2.
        - Caso contrário, apenas retorna o path existente.
        """
        try:
            vf = self.lab_dir / "Vagrantfile"
            if not vf.exists() or force:
                logger.info("[Vagrant] Vagrantfile %s — gerando a partir do template...",
                            "ausente" if not vf.exists() else "forçado")
                return self.write_vagrantfile(template_dir, ctx)
            logger.info(f"[Vagrant] Vagrantfile já existe: {vf}")
            return vf
        except Exception as e:
            logger.error(f"[Vagrant] Falha ao garantir Vagrantfile: {e}")
            raise

    def ensure_vagrantfile_synced(self, template_dir: Path, ctx: dict) -> tuple[Path, str, bool]:
        """
        Gera/atualiza o Vagrantfile apenas se houver mudança no template ou no contexto.
        Retorna (caminho_do_vagrantfile, hash, mudou_bool).
        """
        try:
            lab_dir = Path(self.lab_dir)
            lab_dir.mkdir(parents=True, exist_ok=True)

            tpl_hash = _dir_sha256(Path(template_dir))
            ctx_json = json.dumps(ctx, sort_keys=True, ensure_ascii=False).encode("utf-8")
            fp = hashlib.sha256(tpl_hash.encode() + ctx_json).hexdigest()

            fp_file = lab_dir / ".lab" / "Vagrantfile.hash"
            fp_file.parent.mkdir(parents=True, exist_ok=True)
            old = fp_file.read_text(encoding="utf-8").strip() if fp_file.exists() else ""

            changed = (fp != old)

            if changed:
                logger.info(f"[Vagrantfile] mudanças detectadas (hash {old[:8]}→{fp[:8]}). Gerando…")
                vf_path = self.write_vagrantfile(template_dir, ctx)
                fp_file.write_text(fp, encoding="utf-8")
                logger.info(f"[Vagrantfile] atualizado em {vf_path} (hash {fp[:8]}).")
                return vf_path, fp, True
            else:
                vf_path = lab_dir / "Vagrantfile"
                logger.info(f"[Vagrantfile] inalterado (hash {fp[:8]}).")
                return vf_path, fp, False
        except Exception as e:
            logger.error(f"[Vagrantfile] falha ao sincronizar: {e}")
            raise

    def ensure_created_and_running(
        self,
        name: str,
        template_dir: Path,
        ctx: dict,
        attempts: int = 10,
        delay_s: int = 3
    ) -> Iterable[str]:
        """
        Garante que a VM exista e fique 'running' com SSH pronto.
        Streama o output do 'vagrant up' via yield.
        """
        try:
            state = self.status_by_name(name) or "unknown"
        except Exception as e:
            logger.error(f"[Vagrant] status_by_name({name}) falhou: {e}")
            state = "unknown"

        if state in ("not_created", "pre_transient", "unknown"):
            self.ensure_vagrantfile(template_dir, ctx)
            yield f"[Create] {name} não existe. Executando 'vagrant up' (pode baixar a box na 1ª vez)…"
            for ln in self.up(name):
                yield ln
        elif state in ("poweroff", "aborted", "saved"):
            yield f"[Up] {name} em '{state}'. Executando 'vagrant up'…"
            for ln in self.up(name):
                yield ln
        elif state == "running":
            yield f"[Skip] {name} já está em 'running'."
        else:
            yield f"[Up] {name} em estado '{state}'. Tentando 'vagrant up' mesmo assim…"
            for ln in self.up(name):
                yield ln

        try:
            self.wait_ssh_ready(name, str(self.lab_dir), attempts=attempts, delay_s=delay_s)
            yield f"[Preflight] {name}: SSH pronto."
        except Exception as e:
            yield f"[Preflight] {name}: SSH não respondeu dentro do tempo: {e}"
            raise

    def write_vagrantfile(self, template_dir: Path, ctx: dict) -> Path:
        try:
            env = Environment(loader=FileSystemLoader(str(template_dir)))
            tpl = env.get_template("Vagrantfile.j2")
            vf_content = tpl.render(**ctx)
            vf_path = self.lab_dir / "Vagrantfile"
            vf_path.write_text(vf_content, encoding="utf-8")
            logger.info(f"[Vagrant] Vagrantfile gerado: {vf_path}")
            return vf_path
        except Exception as e:
            logger.error(f"[Vagrant] Falha ao gerar Vagrantfile: {e}")
            raise

    def up(self, name: Optional[str] = None) -> Iterable[str]:
        cmd = ["vagrant", "up"] + ([name] if name else [])
        return self._run(cmd)

    def halt(self, name: Optional[str] = None) -> Iterable[str]:
        cmd = ["vagrant", "halt"] + ([name] if name else [])
        try:
            for ln in self._run(cmd):
                yield ln
        finally:
            if name:
                self._ssh_ready_until.pop(name, None)

    def destroy(self, name: Optional[str] = None) -> Iterable[str]:
        cmd = ["vagrant", "destroy", "-f"] + ([name] if name else [])
        try:
            for ln in self._run(cmd):
                yield ln
        finally:
            if name:
                self._ssh_ready_until.pop(name, None)

    def status(self) -> str:
        try:
            out = subprocess.check_output(["vagrant", "status"], cwd=self.lab_dir, text=True)
            logger.info("[Vagrant] status consultado")
            return out
        except subprocess.CalledProcessError as e:
            logger.error(f"[Vagrant] status erro: {e}")
            return e.output or str(e)

    def status_by_name(self, name: str) -> Optional[str]:
        try:
            out = self.status()
            for line in out.splitlines():
                L = line.strip()
                if not L or not L.lower().startswith(name.lower()):
                    continue
                low = L.lower()
                if "running" in low:
                    return "running"
                if "poweroff" in low or "shutoff" in low:
                    return "poweroff"
                if "not created" in low:
                    return "not_created"
                if "aborted" in low:
                    return "aborted"
                if "saved" in low:
                    return "saved"
                return "unknown"
            return None
        except Exception as e:
            logger.error(f"[Vagrant] status_by_name erro: {e}")
            return None

    def ssh_config(self, name: str) -> str:
        try:
            out = subprocess.check_output(["vagrant", "ssh-config", name], cwd=self.lab_dir, text=True)
            return out
        except subprocess.CalledProcessError as e:
            logger.error(f"[Vagrant] ssh-config erro: {e}")
            raise

    def status_stream(self):
        """
        Executa 'vagrant status' usando o pipeline de streaming (_run),
        permitindo consumir o output linha a linha num QThread.
        """
        return self._run(["vagrant", "status"])

    def wait_ssh_ready(self, vm_name: str, lab_dir: str, attempts: int = 20, delay_s: int = 3, ttl_s: int = 60) -> None:
        def _parse_ssh_config(cfg: str) -> tuple[str, int]:
            host, port = "127.0.0.1", 2222
            for raw in cfg.splitlines():
                line = raw.strip()
                if not line or " " not in line:
                    continue
                k, v = line.split(None, 1)
                if k == "HostName":
                    host = v.strip().strip('"')
                elif k == "Port":
                    try:
                        port = int(v.strip().strip('"'))
                    except Exception:
                        pass
            return host, port

        now = time.time()
        exp = self._ssh_ready_until.get(vm_name, 0)
        if exp > now:
            restante = int(exp - now)
            logger.info(f"[Preflight] {vm_name}: SSH considerado pronto (cache TTL ~{restante}s).")
            return

        try:
            cfg = subprocess.check_output(["vagrant", "ssh-config", vm_name], cwd=lab_dir, text=True, timeout=20)
            host, port = _parse_ssh_config(cfg)
        except Exception as e:
            logger.warning(f"[Preflight] ssh-config falhou ({e}); usando 127.0.0.1:2222")
            host, port = "127.0.0.1", 2222

        for i in range(1, attempts + 1):
            try:
                logger.info(f"[Preflight] Verificando SSH em {vm_name} ({host}:{port}) tentativa {i}/{attempts}...")

                # Espera porta abrir
                deadline = time.time() + max(2.0, delay_s)
                last_err = None
                while time.time() < deadline:
                    try:
                        with socket.create_connection((host, port), timeout=2.0):
                            break
                    except OSError as e:
                        last_err = e
                        time.sleep(0.25)
                else:
                    raise TimeoutError(f"Porta não abriu: {last_err}")

                # Espera banner SSH
                with socket.create_connection((host, port), timeout=3.0) as s:
                    s.settimeout(3.0)
                    data = s.recv(64)
                    if not (data and data.startswith(b"SSH-")):
                        raise RuntimeError(f"Banner inválido/reset: {data!r}")

                # Teste final com shell limpo
                wrapped = "/bin/bash --noprofile --norc -lc 'true'"
                subprocess.check_call(
                    ["vagrant", "ssh", vm_name, "-c", wrapped],
                    cwd=lab_dir,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                logger.info(f"[Preflight] SSH pronto em {vm_name}.")
                self._ssh_ready_until[vm_name] = time.time() + max(5, ttl_s)
                return
            except subprocess.CalledProcessError as e:
                logger.warning(f"[Preflight] 'vagrant ssh -c' falhou (rc={e.returncode}). Aguardando {delay_s}s...")
            except Exception as e:
                logger.warning(f"[Preflight] SSH ainda não disponível em {vm_name}: {e}. Aguardando {delay_s}s...")
            time.sleep(delay_s)
        raise RuntimeError(f"[Preflight] Timeout aguardando SSH de {vm_name}.")

def _dir_sha256(path: Path) -> str:
    h = hashlib.sha256()
    for root, _, files in os.walk(path):
        for f in sorted(files):
            if f.lower().endswith((".j2", ".jinja", "vagrantfile")):
                p = Path(root) / f
                try:
                    h.update(p.read_bytes())
                except Exception as e:
                    logger.error(f"[TemplateHash] falha lendo {p}: {e}")
    return h.hexdigest()