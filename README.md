# VagrantLabUI

UI futurista (PySide6) para orquestrar laboratório de ataques/defesa com Vagrant.

## Pré-requisitos
- VirtualBox 7.x
- Vagrant >= 2.4.x
- Python 3.10+

## Instalação
```bash
python -m venv .venv
. .venv/Scripts/activate  # Windows
pip install -r requirements.txt
```

## Configuração
Edite `config.yaml` para definir **attacker**, **victim** e **sensor** (IPs, CPU, memória, *provisioners*).

## Execução da UI
```bash
python -m app.ui.main
```

## Automação (CLI)
```bash
python manage_lab.py --write-vagrantfile --up --status
python manage_lab.py --halt
python manage_lab.py --destroy
```

> Dica: use `--name attacker` para agir numa única VM.

## Próximos passos (integração TCC)
- Botões “Start Capture”/“Stop Capture” no **sensor** (Zeek/tcpdump/NFStream)
- Painel de **anotações de experimento** (tempo início/fim, IP alvo, TTP ATT&CK)
- Export de **artefatos** (logs+pcap) para `datasets/EXP##_...` com `zip`