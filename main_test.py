# # from pathlib import Path
# #
# # from lab.datasets.pre_etl import _parse_zeek_tsv, generate_conn_features
# #
# # df = _parse_zeek_tsv(Path(r"C:\Users\diego\Desktop\faculdade\TCC\python-projects\VagrantLabUI\data\EXP_SCAN_BRUTE\sensor\zeek\conn.log"))
# # print(df.head(3), df.shape)
# #
# # out = generate_conn_features(Path(r"C:\Users\diego\Desktop\faculdade\TCC\python-projects\VagrantLabUI\data\EXP_SCAN_BRUTE"))
# # print("features em:", out)
#
# # from pathlib import Path
# # import logging
# # from lab.datasets.etl_netsec import run_etl
# #
# # logging.basicConfig(level=logging.INFO)
# #
# # run_etl(Path(r'data\EXP_SCAN_BRUTE'), Path(r'data\EXP_SCAN_BRUTE'))
#
# #!/usr/bin/env python3
# # python
# import argparse
# from pathlib import Path
# import pandas as pd
# import sys
#
# DEFAULT_DIR = Path(r"C:\Users\diego\Desktop\faculdade\TCC\python-projects\VagrantLabUI\data\processed\EXP_SCAN_BRUTE")
#
# def print_parquet_files(folder: Path, nrows: int = 10):
#     if not folder.exists() or not folder.is_dir():
#         print(f"Erro: pasta não encontrada: {folder}", file=sys.stderr)
#         return 1
#
#     files = sorted(folder.glob("*.parquet")) + sorted(folder.glob("*.parq"))
#     if not files:
#         print(f"Nenhum arquivo .parquet/.parq em: {folder}")
#         return 0
#
#     for f in files:
#         print("=" * 80)
#         print(f"Arquivo: {f}")
#         try:
#             df = pd.read_parquet(f, engine="pyarrow")
#         except Exception as e:
#             print(f"Falha ao ler {f}: {e}", file=sys.stderr)
#             continue
#
#         print(f"Shape: {df.shape}")
#         print(f"Colunas: {list(df.columns)}")
#         # imprime nrows linhas (ou menos se menor)
#         print(f"Primeiras {nrows} linhas:")
#         print(df.head(nrows).to_string(index=False))
#     print("=" * 80)
#     return 0
#
# def main():
#     p = argparse.ArgumentParser(description="Imprime conteúdo de arquivos parquet numa pasta")
#     p.add_argument("--dir", "-d", type=Path, default=DEFAULT_DIR, help="Pasta com arquivos parquet")
#     p.add_argument("--rows", "-n", type=int, default=10, help="Linhas a mostrar por arquivo")
#     args = p.parse_args()
#     return_code = print_parquet_files(args.dir, args.rows)
#     sys.exit(return_code)
#
# if __name__ == "__main__":
#     main()
import logging
from pathlib import Path
from lab.datasets.pre_etl import generate_conn_features

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("[RegenFeatures]")

try:
    out = generate_conn_features(
        Path(r"C:\Users\diego\Desktop\faculdade\TCC\python-projects\VagrantLabUI\data\EXP_SCAN_BRUTE"),
        window_s=10  # <— granularidade maior
    )
    logger.info(f"Features geradas em: {out}")
except Exception as e:
    logger.error(f"Falha ao gerar features: {e}")
