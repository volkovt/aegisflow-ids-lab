import logging, sys
from pathlib import Path
import pandas as pd
import numpy as np
from sklearn.model_selection import train_test_split, GroupShuffleSplit, TimeSeriesSplit
from sklearn.compose import ColumnTransformer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from sklearn.impute import SimpleImputer
from sklearn.ensemble import RandomForestClassifier, IsolationForest
from sklearn.metrics import classification_report, confusion_matrix, f1_score, roc_auc_score

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("[Viability]")

def load_df(p: Path) -> pd.DataFrame:
    try:
        logger.info(f"[Viability] Lendo: {p}")
        return pd.read_csv(p)
    except Exception as e:
        logger.error(f"[Viability] Falha ao ler CSV: {e}")
        raise

def choose_split(df: pd.DataFrame):
    # tenta blocar por captura/host/execução para evitar vazamento
    for g in ["capture_id","run_id","sensor_host","src_h","vm_name"]:
        if g in df.columns and df[g].nunique() > 1:
            logger.info(f"[Viability] Usando GroupShuffleSplit por '{g}'")
            grp = df[g].astype(str)
            splitter = GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=42)
            idx_train, idx_test = next(splitter.split(df, groups=grp))
            return idx_train, idx_test
    # tenta split temporal se existir timestamp
    for t in ["ts","start_ts","time","datetime"]:
        if t in df.columns:
            logger.info(f"[Viability] Split temporal por '{t}'")
            df = df.sort_values(t)
            n = len(df)
            cut = int(n*0.8)
            return np.arange(cut), np.arange(cut, n)
    # fallback aleatório
    logger.warning("[Viability] Split aleatório (sem grupos/tempo)")
    n = len(df)
    idx = np.random.RandomState(42).permutation(n)
    cut = int(n*0.8)
    return idx[:cut], idx[cut:]

def supervised_baseline(df: pd.DataFrame, label_col: str):
    y = df[label_col].astype(str)
    X = df.drop(columns=[label_col])
    num_cols = [c for c in X.columns if pd.api.types.is_numeric_dtype(X[c])]
    cat_cols = [c for c in X.columns if c not in num_cols]
    pre = ColumnTransformer([
        ("num", Pipeline([("imp", SimpleImputer(strategy="median")),
                          ("sc", StandardScaler(with_mean=False))]), num_cols),
        ("cat", Pipeline([("imp", SimpleImputer(strategy="most_frequent")),
                          ("oh", OneHotEncoder(handle_unknown="ignore"))]), cat_cols),
    ])
    clf = RandomForestClassifier(n_estimators=300, class_weight="balanced",
                                 n_jobs=-1, random_state=42)
    pipe = Pipeline([("pre", pre), ("clf", clf)])
    idx_tr, idx_te = choose_split(df)
    Xtr, Xte, ytr, yte = X.iloc[idx_tr], X.iloc[idx_te], y.iloc[idx_tr], y.iloc[idx_te]
    logger.info("[Viability] Treinando RandomForest...")
    pipe.fit(Xtr, ytr)
    yp = pipe.predict(Xte)
    logger.info("\n" + classification_report(yte, yp))
    logger.info("\nMatriz de confusão:\n%s", confusion_matrix(yte, yp))
    return True

def unsupervised_baseline(df: pd.DataFrame, label_col: str | None):
    Xu = df.drop(columns=[label_col]) if label_col else df.copy()
    num = [c for c in Xu.columns if pd.api.types.is_numeric_dtype(Xu[c])]
    if len(num) < 2:
        logger.error("[Viability] Poucas features numéricas para não-supervisionado.")
        return False
    Xu = Xu[num].replace([np.inf,-np.inf], np.nan).fillna(0)
    iso = IsolationForest(n_estimators=200, contamination="auto",
                          n_jobs=-1, random_state=42)
    logger.info("[Viability] Treinando IsolationForest...")
    iso.fit(Xu)
    if label_col:
        y_bin = (df[label_col].astype(str) != "benign").astype(int)
        pred = (iso.predict(Xu) == -1).astype(int)
        f1 = f1_score(y_bin, pred)
        try:
            scores = -iso.decision_function(Xu)
            auc = roc_auc_score(y_bin, scores)
        except Exception:
            auc = None
        logger.info(f"[Viability] F1(anomalia)={f1:.3f} | AUC={auc}")
    return True

def main(csv_path: str):
    try:
        df = load_df(Path(csv_path))
        label = next((c for c in ["label","target","y"] if c in df.columns), None)
        if label and df[label].nunique() > 1:
            logger.info("[Viability] Modo supervisionado (rótulos detectados).")
            ok = supervised_baseline(df, label)
        else:
            logger.warn("[Viability] Sem rótulo útil → modo não-supervisionado.")
            ok = unsupervised_baseline(df, label)
        if ok:
            logger.info("[Viability] ✅ Já é possível treinar com o dado atual.")
        else:
            logger.error("[Viability] ❌ Faltam condições mínimas (features/variação).")
    except Exception as e:
        logger.error(f"[Viability] Erro geral: {e}")

if __name__ == "__main__":
    csv = sys.argv[1] if len(sys.argv) > 1 else "C:\\Users\\diego\\Desktop\\faculdade\\TCC\\python-projects\\VagrantLabUI\\etl\\exp_hydra_sweep\\csv\\full.csv"
    main(csv)
