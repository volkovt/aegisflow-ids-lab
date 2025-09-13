from __future__ import annotations
from typing import Callable, Dict
from pathlib import Path
import logging

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QPushButton

from app.core.workers.worker import Worker
from app.core.workers.result_worker import ResultWorker

class MainController:
    """
    Reúne a lógica de aplicação e orquestra os serviços.
    Mantém a MainWindow focada apenas em construir a UI.
    """

    def __init__(
        self,
        *,
        project_root: Path,
        lab_dir: Path,
        cfg,
        vagrant,
        ssh,
        preflight,
        warmup,
        ds_controller,
        task_manager,
        machine_info_service,
        vagrant_ctx_service,
        set_card_info_cb: Callable[[str, str, str, str], None],
        apply_status_to_cards_cb: Callable[[str], None],
        append_log: Callable[[str], None],
        logger: logging.Logger,
    ):
        self.project_root = Path(project_root)
        self.lab_dir = Path(lab_dir)
        self.cfg = cfg
        self.vagrant = vagrant
        self.ssh = ssh
        self.preflight = preflight
        self.warmup = warmup
        self.ds = ds_controller
        self.tm = task_manager
        self.machine_info = machine_info_service
        self.vagrant_ctx = vagrant_ctx_service
        self.set_card_info = set_card_info_cb
        self.apply_status_to_cards_cb = apply_status_to_cards_cb
        self.append_log = append_log
        self.logger = logger

        # Dataset events → feedback na UI
        try:
            self.ds.started.connect(self._on_ds_started)
            self.ds.finished.connect(self._on_ds_finished)
        except Exception:
            pass

        self.current_yaml_path: Path = self.project_root / "lab" / "experiments" / "exp_all.yaml"
        self.yaml_selected_by_user: bool = False

    # ---------------- Dataset ----------------
    def _on_ds_started(self):
        try:
            self.append_log("[Dataset] Iniciando…")
        except Exception as e:
            self.append_log(f"[WARN] _on_ds_started: {e}")

    def _on_ds_finished(self, status: str):
        try:
            self.append_log(f"[Dataset] Finalizado com status: {status}")
        except Exception as e:
            self.append_log(f"[WARN] _on_ds_finished: {e}")

    def generate_dataset(self, *, toggle_cancel: Callable[[], None]):
        try:
            w = getattr(self.ds, "_worker", None)
            if w and w.is_alive():
                self.append_log("[Dataset] Cancelamento solicitado pelo usuário.")
                try:
                    self.ds.cancel()
                except Exception as e:
                    self.append_log(f"[ERRO] Cancel: {e}")
                return
            yaml_path = str(self.current_yaml_path)
            out_dir = str(self.project_root / "data")
            self.ds.start(yaml_path, out_dir)
        except Exception as e:
            self.append_log(f"[ERRO] Dataset: {e}")

    # ---------------- Vagrantfile ----------------
    def write_vagrantfile(self, *, btn: QPushButton):
        def job():
            from jinja2 import Environment, FileSystemLoader
            env = Environment(loader=FileSystemLoader(str(self.project_root / "app" / "templates")))
            return str(self.vagrant.write_vagrantfile(self.project_root / "app" / "templates", self.cfg.to_template_ctx()))

        try:
            w = ResultWorker(job)
            self.tm.wire_button(btn, w, active_label="Gerando…", idle_label="Gerar Vagrantfile")
            w.result.connect(lambda p: self.append_log(f"Vagrantfile gerado em: {p}"))
            w.error.connect(lambda msg: self.append_log(f"[ERRO] Gerar Vagrantfile: {msg}"))
            self.tm.keep(w, tag="write_vagrantfile")
            w.start()
        except Exception as e:
            self.append_log(f"Erro ao gerar Vagrantfile: {e}")

    # ---------------- Status ----------------
    def status_all(self, *, btn: QPushButton):
        try:
            buffer: list[str] = []
            worker = Worker(self.vagrant.status_stream)
            worker.line.connect(lambda ln: buffer.append(ln))
            worker.error.connect(lambda msg: self.append_log(f"[ERRO] {msg}"))

            def finalize():
                out = "\n".join(buffer)
                self.append_log("Status geral:\n" + out)
                self.apply_status_to_cards_cb(out)
                btn.setEnabled(True)

            btn.setEnabled(False)
            worker.done.connect(finalize)
            self.tm.wire_button(btn, worker, active_label="Status…", idle_label="Status")
            self.tm.keep(worker, tag="status_stream")
            worker.start()
        except Exception as e:
            self.append_log(f"Erro no status: {e}")
            btn.setEnabled(True)

    def status_by_name(self, name: str, *, on_card_status: Callable[[str], None]):
        try:
            self.append_log(f"[Status] Checando {name}…")
            w = ResultWorker(self.vagrant.status_by_name, name)

            def on_result(out: str):
                self.append_log(f"Status de {name}: {out}")
                on_card_status(out)
                self.spawn_info_update(name, out)

            w.result.connect(on_result)
            w.error.connect(lambda msg: self.append_log(f"Erro no status de {name}: {msg}"))
            self.tm.keep(w, tag=f"status_by_name:{name}")
            w.start()
        except Exception as e:
            self.append_log(f"Erro no status de {name}: {e}")

    def apply_status_to_cards(self, out: str, *, set_card_status_cb: Callable[[str, str], None]):
        try:
            states: Dict[str, str] = {}
            for line in out.splitlines():
                parts = line.strip().split()
                if len(parts) >= 2:
                    states[parts[0]] = parts[1]

            for name, st in states.items():
                set_card_status_cb(name, st)

            running = [n for n, st in states.items() if st == "running"]
            for i, n in enumerate(running):
                QTimer.singleShot(1200 * i, lambda name=n, st="running": self.spawn_info_update(name, st))
        except Exception as e:
            self.append_log(f"[WARN] apply_status_to_cards: {e}")

    def spawn_info_update(self, name: str, state: str):
        def job():
            os_t, host, ip = self.machine_info.collect_machine_details(name, state_hint=state)
            return name, os_t, host, ip

        w = ResultWorker(job)
        w.result.connect(lambda res: self.set_card_info(*res))
        w.error.connect(lambda msg: self.append_log(f"[WARN] Info {name} falhou: {msg}"))
        self.tm.keep(w, tag=f"info:{name}")
        w.start()

    # ---------------- Up/Restart/Halt/Destroy ----------------
    def up_all(self, *, btn):
        def gen():
            names = ["attacker", "sensor", "victim"]
            template_dir = self.project_root / "app" / "templates"
            ctx = self.vagrant_ctx.build(self.current_yaml_path)

            yield "[UpAll] Sincronizando Vagrantfile…"
            try:
                vf_path, vf_hash, changed = self.vagrant.ensure_vagrantfile_synced(template_dir, ctx)
                status = "atualizado" if changed else "inalterado"
                yield f"[UpAll] Vagrantfile {status}: {vf_path.name} (hash {vf_hash[:8]})."
            except Exception as e:
                yield f"[UpAll] Falha ao sincronizar Vagrantfile: {e}"

            yield "[UpAll] Iniciando criação/subida das VMs…"
            try:
                _ = self.vagrant.status()
                states = {n: self.vagrant.status_by_name(n) for n in names}
            except Exception as e:
                yield f"[Status] Falha ao consultar: {e}"
                states = {}

            to_create = [n for n in names if states.get(n) in (None, "not_created", "pre_transient")]
            to_start = [n for n in names if n not in to_create]

            for n in to_create + to_start:
                yield f"[UpAll] Garantindo {n}…"
                try:
                    for ln in self.vagrant.ensure_created_and_running(n, template_dir, ctx, attempts=20, delay_s=4):
                        yield ln
                    self.warmup.mark_boot(n)
                    yield f"[Warmup] {n}: janela de aquecimento iniciada (30s)."
                except Exception as e:
                    yield f"[UpAll] Falha em {n}: {e}"

            yield "[UpAll] Concluído."
            return "ok"

        try:
            worker = Worker(gen)
            worker.line.connect(self.append_log)

            def finalize():
                try:
                    out = self.vagrant.status()
                    self.apply_status_to_cards_cb(out)
                    self.append_log("[UpAll] Finalizado.")
                except Exception as e:
                    self.append_log(f"[UpAll] Falha ao atualizar status: {e}")

            worker.done.connect(finalize)
            worker.error.connect(lambda msg: self.append_log(f"[ERRO] Up All: {msg}"))
            self.tm.wire_button(btn, worker, active_label="Subindo VMs…", idle_label="Subir todas")
            self.tm.keep(worker, tag="up_all")
            worker.start()
        except Exception as e:
            self.append_log(f"[ERRO] Up All: {e}")

    def restart_vm(self, name: str, *, btn: QPushButton | None = None):
        def gen():
            try:
                self.append_log(f"[Restart] Reiniciando {name}…")
                try:
                    self.append_log("[Thread] Parando threads antes de 'reload'…")
                    self.tm.quiesce(reason="reload", timeout_ms=6000)
                except Exception as e:
                    self.append_log(f"[WARN] reload quiesce: {e}")

                try:
                    st = self.vagrant.status_by_name(name)
                except Exception as e:
                    st = None
                    yield f"[Restart] Falha ao consultar status de {name}: {e}"

                if st != "running":
                    yield f"[Restart] {name} não está 'running' — executando Up."
                    try:
                        for ln in self.vagrant.up(name):
                            yield ln
                    except Exception as e:
                        yield f"[ERRO] Up {name}: {e}"
                        return "error"
                else:
                    try:
                        if hasattr(self.vagrant, "reload"):
                            yield f"[Restart] Executando vagrant reload em {name}…"
                            for ln in self.vagrant.reload(name):
                                yield ln
                        else:
                            yield f"[Restart] reload indisponível; executando Halt + Up em {name}…"
                            for ln in self.vagrant.halt(name):
                                yield ln
                            for ln in self.vagrant.up(name):
                                yield ln
                    except Exception as e:
                        yield f"[ERRO] Restart {name}: {e}"
                        return "error"

                try:
                    self.vagrant.wait_ssh_ready(name, str(self.lab_dir), attempts=10, delay_s=3)
                    yield f"[Restart] {name} está 'running' e SSH pronto."
                except Exception as e:
                    yield f"[Restart] {name} 'running' porém SSH ainda não respondeu: {e}"

                try:
                    self.warmup.mark_boot(name)
                    yield f"[Warmup] {name}: janela de aquecimento iniciada (30s)."
                except Exception as e:
                    yield f"[Warmup] Falha ao marcar boot de {name}: {e}"

                yield "[Restart] Concluído."
                return "ok"

            except Exception as e:
                yield f"[ERRO] Restart {name}: {e}"
                return "error"

        try:
            worker = Worker(gen)
            worker.line.connect(self.append_log)
            worker.error.connect(lambda msg: self.append_log(f"[ERRO] Restart {name}: {msg}"))
            if btn is not None:
                self.tm.wire_button(btn, worker, active_label="Restart…", idle_label="Restart")
            worker.done.connect(lambda: self.status_by_name(name, on_card_status=lambda _st: None))
            self.tm.keep(worker, tag=f"restart:{name}")
            worker.start()
        except Exception as e:
            self.append_log(f"[ERRO] restart_vm({name}): {e}")

    def halt_all(self, *, btn: QPushButton):
        self._run_simple_vagrant(self.vagrant.halt, btn=btn, active_label="Halt…", idle_label="Halt todas")

    def destroy_all(self, *, btn: QPushButton, confirm_dialog: Callable[[str, str], int]):
        confirm = confirm_dialog("Confirmar", "Destruir TODAS as VMs? Esta ação é irreversível.")
        if confirm:
            self._run_simple_vagrant(self.vagrant.destroy, btn=btn, active_label="Destroy…", idle_label="Destroy todas")

    def _run_simple_vagrant(self, func, *, btn: QPushButton, active_label: str, idle_label: str):
        try:
            worker = Worker(func)
            worker.line.connect(self.append_log)
            worker.error.connect(lambda msg: self.append_log(f"[ERRO] {msg}"))
            worker.done.connect(lambda: self.append_log("[OK] Operação finalizada."))
            self.tm.wire_button(btn, worker, active_label=active_label, idle_label=idle_label)
            self.tm.keep(worker, tag=func.__name__)
            worker.start()
        except Exception as e:
            self.append_log(f"Erro ao iniciar {func.__name__}: {e}")

    # ---------------- Misc ----------------
    def open_guide(self, *, dialog_factory: Callable[..., object], yaml_for_guide: str):
        try:
            dlg = dialog_factory(yaml_path=yaml_for_guide, ssh=self.ssh, vagrant=self.vagrant, lab_dir=str(self.lab_dir), project_root=str(self.project_root))
            if hasattr(dlg, "show"):
                dlg.show()
            return dlg
        except Exception as e:
            self.append_log(f"[ERRO] Abrir Guia: {e}")
            raise

    def open_folder(self, path: Path):
        from sys import platform as sysplat
        import subprocess, os
        try:
            path = Path(path)
            path.mkdir(parents=True, exist_ok=True)
            if sysplat.startswith("win"):
                os.startfile(str(path))
            elif sysplat == "darwin":
                subprocess.check_call(["open", str(path)])
            else:
                subprocess.check_call(["xdg-open", str(path)])
            self.append_log(f"[UI] Abrindo pasta: {path}")
        except Exception as e:
            self.append_log(f"[UI] Falha ao abrir pasta: {e}")

    def ssh_open(self, name: str):
        def gen():
            try:
                yield f"[SSH] Preparando {name}…"
                st = self.vagrant.status_by_name(name)
                if st != "running":
                    yield f"[WARN] {name} não está 'running' (rode: vagrant up {name})."
                    return "skip"
                try:
                    self.vagrant.wait_ssh_ready(name, str(self.lab_dir), attempts=10, delay_s=3)
                    yield f"[SSH] {name} com SSH pronto."
                except Exception as e:
                    yield f"[WARN] SSH ainda não pronto em {name}: {e}"
                    return "skip"
                yield f"Abrindo terminal SSH externo para {name}…"
                try:
                    self.ssh.open_external_terminal(name)
                    return "ok"
                except Exception as e:
                    yield f"[ERRO] Falha ao abrir SSH externo: {e}"
                    return "error"
            except Exception as e:
                yield f"[ERRO] SSH {name}: {e}"
                return "error"

        try:
            worker = Worker(gen)
            worker.line.connect(self.append_log)
            worker.error.connect(lambda msg: self.append_log(f"[ERRO] SSH {name}: {msg}"))
            self.tm.keep(worker, tag=f"ssh:{name}")
            worker.start()
        except Exception as e:
            self.append_log(f"[ERRO] ssh_open: {e}")
