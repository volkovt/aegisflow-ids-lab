import subprocess
import sys
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
        return self._run(cmd)

    def destroy(self, name: Optional[str] = None) -> Iterable[str]:
        cmd = ["vagrant", "destroy", "-f"] + ([name] if name else [])
        return self._run(cmd)

    def status(self) -> str:
        try:
            out = subprocess.check_output(["vagrant", "status"], cwd=self.lab_dir, text=True)
            logger.info("[Vagrant] status consultado")
            return out
        except subprocess.CalledProcessError as e:
            logger.error(f"[Vagrant] status erro: {e}")
            return e.output or str(e)

    def status_by_name(self, name: str) -> Optional[str]:
        status_out = self.status()
        for line in status_out.splitlines():
            if line.startswith(name):
                parts = line.split()
                if len(parts) >= 2:
                    return parts[1]
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