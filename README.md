```markdown
# 🛡️ AegisFlow IDS Lab — Vagrant + PySide6 + ML

Laboratório **reprodutível** para **simular ataques**, **coletar telemetria** (Zeek/PCAP/Sysmon/auditd) e **treinar modelos de detecção de anomalias** (Isolation Forest, Random Forest, Autoencoder) — pensado para o seu **TCC de Segurança Cibernética**.

> ⚠️ **Ética e segurança**: use **somente** em ambiente isolado (VirtualBox Host-Only / NAT Network). Não exponha os alvos/ataques à internet.

---

## ✨ Principais recursos

- **Orquestração de VMs com Vagrant** (attacker / victim / sensor).
- **UI em PySide6** futurista para: gerar `Vagrantfile`, dar `up/halt/destroy`, ver **status** e **pills** com SO/Host/Guest e abrir **SSH** com um clique.
- **Preflight**: checa Vagrant, VirtualBox, SSH, disco, redes host-only e `ssh-config` por VM; gera relatório em `.logs/lab_preflight.txt`.
- **Coleta de dados**: PCAP rotativo + Zeek (conn/http/dns), auditd/auth.log (Linux), Sysmon/Winlogbeat (Windows).
- **Pipeline ML** (exemplo): engenharia de features, treino/validação com métricas de segurança (Recall/Precision/F1, ROC-AUC).
- **Presets de Experimentos (YAML)**: _scan+brute_, _DoS_, e um **preset heavy** (hping3 SYN) + **BRUTE-HTTP com hydra**.

> A base técnica da automação (CLI/UI, preflight, SSH resiliente, carga de config) foi estruturada nos módulos `manage_lab.py`, `main.py`, `preflight.py`, `ssh_manager.py`, `config_loader.py`. Estes componentes já implementam logs robustos e validações do lab. 

---

## 📦 Estrutura (sugerida)

```

aegisflow-ids-lab/
├─ app/
│  ├─ core/
│  │  ├─ config\_loader.py
│  │  ├─ preflight.py
│  │  ├─ vagrant\_manager.py
│  │  ├─ ssh\_manager.py
│  │  └─ logger\_setup.py
│  ├─ templates/
│  │  └─ Vagrantfile.j2
│  ├─ ui/
│  │  ├─ futuristic.qss
│  │  ├─ info\_pills.py
│  │  └─ flow\_layout.py
│  └─ main.py
├─ manage\_lab.py
├─ experiments/
│  ├─ exp\_scan\_brute.yaml
│  ├─ exp\_dos.yaml
│  └─ presets/
│     ├─ preset\_heavy\_hping3.yaml
│     └─ preset\_brute\_http\_hydra.yaml
├─ data/            # (ignore no Git) pcap/logs/zeek
├─ datasets/        # (ignore no Git) datasets processados para ML
├─ .logs/           # (ignore no Git) relatórios e logs
├─ requirements.txt
├─ config.example.yaml
├─ .gitignore
└─ README.md

````

---

## 🚀 Comece agora

### 1) Requisitos
- **Python 3.11+**
- **Vagrant** e **VirtualBox** instalados
- Windows / Linux / macOS

### 2) Setup
```bash
# Clone
git clone https://github.com/<seu-usuario>/aegisflow-ids-lab.git
cd aegisflow-ids-lab

# Ambiente virtual
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt

# Config local
cp config.example.yaml config.yaml
# edite config.yaml conforme seu host (ip_base, boxes etc.)
````

### 3) Geração do Vagrantfile e subida do lab (CLI)

```bash
# Gerar Vagrantfile
python manage_lab.py --write-vagrantfile

# Subir todas as VMs
python manage_lab.py --up

# Status / Halt / Destroy
python manage_lab.py --status
python manage_lab.py --halt
python manage_lab.py --destroy
```

### 4) UI (PySide6)

```bash
python -m app.main
```

### 5) Preflight (relatório do ambiente)

```bash
python manage_lab.py --preflight
# Relatório salvo em .logs/lab_preflight.txt
```

---

## ⚙️ `config.example.yaml` (modelo resumido)

```yaml
project_name: "AegisFlow IDS Lab"
lab_dir: "lab"
provider: "virtualbox"
network:
  ip_base: "192.168.56."

machines:
  - name: "attacker"
    box: "kalilinux/rolling"
    hostname: "attacker.local"
    cpus: 2
    memory: 2048
    ip_last_octet: 10
    synced_folders:
      - { host: "./data",  guest: "/data" }
    provision:
      - { inline: "sudo apt-get update -y" }

  - name: "victim"
    box: "bento/ubuntu-16.04"
    hostname: "victim.local"
    cpus: 2
    memory: 2048
    ip_last_octet: 11

  - name: "sensor"
    box: "bento/ubuntu-20.04"
    hostname: "sensor.local"
    cpus: 2
    memory: 2048
    ip_last_octet: 12
```

---

## 🧪 Presets de Experimentos (YAML)

### `experiments/exp_scan_brute.yaml`

```yaml
id: EXP01_SCAN_BRUTE
description: "Nmap scan + brute force SSH com hydra (lab isolado)"
steps:
  - when: "t0"
    on: "attacker"
    run: "nmap -sS -sV -O -T4 192.168.56.11 -oN /data/scan_victim.txt"
  - when: "t1"
    on: "attacker"
    run: "hydra -l vagrant -P /usr/share/wordlists/rockyou.txt ssh://192.168.56.11 -t 4 -f -o /data/brute_ssh.txt"
labels:
  attack_window: ["t0","t1"]
  target: "victim"
notes: "Verificar /var/log/auth.log (victim) e conn.log/http.log (sensor/zeek)."
```

### `experiments/exp_dos.yaml`

```yaml
id: EXP02_DOS
description: "DoS controlado (slowhttptest) contra serviço web interno"
steps:
  - when: "t0"
    on: "attacker"
    run: "slowhttptest -c 500 -H -i 10 -r 200 -t GET -u http://192.168.56.11:3000/ -x 24 -p 3 -l 60 -o /data/slowhttp"
labels:
  attack_window: ["t0"]
  target: "victim"
notes: "Checar http.log/conn.log (Zeek) e disponibilidade do serviço."
```

> **Bônus (presets):** `preset_heavy_hping3.yaml` (SYN flood controlado) e `preset_brute_http_hydra.yaml` (hydra em `http-post-form`) vêm prontos para acionar pela UI “Designer (YAML)”.

---

## 🧰 Coleta & ML (exemplo rápido)

```python
import logging
import pandas as pd
from sklearn.ensemble import IsolationForest, RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("[MLPipeline]")

try:
    logger.info("Carregando logs Zeek (conn.log TSV-like)…")
    df = pd.read_csv("data/zeek/conn.log", sep=r"\s+", engine="python", comment="#")

    # Engenharia simples (exemplo)
    feats = df[["orig_pkts","resp_pkts","orig_ip_bytes","resp_ip_bytes"]].fillna(0)
    X_train, X_test = train_test_split(feats, test_size=0.3, random_state=42)

    logger.info("Treinando Isolation Forest (não supervisionado)…")
    iso = IsolationForest(n_estimators=200, contamination=0.02, random_state=42)
    iso.fit(X_train)
    scores = iso.score_samples(X_test)

    # Exemplo supervisionado (se tiver rótulo)
    # df["label"] = ...  # 0=benign, 1=attack
    # Xtr, Xte, ytr, yte = train_test_split(feats, df["label"], test_size=0.3, stratify=df["label"], random_state=42)
    # rf = RandomForestClassifier(n_estimators=300, random_state=42, n_jobs=-1)
    # rf.fit(Xtr, ytr)
    # print(classification_report(yte, rf.predict(Xte)))

    logger.info("Pipeline concluído (exemplo).")
except Exception as e:
    logger.error(f"Falha no pipeline ML: {e}")
```

---

## 🧭 Roadmap

* UI **Designer (YAML)** in-app para montar/rodar cenários e presets.
* **Preset heavy** (hping3 SYN flood) + **BRUTE-HTTP** (hydra http-post-form).
* Painel de **telemetria** (gráficos) e export para **SIEM**.
* **Validação automática** pós-experimento (carimbo temporal, rótulos).
* **AutoML** de modelos candidatos e seleção por F1/Recall.

---

## 🤝 Contribuição

* Issues e PRs são bem-vindos.
* Padrões: logs com `logger.info/warning/error`, `try/except` nos trechos críticos, e scripts reprodutíveis.

---

## 📄 Licença

MIT (ajuste conforme necessidade do TCC/empresa).

---

## 🧷 Notas acadêmicas

Este repositório auxilia a entrega do seu **TCC em Segurança Cibernética** (FACOM/UFU), com foco em **detecção de anomalias** por ML em tráfego de rede/telemetria de endpoint.

```

---

Se quiser, eu já te entrego também os arquivos de preset (`exp_scan_brute.yaml`, `exp_dos.yaml`, `preset_heavy_hping3.yaml`, `preset_brute_http_hydra.yaml`) e a **UI “Designer (YAML)”** esqueleto para editar/rodar cenários direto do app.

**Base usada para alinhar este pacote**: o CLI/UI e utilitários que você já tem estruturados (geração de Vagrantfile, preflight, status stream, SSH resiliente e carregamento de config) — garantindo que README e .gitignore reflitam o projeto real. :contentReference[oaicite:0]{index=0} :contentReference[oaicite:1]{index=1} :contentReference[oaicite:2]{index=2} :contentReference[oaicite:3]{index=3} :contentReference[oaicite:4]{index=4}

**Checklist de experimentos** (ordem sugerida) já mapeado e compatível com o lab descrito acima — útil pra organizar as execuções e coleta dos dados do TCC. :contentReference[oaicite:5]{index=5}

**Observação sobre normas do TCC**: este repo foi pensado para facilitar a escrita e validação do trabalho no formato exigido pela especialização (SBC/UFU), mantendo os experimentos reprodutíveis. :contentReference[oaicite:6]{index=6}

--- 

Quer que eu já gere os **arquivos de preset** e um **`config.example.yaml` completo** com suas boxes preferidas? Ou quer trocar o nome do repositório por outra vibe (ex.: `sentinel-lab-ids-ml`, `aegisflow-lab`, `anomalix-lab`)?
```
