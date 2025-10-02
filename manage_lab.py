import argparse
from pathlib import Path

from app.core.logger_setup import setup_logger
from app.core.config_loader import load_config
from app.core.pathing import get_project_root, find_config
from app.core.preflight import run_preflight
from app.core.ssh_manager import SSHManager
from app.core.vagrant_manager import VagrantManager

logger = setup_logger(Path('.logs'), name="[ManageLab]")

def _import_orchestrator():
    """
    Tenta importar orquestrador tanto em 'orchestrator/*' quanto 'app/orchestrator/*'.
    Retorna (load_experiment_from_yaml, ExperimentRunner).
    """
    try:
        from lab.orchestrator.runner import ExperimentRunner
        from lab.orchestrator.yaml_loader import load_experiment_from_yaml
        return load_experiment_from_yaml, ExperimentRunner
    except Exception as e:
        logger.error(f"[ManageLab] Erro:': {e}")
        raise ImportError(
            "Módulos de orquestração não encontrados. "
            "Crie as pastas 'orchestrator/', 'agents/', 'actions/', 'capture/', 'datasets/' e 'experiments/' "
            "conforme sugerido (ou use 'app/orchestrator/*')."
        ) from e

parser = argparse.ArgumentParser(description="Orquestra laboratório Vagrant para TCC IDS/ML")
parser.add_argument("--write-vagrantfile", action="store_true")
parser.add_argument("--up", action="store_true")
parser.add_argument("--halt", action="store_true")
parser.add_argument("--destroy", action="store_true")
parser.add_argument("--status", action="store_true")
parser.add_argument("--preflight", action="store_true", help="Roda checagens do laboratório e gera relatório")
parser.add_argument("--name", type=str, default=None, help="Nome da máquina-alvo (opcional)")

# NOVO: geração de dataset via YAML
parser.add_argument("--generate-dataset", action="store_true", help="Executa experimento do YAML e gera dataset.zip")
parser.add_argument("--exp-config", type=str, default="experiments/exp_all.yaml", help="Caminho do YAML do experimento")
parser.add_argument("--out-dir", type=str, default="data", help="Diretório de saída para o dataset")
parser.add_argument("--no-pre-etl", action="store_true", help="Não gerar pré-ETL (features_conn_window.csv)")

args = parser.parse_args()

try:
    project_root = get_project_root()
    cfg = load_config(find_config(project_root / "config.yaml"))
    project_root = Path.cwd()
    lab_dir = project_root / cfg.lab_dir
    vg = VagrantManager(project_root, lab_dir)

    if args.write_vagrantfile:
        vg.write_vagrantfile(project_root / "app" / "templates", cfg.to_template_ctx())

    if args.up:
        for ln in vg.up(args.name):
            logger.info(ln)

    if args.halt:
        for ln in vg.halt(args.name):
            logger.info(ln)

    if args.destroy:
        for ln in vg.destroy(args.name):
            logger.info(ln)

    if args.status:
        print(vg.status())

    if args.preflight:
        sshm = SSHManager(lab_dir)
        for ln in run_preflight(project_root, lab_dir, cfg, vg, sshm):
            logger.info(ln)

    if args.generate_dataset:
        try:
            load_yaml, Runner = _import_orchestrator()
        except Exception as e:
            logger.error(e)
            raise

        sshm = SSHManager(lab_dir)
        exp = load_yaml(args.exp_config)
        runner = Runner(ssh_manager=sshm, lab_dir=lab_dir)
        zip_path = runner.run(exp, out_dir=Path(args.out_dir), run_pre_etl=(not args.no_pre_etl))
        logger.info(f"[GenDataset] Dataset gerado em: {zip_path}")

except Exception as e:
    logger.error(f"Erro na automação: {e}")
    raise
