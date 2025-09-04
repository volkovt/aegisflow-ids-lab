import logging
import shutil
import socket
import subprocess
import platform
from datetime import datetime
from pathlib import Path
from typing import Iterable, Tuple

from app.core.logger_setup import setup_logger
from app.core.config_loader import LabConfig
from app.core.vagrant_manager import VagrantManager
from app.core.ssh_manager import SSHManager

logger = logging.getLogger("[Preflight]")

def _run_cmd(cmd: list[str]) -> Tuple[bool, str]:
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT)
        return True, out.strip()
    except Exception as e:
        return False, str(e)

def _check_binary(name: str, version_args: list[str]) -> Tuple[bool, str]:
    ok, out = _run_cmd(version_args)
    if ok:
        return True, f"{name} OK: {out}"
    return False, f"{name} NÃO encontrado/erro: {out}"

def _check_disk(path: Path, min_gb: int = 10) -> Tuple[bool, str]:
    try:
        total, used, free = shutil.disk_usage(path)
        free_gb = int(free / (1024**3))
        if free_gb >= min_gb:
            return True, f"Disco OK em {path} (livre: ~{free_gb} GB)"
        return False, f"Pouco espaço em {path} (livre: ~{free_gb} GB, mínimo recomendado: {min_gb} GB)"
    except Exception as e:
        return False, f"Falha ao checar disco em {path}: {e}"

def _try_connect(host: str, port: int, timeout: float = 2.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False

def run_preflight(project_root: Path,
                  lab_dir: Path,
                  cfg: LabConfig,
                  vg: VagrantManager,
                  sshm: SSHManager) -> Iterable[str]:
    """
    Executa verificações de pré-voo do laboratório e escreve relatório em .logs/lab_preflight.txt.
    Agora também PERSISTE os detalhes por VM (estado, ssh-config, chave, conectividade).
    """
    log = setup_logger(Path(".logs"), name="[Preflight]")
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    report_path = Path(".logs") / "lab_preflight.txt"

    header = [
        "================== PRE-FLIGHT DO LAB IDS/ML ==================",
        f"Data/Hora: {ts}",
        f"Sistema: {platform.system()} {platform.release()} ({platform.version()})",
        f"Projeto: {cfg.project_name}",
        f"Provider: {cfg.provider}",
        f"Lab dir: {lab_dir}",
        f"Máquinas definidas no config: {len(cfg.machines)}",
        "==============================================================",
        ""
    ]
    for line in header:
        log.info(line)
        yield line

    checks: list[Tuple[str, bool, str]] = []

    # 1) Binaries essenciais
    ok_vagrant, msg_vagrant = _check_binary("Vagrant", ["vagrant", "--version"])
    checks.append(("Vagrant", ok_vagrant, msg_vagrant)); log.info(msg_vagrant); yield msg_vagrant

    if cfg.provider.lower() == "virtualbox":
        ok_vb, msg_vb = _check_binary("VirtualBox (VBoxManage)", ["VBoxManage", "--version"])
        checks.append(("VirtualBox", ok_vb, msg_vb)); log.info(msg_vb); yield msg_vb

    ok_ssh, msg_ssh = _check_binary("SSH (cliente)", ["ssh", "-V"])
    checks.append(("SSH", ok_ssh, msg_ssh)); log.info(msg_ssh); yield msg_ssh

    # 2) Espaço em disco
    ok_disk, msg_disk = _check_disk(lab_dir, min_gb=10)
    checks.append(("Disco", ok_disk, msg_disk)); log.info(msg_disk); yield msg_disk

    # 3) Config e Vagrantfile
    lab_dir.mkdir(parents=True, exist_ok=True)
    vagrantfile = lab_dir / "Vagrantfile"
    if not vagrantfile.exists():
        msg = "Vagrantfile NÃO encontrado (gere-o antes com: Gerar Vagrantfile / --write-vagrantfile)."
        checks.append(("Vagrantfile", False, msg)); log.warning(msg); yield msg
    else:
        msg = f"Vagrantfile encontrado em {vagrantfile}"
        checks.append(("Vagrantfile", True, msg)); log.info(msg); yield msg

    # 4) Status do Vagrant
    try:
        status_out = vg.status()
        msg = "Status do Vagrant obtido com sucesso."
        checks.append(("Vagrant status", True, msg)); log.info(msg); yield msg
    except Exception as e:
        msg = f"Falha ao obter status do Vagrant: {e}"
        checks.append(("Vagrant status", False, msg)); log.error(msg); yield msg
        status_out = ""

    # 5) Host-only (VirtualBox)
    if cfg.provider.lower() == "virtualbox":
        ok_net, net_out = _run_cmd(["VBoxManage", "list", "hostonlyifs"])
        if ok_net:
            yield "Redes host-only (VirtualBox) listadas."
            log.info("Host-only IFs listadas.")
            ip_base = cfg.ip_base
            prefix = ip_base.rsplit(".", 1)[0] + "."
            present = prefix in net_out
            if present:
                msg = f"Rede host-only compatível com {prefix}* encontrada."
                checks.append(("Host-only", True, msg)); log.info(msg); yield msg
            else:
                msg = f"ATENÇÃO: não encontrei host-only IF com prefixo {prefix}*. Verifique VirtualBox Network."
                checks.append(("Host-only", False, msg)); log.warning(msg); yield msg
        else:
            msg = f"Não foi possível listar host-only IFs: {net_out}"
            checks.append(("Host-only", False, msg)); log.warning(msg); yield msg

    # 6) Por VM: capturar detalhes para persistência
    vm_order: list[str] = []
    vm_lines: dict[str, list[str]] = {}

    def _vm_add(name: str, line: str):
        vm_lines.setdefault(name, []).append(line)

    for m in cfg.machines:
        yield ""
        title = f"[VM] {m.name}"
        log.info(title); yield title

        vm_order.append(m.name)
        vm_lines.setdefault(m.name, [])

        # Estado
        try:
            state = vg.status_by_name(m.name)
            line = f" - Estado: {state}"
            yield line; _vm_add(m.name, line)
        except Exception as e:
            line = f" - Erro ao consultar estado: {e}"
            log.error(line); yield line; _vm_add(m.name, line)
            state = None

        # ssh-config + conectividade
        try:
            f = sshm.get_ssh_fields(m.name)
            line = f" - ssh-config OK (User={f['User']}, HostName={f['HostName']}, Port={f['Port']})"
            yield line; _vm_add(m.name, line)

            key_path = Path(f["IdentityFile"].replace('"', '').strip())
            if key_path.exists():
                line = f" - IdentityFile OK: {key_path}"
                yield line; _vm_add(m.name, line)
            else:
                line = f" - AVISO: IdentityFile não encontrado: {key_path}"
                log.warning(line); yield line; _vm_add(m.name, line)

            if state == "running":
                if _try_connect(f["HostName"], int(f["Port"]), timeout=2.5):
                    line = " - Conectividade SSH OK (socket aberto)."
                    yield line; _vm_add(m.name, line)
                else:
                    line = " - AVISO: Não consegui abrir socket na porta SSH informada pelo vagrant."
                    log.warning(line); yield line; _vm_add(m.name, line)
        except Exception as e:
            line = f" - ssh-config indisponível: {e} (Provável VM não criada/ligada)"
            log.warning(line); yield line; _vm_add(m.name, line)

    # 7) Resumo
    total = len(checks)
    oks = sum(1 for _, ok, _ in checks if ok)
    fails = total - oks
    summary = f"\nResumo: {oks}/{total} checagens OK; {fails} com atenção."
    log.info(summary); yield summary

    # 8) Grava relatório (agora com detalhes por VM)
    try:
        with report_path.open("w", encoding="utf-8") as f:
            for line in header:
                f.write(line + "\n")
            f.write("\n-- RESULTADOS --\n")
            for name, ok, msg in checks:
                status = "OK" if ok else "ATENÇÃO"
                f.write(f"[{status}] {name}: {msg}\n")

            f.write("\n-- DETALHES POR VM --\n")
            for name in vm_order:
                f.write(f"\n[VM] {name}\n")
                lines = vm_lines.get(name, [])
                if lines:
                    for ln in lines:
                        f.write(ln + "\n")
                else:
                    f.write(" (sem detalhes gerados)\n")

            # (Opcional, útil para rastreabilidade do TCC)
            if status_out:
                f.write("\n-- Vagrant status (bruto) --\n")
                f.write(status_out.strip() + "\n")

            f.write("\n" + summary.strip() + "\n")

        yield f"Relatório salvo em: {report_path}"
        log.info(f"Relatório salvo em: {report_path}")
    except Exception as e:
        err = f"Falha ao salvar relatório: {e}"
        log.error(err); yield err

    yield "Preflight concluído."
    log.info("Preflight concluído.")

