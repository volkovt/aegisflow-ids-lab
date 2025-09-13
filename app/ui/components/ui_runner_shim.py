from pathlib import Path


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
            loader, Runner = _import_orchestrator()
        except Exception as e:
            raise RuntimeError(f"Orquestrador ausente: {e}")

        try:
            try:
                self._preflight.ensure(["attacker", "sensor", "victim"])
            except Exception as e:
                self._log(f"[Preflight] Aviso: {e}")

            exp = loader(str(yaml_path))
            runner = Runner(self.ssh, self._lab_dir)

            try:
                zip_path = runner.run(exp, out_dir=self._project_root / "data",
                                      run_pre_etl=True, cancel_event=cancel_event)
            except TypeError:
                zip_path = runner.run(exp, out_dir=self._project_root / "data",
                                      run_pre_etl=True)
            return str(zip_path)
        except Exception as e:
            raise
