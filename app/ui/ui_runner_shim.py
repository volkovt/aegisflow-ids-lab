from pathlib import Path

from app.ui.main import _import_orchestrator


class UiRunnerShim:
    """
    Adaptador leve para o DatasetController.
    - Expõe .ssh para permitir cancel_all_running().
    - Faz preflight e chama o Runner real.
    - Se o Runner suportar cancel_event, repassa; se não, segue sem (fallback).
    """
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
            # Preflight não bloqueante: loga warning e segue (evita “cancelar por detalhe”)
            try:
                self._preflight.ensure(["attacker", "sensor", "victim"])
            except Exception as e:
                self._log(f"[Preflight] Aviso: {e}")

            exp = loader(str(yaml_path))
            runner = Runner(self.ssh, self._lab_dir)

            # Tenta com cancel_event; se a sua versão do Runner não aceitar, cai no fallback.
            try:
                zip_path = runner.run(exp, out_dir=self._project_root / "data",
                                      run_pre_etl=True, cancel_event=cancel_event)
            except TypeError:
                zip_path = runner.run(exp, out_dir=self._project_root / "data",
                                      run_pre_etl=True)
            return str(zip_path)
        except Exception as e:
            raise
