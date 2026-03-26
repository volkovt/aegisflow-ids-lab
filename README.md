# Vagrant Lab UI

## Visão geral

O **Vagrant Lab UI** é uma aplicação desktop em **PySide6** criada para **orquestrar um laboratório de experimentos de segurança e geração de datasets** com base em **Vagrant**, **SSH** e **roteiros YAML**. Na prática, a aplicação junta quatro responsabilidades principais:

1. **Infraestrutura do laboratório**: gera o `Vagrantfile`, sobe/desliga/destroi máquinas virtuais e consulta estado do ambiente.
2. **Execução operacional**: estabelece SSH nas VMs, executa comandos remotos com tolerância a falhas e expõe ações pela interface.
3. **Experimentos guiados por YAML**: permite escolher ou editar um YAML de experimento, transformar esse YAML em passos executáveis e conduzir a execução pelo “Guia do Experimento”.
4. **Pós-processamento / dataset**: após a captura, aciona a geração de manifesto e ETL para produzir artefatos de dataset.

A aplicação foi desenhada para um cenário de **laboratório IDS / ML / tráfego de rede**, com papéis como **attacker**, **victim** e **sensor**, incluindo suporte a captura de tráfego, execução de perfis de ataque e coleta de artefatos.

---

## Propósito da aplicação

O propósito central é **tirar o laboratório do modo manual**.

Em vez de o operador:
- editar manualmente um `Vagrantfile`,
- subir VMs por terminal,
- descobrir IPs,
- abrir SSH máquina por máquina,
- rodar scripts de ataque/coleta manualmente,
- organizar capturas e então disparar ETL,

a aplicação concentra tudo isso em uma interface única.

Ela serve especialmente para cenários como:
- validação de ambientes de experimento;
- execução repetível de ataques controlados;
- coleta de tráfego e artefatos;
- produção de insumos para análise posterior;
- prototipação de experimentos reprodutíveis com YAML.

---

## O que a aplicação faz

### 1. Carrega a configuração do laboratório

A aplicação lê um `config.yaml` e transforma esse arquivo em uma estrutura tipada contendo:
- `project_name`
- `lab_dir`
- `provider`
- `network`
- `machines`

Cada máquina possui, no mínimo:
- `name`
- `box`
- `hostname`
- `cpus`
- `memory`
- `ip_last_octet`

E opcionalmente:
- `synced_folders`
- `provision`

Essa configuração também é convertida em contexto para renderizar o `Vagrantfile` via template Jinja2.

### 2. Descobre a raiz do projeto e o arquivo de configuração

A resolução do `config.yaml` acontece na seguinte ordem:
1. variável de ambiente `VAGRANTLAB_CONFIG`;
2. caminho explícito informado ao resolver;
3. `config.yaml` no diretório atual;
4. `config.yaml` na raiz do projeto.

A própria detecção de raiz usa uma heurística que procura por `manage_lab.py` e pela pasta `app`.

### 3. Gera e sincroniza o Vagrantfile

O `VagrantManager`:
- gera o `lab/Vagrantfile` a partir de template;
- evita regravação desnecessária usando hash;
- faz `up`, `halt`, `destroy`, `status`;
- garante que uma VM exista e esteja rodando;
- valida se o SSH realmente está pronto.

### 4. Executa pré-checagens do ambiente

O **preflight** verifica:
- Vagrant instalado;
- VirtualBox instalado, quando o provider é `virtualbox`;
- cliente SSH disponível;
- espaço mínimo em disco;
- existência do `Vagrantfile`;
- status do Vagrant;
- presença de rede host-only compatível com o prefixo do laboratório;
- detalhes por VM:
  - estado,
  - `ssh-config`,
  - chave privada,
  - conectividade na porta SSH.

Além disso, grava um relatório em:

```text
.logs/lab_preflight.txt
```

### 5. Mantém conexões SSH persistentes

O `SSHManager` implementa uma camada relativamente robusta de execução remota:
- reaproveitamento de conexões Paramiko;
- parser robusto de `vagrant ssh-config`;
- validação de porta e banner SSH;
- retries;
- fallback para `vagrant ssh -c` quando necessário;
- execução por stdin em shell limpo (`bash --noprofile --norc`);
- abertura opcional de terminal externo conectado à VM.

Isso sugere preocupação com estabilidade de execução em laboratório.

### 6. Expõe tudo em uma interface gráfica

A UI principal é montada em `app/ui/main_window.py`.

Ela instancia:
- `VagrantManager`
- `SSHManager`
- `PreflightEnforcer`
- `DatasetController`
- `WarmupCoordinator`
- `TaskManager`
- `MachineInfoService`
- `VagrantContextService`
- `PresetBootstrapper`
- `MainController`

A tela possui um dock lateral com ações de infraestrutura e experimento, além de cards das máquinas e área de logs.

### 7. Orquestra o fluxo pelo MainController

O `MainController` concentra a lógica de aplicação e deixa a janela mais enxuta. Ele:
- integra logs à UI;
- mantém o YAML atual selecionado;
- dispara geração do `Vagrantfile`;
- consulta status;
- sobe/desliga/destroi VMs;
- sincroniza status com os cards;
- atualiza informações de máquina;
- aciona o fluxo de dataset;
- trabalha com o contexto de experimento para renderizar infraestrutura e execução.

### 8. Trabalha com experimentos baseados em YAML

O projeto possui YAMLs em:
- `app/templates/hydra_attack.yaml`
- `app/templates/hydra_extended_attack.yaml`
- `app/templates/official_steps.yaml`
- `lab/experiments/*.yaml`
- `lab/templates/*.yaml`

O parser:
- carrega YAML de forma segura;
- aceita variáveis globais;
- resolve placeholders como:
  - `{attacker_ip}`
  - `{victim_ip}`
  - `{sensor_ip}`
- aplica parâmetros por step;
- gera comandos derivados (`command_normal`, `command_b64`, etc.);
- monta passos executáveis para o guia.

### 9. Possui um Guia do Experimento

O `ExperimentGuideDialog` e os `StepCard`s transformam o experimento em um fluxo visual.

Cada passo pode:
- ser executado individualmente;
- ter comando copiado;
- abrir SSH para o host correspondente;
- ser marcado;
- participar de execução em lote.

O guia também expõe ações operacionais extras, como:
- rodar todos os passos;
- isolamento do atacante;
- acionar o “Runner” para manifesto + ETL.

### 10. Gera dataset ao final do fluxo

No guia, existe um fluxo assíncrono que:
1. procura capturas em `lab/data/captures`;
2. chama `lab.data.manifest.get_latest_run_ts` e `build_manifest`;
3. salva `manifest.json`;
4. chama `lab.data.etl.run_etl_from_manifest`;
5. grava saídas em `lab/data/processed/<run_ts>`.

Ou seja: a aplicação não fica só na execução do laboratório, ela também participa da **pipeline de materialização do dataset**.

---

## Arquitetura resumida

```text
app/
  core/
    config_loader.py
    pathing.py
    vagrant_manager.py
    ssh_manager.py
    preflight.py
    preflight_enforcer.py
    yaml_parser.py
    workers/
  ui/
    main.py
    main_window.py
    controllers/
    services/
    components/
    guide/
    templates/
lab/
  Vagrantfile
  agents/
  experiments/
  templates/
  security/
  shared/
requirements.txt
```

### Camadas

#### Core
Responsável pela lógica de infraestrutura e execução:
- leitura de config;
- geração de Vagrantfile;
- SSH;
- preflight;
- parsing de YAML.

#### UI
Responsável pela interface:
- janela principal;
- cards das máquinas;
- dock de ações;
- diálogo do guia;
- designer de YAML.

#### Services
Apoio à UI:
- coleta de informações de máquina;
- task manager;
- bootstrap de presets;
- construção de contexto para Vagrant.

#### Controllers
Coordenação de fluxo:
- `MainController`
- `DatasetController`

#### Lab
Material do ambiente de experimento:
- Vagrantfile do laboratório;
- agentes;
- experimentos;
- scripts;
- templates.

---

## Fluxo de uso da aplicação

## 1. Preparar o ambiente

Você precisa ter no host:

### Dependências Python
Instaladas via `requirements.txt`:

```txt
PySide6==6.7.2
paramiko==3.4.0
PyYAML==6.0.2
Jinja2==3.1.6
colorama==0.4.6
qtawesome
```

### Dependências externas
Pelo código de preflight, são esperados:
- **Vagrant**
- **VirtualBox** (quando `provider: virtualbox`)
- **cliente SSH**
- espaço em disco suficiente
- rede host-only compatível com o prefixo configurado

---

## 2. Instalar dependências Python

### Windows
```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### Linux/macOS
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## 3. Criar ou revisar o config.yaml

O projeto espera um `config.yaml`. Um exemplo compatível com a estrutura encontrada no código seria:

```yaml
project_name: "lab-ids-ml"
lab_dir: "lab"
provider: "virtualbox"

network:
  ip_base: "192.168.56."

machines:
  - name: "attacker"
    box: "generic/ubuntu2204"
    hostname: "attacker"
    cpus: 2
    memory: 2048
    ip_last_octet: 11
    synced_folders:
      - host: "./lab"
        guest: "/home/vagrant/lab"
    provision:
      - inline: |
          echo "Provision attacker"

  - name: "victim"
    box: "generic/ubuntu2204"
    hostname: "victim"
    cpus: 2
    memory: 2048
    ip_last_octet: 12

  - name: "sensor"
    box: "generic/ubuntu2204"
    hostname: "sensor"
    cpus: 2
    memory: 2048
    ip_last_octet: 13
```

### Observações
- `ip_last_octet` é usado junto com `network.ip_base`.
- `synced_folders` e `provision` são opcionais.
- se quiser usar outro caminho, você pode apontar a variável de ambiente:

```bash
set VAGRANTLAB_CONFIG=C:\caminho\config.yaml
```

ou no Linux/macOS:

```bash
export VAGRANTLAB_CONFIG=/caminho/config.yaml
```

---

## 4. Executar a aplicação

O ponto de entrada gráfico confirmado no código é:

```text
app/ui/main.py
```

Você pode iniciar de uma destas formas:

```bash
python app/ui/main.py
```

ou, se o pacote estiver configurado corretamente no seu ambiente:

```bash
python -m app.ui.main
```

---

## 5. Operar a infraestrutura pela interface

No dock lateral, as ações principais são:

### Infra
- **Gerar Vagrantfile**
- **Subir todas**
- **Status**
- **Halt todas**
- **Destroy todas**
- **Preflight**

### Dataset & Experimentos
- **Designer (YAML)**
- **Escolher YAML**
- **Guia do Experimento**

---

## Fluxo recomendado de uso

### Etapa 1 — Gerar Vagrantfile
Use **Gerar Vagrantfile** para materializar o arquivo do laboratório a partir do template e do contexto da configuração.

### Etapa 2 — Executar preflight
Use **Preflight** antes de subir o ambiente. Isso antecipa problemas de:
- instalação do Vagrant;
- VirtualBox;
- SSH;
- rede host-only;
- espaço em disco;
- inconsistências por VM.

### Etapa 3 — Subir as VMs
Use **Subir todas** ou suba as VMs necessárias. O sistema:
- garante criação;
- aguarda o SSH ficar de fato pronto;
- marca uma janela de “warmup” de 30s.

### Etapa 4 — Validar status
Use **Status** para refletir o estado das VMs e atualizar os cards com:
- estado;
- endpoint SSH;
- IP guest;
- identificação amigável do SO.

### Etapa 5 — Selecionar ou editar um experimento YAML
Você pode:
- escolher um YAML existente;
- abrir o designer;
- usar os templates presentes no projeto.

### Etapa 6 — Abrir o Guia do Experimento
No guia, os steps são exibidos de forma operacional. Você pode:
- executar passo a passo;
- copiar comandos;
- rodar tudo em lote;
- abrir SSH diretamente;
- acionar o runner de geração de dataset.

### Etapa 7 — Gerar dataset
Ao final, o runner:
- gera `manifest.json`;
- executa ETL;
- produz saídas em `lab/data/processed`.

---

## Componentes principais

## `app/core/config_loader.py`
Responsável por transformar o YAML de configuração em objetos Python usados no restante da aplicação.

## `app/core/pathing.py`
Resolve a raiz do projeto e localiza `config.yaml`.

## `app/core/vagrant_manager.py`
Camada de infraestrutura Vagrant:
- status;
- up/halt/destroy;
- geração e sincronização do Vagrantfile;
- espera ativa por SSH.

## `app/core/preflight.py`
Validação do ambiente antes da execução do laboratório.

## `app/core/preflight_enforcer.py`
Força checagens curtas de prontidão SSH com cache temporal para não repetir validações desnecessárias.

## `app/core/ssh_manager.py`
Camada robusta de SSH com:
- pool de conexões;
- execução remota;
- retries;
- fallback;
- terminal externo.

## `app/core/yaml_parser.py`
Converte YAML em passos concretos e faz substituição de variáveis e IPs.

## `app/core/data_collector.py`
Controla uma janela de aquecimento para serializar coleta logo após boot das VMs.

## `app/ui/main_window.py`
Janela principal e composição dos serviços.

## `app/ui/controllers/main_controller.py`
Orquestra a lógica de negócio da UI.

## `app/ui/guide/guide_dialog.py`
Tela operacional do experimento.

## `app/ui/components/machine_card.py`
Representação visual das máquinas com estado, avatar e informações resumidas.

---

## Estrutura esperada de diretórios importantes

### Logs
```text
.logs/
  lab.log
  lab_preflight.txt
```

### Templates e YAMLs
```text
app/templates/
lab/templates/
lab/experiments/
```

### Dados de captura e processamento
```text
lab/data/captures/
lab/data/processed/
```

---

## O que aparece na interface

A interface tende a exibir:

- **Cards por máquina**
  - nome
  - status
  - avatar/ícone por papel
  - endpoint de host
  - IP guest
  - SO amigável

- **Área de logs**
  - logs internos da aplicação
  - logs de agentes
  - logs de execução de passos
  - feedback do runner

- **Dock de ações**
  - infraestrutura
  - experimentos
  - navegação operacional

---

## Como os experimentos são montados

O parser de YAML permite montar passos com:
- título
- descrição
- host alvo
- timeout
- script
- tags
- artefatos
- parâmetros
- placeholders de IP

Exemplo conceitual de step:

```yaml
- id: sensor_capture_start
  title: "Sensor | Iniciar captura"
  host: sensor
  timeout: 30
  script: |
    set -Eeuo pipefail
    TS=$(date +%Y%m%d_%H%M%S)
    mkdir -p "{cap_dir}" "{log_dir}"
    victim="{victim_ip}"
    attacker="{attacker_ip}"
    echo "Capturando tráfego..."
```

O parser renderiza isso com o contexto do ambiente e converte o script em comando executável.

---

## Exemplo de uso prático

Um fluxo realista seria:

1. abrir a aplicação;
2. carregar o `config.yaml`;
3. clicar em **Gerar Vagrantfile**;
4. clicar em **Preflight**;
5. clicar em **Subir todas**;
6. clicar em **Status**;
7. escolher `hydra_attack.yaml` ou outro experimento;
8. abrir **Guia do Experimento**;
9. executar os passos do cenário;
10. no final, rodar o **Runner**;
11. coletar o `manifest.json` e os arquivos em `lab/data/processed`.

---

## Requisitos funcionais inferidos

Com base no código, os requisitos funcionais principais são:

- carregar configuração do laboratório;
- gerar infraestrutura Vagrant;
- controlar ciclo de vida das VMs;
- garantir SSH estável;
- exibir informações do ambiente;
- editar/selecionar YAMLs de experimento;
- traduzir YAML em passos executáveis;
- executar comandos remotos por host;
- produzir artefatos de captura e dataset;
- registrar logs de operação.

---

## Requisitos não funcionais inferidos

A aplicação também mostra preocupação com:

- **robustez operacional**
  - retries
  - fallback de SSH
  - validação de banner
  - cache de prontidão

- **observabilidade**
  - logs em arquivo
  - logs integrados à UI
  - relatórios de preflight

- **usabilidade**
  - interface gráfica
  - cards de máquina
  - dock de ações
  - guia passo a passo

- **reprodutibilidade**
  - experimentos declarados em YAML
  - Vagrantfile gerado por template
  - ETL baseado em manifesto

---

## Pontos fortes da aplicação

### 1. Boa separação de responsabilidades
O projeto tem uma divisão clara entre:
- core,
- ui,
- services,
- controllers,
- lab.

### 2. Camada SSH mais madura que o comum
A implementação não depende de chamadas simples e frágeis; ela tenta manter conexões persistentes e ter fallback.

### 3. Preflight útil
Há validação real do ambiente, inclusive por VM.

### 4. Orquestração por YAML
Isso facilita repetibilidade e evolução de cenários.

### 5. Pipeline além da execução
A aplicação vai até a geração de manifesto e ETL, o que é muito útil para experimentos de dataset.

---

## Limitações e observações importantes

### 1. O README original não foi encontrado no material analisado
Este documento foi montado a partir do código indexado no manifesto.

### 2. Nem todos os arquivos referenciados estavam detalhados por completo
Alguns módulos aparecem citados, mas não totalmente expandidos no material acessível.

### 3. Há módulos referenciados mas não detalhados no índice visível
Exemplo:
- `lab.data.manifest`
- `lab.data.etl`

Eles são usados pelo guia/runner, mas o conteúdo deles não apareceu no material principal analisado.

### 4. O arquivo `manage_lab.py` é mencionado na heurística de descoberta da raiz do projeto, mas o conteúdo dele não apareceu no material indexado
Por isso, este README foi escrito com base no **entrypoint gráfico confirmado**, e não em uma eventual CLI externa.

### 5. O foco confirmado é principalmente Linux nas VMs
A camada de detecção de SO tem fallback, mas o fluxo operacional principal usa `bash` e pressupostos típicos de Linux.

---

## Problemas comuns e diagnóstico

## Erro: `config.yaml não encontrado`
Verifique:
- se o arquivo existe na raiz do projeto;
- se está no diretório atual;
- se a variável `VAGRANTLAB_CONFIG` está apontando corretamente.

## Erro no preflight sobre Vagrant
Instale o Vagrant e confirme no terminal:

```bash
vagrant --version
```

## Erro no preflight sobre VirtualBox
Se `provider` for `virtualbox`, valide:

```bash
VBoxManage --version
```

## Erro de SSH
A aplicação depende de:
- VM criada;
- VM ligada;
- `vagrant ssh-config` funcional;
- porta SSH acessível;
- banner SSH válido.

Se necessário, valide manualmente:
```bash
vagrant status
vagrant ssh-config attacker
```

## Erro ao gerar dataset
Verifique se existe:

```text
lab/data/captures
```

Sem capturas válidas, o runner não consegue gerar o manifesto nem o ETL.

---

## Sugestão de seção “Como usar” para um README oficial do projeto

```md
## Como usar

1. Instale Vagrant, VirtualBox e SSH.
2. Instale as dependências Python com `pip install -r requirements.txt`.
3. Configure o `config.yaml`.
4. Execute `python app/ui/main.py`.
5. Gere o `Vagrantfile`.
6. Rode o `Preflight`.
7. Suba as VMs.
8. Escolha um YAML de experimento.
9. Abra o Guia do Experimento.
10. Execute os passos.
11. Gere o dataset ao final.
```

---

## Conclusão

O **Vagrant Lab UI** é, essencialmente, um **orquestrador visual de laboratório de segurança e coleta de dados**, com foco em:
- repetibilidade,
- automação,
- segurança operacional,
- execução guiada por YAML,
- e geração de artefatos para análise / dataset.

Ele vai além de uma simples interface para Vagrant: ele conecta **infraestrutura**, **execução remota**, **roteiros experimentais** e **pós-processamento** em uma mesma aplicação.