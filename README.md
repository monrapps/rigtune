# RigTune

Controla o **power limit de GPUs NVIDIA por temperatura**, individualmente, em
Linux/HiveOS. Python 3.6+ e `nvidia-smi`, sem pacotes externos. A potência é
configurada em watts e a temperatura em °C.

O objetivo é **maximizar o hashrate dentro do alvo térmico**, aproveitando a
energia disponível. O controlador aumenta a potência quando há margem térmica
e a reduz quando o ambiente aquece, como durante a tarde. O critério de controle
é a temperatura do núcleo, respeitando os limites de potência de cada placa.
Atingir exatamente o alvo também depende da carga, ventilação e temperatura
ambiente. O hashrate é acompanhado pelo minerador; o RigTune controla o PL pela
temperatura.

## Uso rápido

Obtenha o projeto e execute a partir da pasta criada:

```bash
git clone https://github.com/monrapps/rigtune.git
cd rigtune
python3 -m rigtune list
cp config.example.json config.json
nano config.json
python3 -m rigtune check --config config.json --hardware
python3 -m rigtune run --config config.json --dry-run
```

Depois de conferir as decisões, inicie a aplicação real:

```bash
sudo python3 -m rigtune run --config config.json
```

`Ctrl+C` encerra o controle. O último PL aplicado permanece em vigor; encerrar
não restaura um teto anterior que poderia ser maior. `--once` executa um único
ciclo, preservando as regras de aquecimento e acomodação. `check` sem
`--hardware` valida o arquivo sem consultar GPUs.

O modo `--dry-run` consulta sensores reais e mantém um PL virtual entre ciclos,
mas **não reproduz a resposta térmica a ajustes que não foram aplicados**. Para
testar convergência com atraso térmico, há uma planta simulada nos testes.

## Configuração

O arquivo [config.example.json](config.example.json) contém todas as opções.
Os números são exemplos; confira a faixa de cada modelo com `list`.

```json
{
  "poll_interval_s": 5,
  "command_timeout_s": 10,
  "defaults": {
    "power_min_w": 100,
    "power_max_w": 160,
    "target_temp_c": 60,
    "hysteresis_c": 2,
    "critical_temp_c": 75,
    "step_up_w": 3,
    "step_down_w": 5,
    "fine_step_w": 1,
    "fine_band_c": 5,
    "settle_time_s": 60
  },
  "gpus": [
    { "id": 0 },
    { "id": 1, "power_min_w": 125, "power_max_w": 185, "target_temp_c": 62 }
  ]
}
```

`gpus` é uma lista explícita das placas controladas. `id` aceita um índice do
**nvidia-smi** ou o UUID completo retornado por `list`. Prefira UUID: os índices
podem mudar após reiniciar e podem diferir da ordem do minerador/HiveOS. Ao
iniciar, índices são resolvidos em UUIDs; todas as escritas usam esses UUIDs.
Não são aceitos seletores parciais, GPUs duplicadas ou nomes de campos desconhecidos.

Qualquer campo de `defaults` pode ser sobrescrito dentro de uma entrada de `gpus`.
`power_min_w`, `power_max_w` e `target_temp_c` são obrigatórios, por GPU ou em
`defaults`. A faixa configurada deve caber inteiramente nos limites informados
pelo driver; a validação de todas as GPUs ocorre antes de qualquer alteração.

| Campo | Padrão | Efeito |
| --- | --- | --- |
| `poll_interval_s` | 5 | Intervalo de leitura de todas as GPUs. |
| `command_timeout_s` | 10 | Timeout de cada chamada a `nvidia-smi`. |
| `power_min_w` / `power_max_w` | obrigatórios | Menor e maior PL permitido, em watts inteiros. |
| `target_temp_c` | obrigatório | Temperatura desejada do núcleo. |
| `hysteresis_c` | 2 | Tolerância em torno do alvo. Ex.: 60 ± 2 °C. 0 remove a faixa de tolerância. |
| `critical_temp_c` | 75 | Temperatura bruta que aciona redução ao mínimo. |
| `step_up_w` | 3 | Passo de aumento longe do alvo. |
| `step_down_w` | 5 | Passo de redução longe do alvo. |
| `fine_step_w` | 1 | Passo usado perto do alvo, para ambas as direções. |
| `fine_band_c` | 5 | Distância do alvo na qual o passo fino entra em ação. |
| `settle_time_s` | 60 | Espera mínima após um ajuste confirmado. |
| `warmup_s` | 60 | Tempo contínuo sob carga antes de permitir aumentos. |
| `trend_window_s` | 30 | Janela de leituras usada para medir a tendência térmica. |
| `max_rise_c_per_min` | 0.5 | Bloqueia aumentos se a temperatura subir mais rápido que isso. |
| `temperature_alpha` | 0.3 | Peso da leitura nova na média exponencial; 1 desliga a suavização. |
| `min_utilization_pct` | 30 | Carga mínima para subir PL; usa o maior uso entre GPU e memória. 0 desliga esse filtro. |
| `sensor_failure_limit` | 3 | Falhas consecutivas de temperatura/PL antes de tentar aplicar o mínimo. |
| `recovery_s` | 60 | Tempo com leitura válida e temperatura abaixo do teto da faixa para sair da proteção. |

Os passos devem ser inteiros positivos; `fine_step_w` não pode superar os
passos normais. A janela fina deve superar a tolerância. A temperatura crítica
deve superar `target_temp_c + hysteresis_c`. Acomodação, janela de tendência e
recuperação devem durar pelo menos um intervalo de leitura. Alterações no JSON
entram em vigor ao reiniciar o programa.

## Atraso térmico e passos

O controlador faz uma alteração e espera observar seu efeito:

1. Lê temperatura, utilização, consumo e PL atual. A suavização usa uma média
   exponencial; para decidir, considera o maior valor entre temperatura bruta
   e suavizada, evitando que a média esconda um aquecimento recente.
2. Dentro da tolerância, mantém o PL. Acima dela, reduz; abaixo dela, pode
   aumentar se houver carga e o aquecimento inicial tiver terminado.
3. Usa passos menores na vizinhança do alvo. Com alvo 60, tolerância 2 e faixa
   fina 5: a 56 °C sobe 1 W; a 50 °C sobe 3 W; a 64 °C desce 1 W; a 70 °C desce 5 W.
4. Confirma o novo PL com outra leitura do driver. **O tempo de acomodação
   começa depois dessa confirmação**, e o histórico de tendência é reiniciado.
5. Durante `settle_time_s`, continua monitorando, mas aguarda antes de outro
   ajuste normal. Depois disso, só permite um aumento se houver uma janela
   completa de observações e a taxa de subida estiver abaixo do limite.

A tendência usa regressão linear das temperaturas brutas pelo tempo monotônico.
Uma subida lenta abaixo do limite ainda permite aumento; a regra evita empilhar
passos enquanto a placa responde rapidamente ao anterior. Reduções normais
também respeitam a acomodação. Temperatura crítica e proteção por falha de sensor
podem agir imediatamente, assim como corrigir um PL acima do máximo configurado.

Comece com os tempos e passos pequenos do exemplo e observe as curvas na própria
rig. Se a resposta demorar mais, aumente `settle_time_s` e `trend_window_s`; se
houver oscilação, reduza os passos. Ventoinhas variando rapidamente alteram essa
resposta e dificultam atribuir a mudança térmica ao PL.

Ao entrar com um PL acima do máximo, o programa o reduz diretamente ao máximo.
Se estiver abaixo do mínimo, só o eleva até o mínimo quando as condições para
um aumento forem atendidas. Esse enquadramento inicial e as reduções de proteção
são exceções ao tamanho dos passos. Uma GPU quente com PL abaixo do mínimo nunca
recebe um aumento para enquadrá-la na faixa.

## Proteções e limites

- Temperatura crítica: tenta aplicar `power_min_w` imediatamente, sem esperar
  a média ou a acomodação. Mantém a proteção até completar `recovery_s` em
  condições válidas e frias; depois exige novamente o aquecimento sob carga.
- Leituras ausentes, `N/A`, inválidas ou timeout: bloqueia aumentos desde a
  primeira falha; ao atingir o limite de falhas, tenta aplicar o mínimo usando
  o UUID já conhecido. A proteção é independente por GPU.
- Falha de escrita ou leitura posterior diferente do PL solicitado: registra
  erro e entra em proteção, tentando aplicar o mínimo no próximo ciclo.
- Ociosa ou utilização desconhecida: não aumenta PL. Reduções por calor
  continuam disponíveis. Se um canal de utilização estiver disponível, ele é usado.
- Alvo inviável: fica no limite da faixa. `hot_at_minimum` registra que a placa
  continua quente mesmo no menor PL configurado; `cold_at_maximum` indica o contrário.
- Detecta mudanças externas no PL e reinicia a acomodação. Locks por UUID em
  `/run/lock/rigtune` impedem duas instâncias ativas deste programa na mesma GPU.

O mínimo configurado é também o PL de proteção, portanto escolha-o considerando
o modelo e a carga. O programa controla a **temperatura do núcleo**, não a da
memória/hotspot, nem fans ou clocks. Ele não interrompe o minerador. Se o driver
deixar de aceitar comandos, a redução não poderá ser garantida; preserve as
proteções térmicas do driver e os mecanismos de parada do HiveOS.

## HiveOS e systemd

O HiveOS utiliza watts absolutos para PL. Perfis de overclock, trocas de
algoritmo e outros programas podem reaplicar limites. Evite ter outro ajuste
periódico de PL disputando a mesma GPU; alterações externas aparecem no log.
O AutoFan atua na ventilação e também muda a temperatura: ajuste seus parâmetros
de forma coerente com o alvo térmico do RigTune e observe a interação dos dois.

Para instalar, já dentro da rig e da pasta do projeto:

```bash
sudo install -d /opt/rigtune /etc/rigtune
sudo cp -r rigtune /opt/rigtune/
sudo install -m 0644 config.example.json /etc/rigtune/config.json
sudo nano /etc/rigtune/config.json
cd /opt/rigtune
sudo python3 -m rigtune check --config /etc/rigtune/config.json --hardware
sudo python3 -m rigtune run --config /etc/rigtune/config.json --dry-run
```

Encerre a simulação e, novamente na pasta original do projeto, instale e ative
o serviço:

```bash
sudo install -m 0644 deploy/rigtune.service /etc/systemd/system/rigtune.service
sudo systemctl daemon-reload
sudo systemctl enable --now rigtune
sudo journalctl -u rigtune -f
```

Nas atualizações, preserve `/etc/rigtune/config.json`. Para aplicar uma mudança
de configuração use `sudo systemctl restart rigtune`; para parar use
`sudo systemctl disable --now rigtune`. O serviço reinicia em falhas de execução;
erros de configuração (saída 2) exigem correção antes de reiniciar manualmente.

Logs mostram UUID, temperatura bruta/suavizada, tendência, consumo, PL e motivo
da decisão. Estados estáveis geram um registro a cada minuto; transições e
ajustes são registrados imediatamente. Falhas por GPU mantêm o laço em execução
para permitir recuperação. Em `--once`, saída 1 indica falha/proteção ou calor
no mínimo; 0 indica ciclo sem esses estados.

## Testes

```bash
python3 -m unittest discover -v
```

Os testes usam sensores e comandos simulados, incluindo uma planta térmica com
constante de tempo de 90 segundos. Cobrem passos, tolerância, acomodação após
confirmação, tendência, carga, limites, falhas de driver/sensores, recuperação,
seleção por UUID após reordenação, exclusão entre processos e dry-run sem escritas.
Nenhum teste dessa suíte altera GPUs reais.

### Validação na Hades

Em 13/10/2026, os 43 testes passaram localmente em Python 3.13 e na Hades em
Python 3.10.12, Ubuntu 22.04.5 e driver NVIDIA 580.173.02. A unidade systemd
passou em `systemd-analyze verify`. A compatibilidade sintática com Python 3.6
foi verificada; a execução nessa versão antiga ainda não foi testada.

Com autorização para testar a rig, foi executado um ensaio de 66 segundos na
RTX 3060: **175 → 174 → 175 W**, confirmando a escrita, a acomodação de 60 segundos
e a restauração. A GTX 1080 Ti manteve 185 W. A faixa do ensaio foi limitada a
174–175 W, com alvo artificial de 50 °C; `hot_at_minimum` após a acomodação era
esperado, pois a placa permaneceu a 58 °C. Esse ensaio valida a integração com
hardware; a convergência térmica foi validada na planta simulada.

O [registro do teste](reports/hades-hardware-smoke.json) contém as leituras e
confirmações. [config.hades.example.json](config.hades.example.json) usa os UUIDs
reais e foi atualizado para a configuração de operação solicitada: alvo de
**62 °C**, tolerância de **0 °C** e faixas completas informadas pelo driver:
**125–300 W na GTX 1080 Ti** e **100–212 W na RTX 3060**. O controlador aumenta
ou reduz o PL gradualmente dentro dessas faixas; os máximos não são aplicados
diretamente. As métricas de desempenho relevantes são hashrate, temperatura e
estabilidade das shares.

A cópia do ensaio ficou em `/tmp/rigtune-test.BpVI6I` na Hades. Após encerrar o
ensaio e restaurar seus limites originais, a operação contínua foi instalada em
`/opt/rigtune`, com configuração em `/etc/rigtune/config.json` e serviço
`rigtune.service` habilitado para iniciar no boot. Para acompanhar a operação:

```bash
ssh root@Hades 'systemctl status rigtune --no-pager'
ssh root@Hades 'journalctl -u rigtune -f'
```

### Operação na T37

A T37 também executa `rigtune.service`, habilitado para iniciar no boot, com
**cinco GTX 1080 Ti**. A configuração usa **alvo de 62 °C**, **tolerância de 0 °C**,
**acomodação de 60 segundos** e a faixa de hardware de **125–300 W por GPU**.
As placas são selecionadas por UUID no arquivo
[config.t37.example.json](config.t37.example.json).

O programa está em `/opt/rigtune` e a configuração ativa em
`/etc/rigtune/config.json` na T37. Para acompanhar:

```bash
ssh root@T37 'systemctl status rigtune --no-pager'
ssh root@T37 'journalctl -u rigtune -f'
```

## Referências

- [NVIDIA SMI](https://docs.nvidia.com/deploy/nvidia-smi/index.html): consulta de
  sensores, identificação estável por UUID e power limit dentro dos limites do hardware.
- [Overclocking no HiveOS](https://hiveon.com/knowledge-base/getting_started/start_oc/):
  PL em watts e configuração de perfis.
- [AutoFan do HiveOS](https://hiveon.com/knowledge-base/guides/autofan/): controle
  de ventilação por temperatura.
