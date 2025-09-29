from pathlib import Path
from time import time

def _ts():
    import datetime as _dt
    return _dt.datetime.now().strftime("%H:%M:%S")

def _import_orchestrator():
    try:
        from lab.orchestrator.runner import ExperimentRunner
        from lab.orchestrator.yaml_loader import load_experiment_from_yaml
        return load_experiment_from_yaml, ExperimentRunner
    except Exception as e:
        raise ImportError(
            "Módulos de orquestração não encontrados. "
            "Crie as pastas 'orchestrator/', 'agents/', 'actions/', 'capture/', 'datasets/' e 'experiments/' "
            "conforme sugerido (ou use 'app/orchestrator/*')."
        ) from e

class UiRunnerShim:
    def __init__(self, ssh_manager, lab_dir: Path, project_root: Path, preflight, log):
        self.ssh = ssh_manager
        self._lab_dir = lab_dir
        self._project_root = project_root
        self._preflight = preflight
        self._log = log

    def run_from_yaml(self, yaml_path: str, out_dir: str, cancel_event=None):
        try:
            self._log(f"[Runner] Carregando orquestrador… ({_ts()})")
            loader, Runner = _import_orchestrator()
        except Exception as e:
            raise RuntimeError(f"Orquestrador ausente: {e}")

        try:
            t0 = time()
            self._log(f"[Preflight] Verificando VMs (attacker/sensor/victim)…")
            try:
                self._preflight.ensure(["attacker", "sensor", "victim"])
                self._log("[Preflight] OK.")
            except Exception as e:
                self._log(f"[Preflight] Aviso: {e}")

            self._log(f"[Runner] Lendo experimento: {yaml_path}")
            exp = loader(str(yaml_path))

            self._log(f"[Runner] Instanciando Runner (out_dir={self._project_root / 'data'})")
            runner = Runner(self.ssh, self._lab_dir)

            if cancel_event is not None:
                self._log("[Runner] Cancelamento suportado (cancel_event ativo).")

            self._log("[Runner] Executando pipeline (com pré-ETL)…")
            try:
                zip_path = runner.run(
                    exp,
                    out_dir=self._project_root / "data",
                    run_pre_etl=True,
                    cancel_event=cancel_event
                )
            except TypeError:
                zip_path = runner.run(
                    exp,
                    out_dir=self._project_root / "data",
                    run_pre_etl=True
                )

            dur = time() - t0
            self._log(f"[Runner] Artefato gerado: {zip_path} (em {dur:.1f}s)")
            return str(zip_path)
        except Exception as e:
            self._log(f"[Runner] Falha: {e}")
            raise