# Jogo da Forca cliente/servidor com servidor resiliente

Trabalho de Sistemas Distribuídos. Dois jogadores disputam a mesma palavra, e o
serviço continua funcionando quando a máquina que o atende morre.

Python 3. O servidor usa **só biblioteca padrão**; o cliente gráfico usa **PyQt6**:

```powershell
py -m pip install PyQt6
```

Use `py`, não `python` — em máquinas onde o Python veio da Microsoft Store, o
comando `python` é apenas um atalho que não executa nada.

## Requisitos

| Requisito | Onde está |
|---|---|
| Socket para comunicação | `protocolo.py`, `servidor.py`, `cliente_gui.py` |
| Servidor resiliente (se cair, outro assume) | `replicacao.py`, `promocao.py`, `infra/agente_fencing.py` |
| Sala de espera, múltiplas requisições | `sala.py` |
| Máximo 2 jogadores por partida | `sala.py` |
| Servidor como semáforo, um por vez | `servidor.py` — event loop de thread única |
| Bonequinho de cada jogador visível para ambos | `cliente_gui.py`, mensagem `estado` |

## Arquivos

| Arquivo | Responsabilidade |
|---|---|
| `protocolo.py` | Enquadramento de mensagens no fluxo TCP |
| `jogo.py` | Regras da forca, sem rede |
| `sala.py` | Sala de espera, convites, fila, tokens |
| `servidor.py` | Event loop, protocolo, replicação, promoção |
| **`cliente_gui.py`** | **Cliente gráfico em PyQt6 — é esta a entrega** |
| `cliente.py` | Cliente de terminal — **ferramenta de teste**, não é entregável |
| `replicacao.py` | Canal do primário para o backup |
| `promocao.py` | Fencing e takeover, no backup |
| `sonda.py` | Mede indisponibilidade no failover |
| `infra/` | VMs, IP virtual, agente de fencing, injeção de falha |

## Endereços

| Endereço | Papel |
|---|---|
| `192.168.56.10` | **IP virtual** — clientes conectam aqui; flutua entre as VMs |
| `192.168.56.11` / `.12` | VM1 / VM2, fixos — heartbeat e replicação |
| `192.168.56.1` | host Windows — clientes e agente de fencing |

Rede host-only, não bridge: funciona em qualquer rede, sem interferência de VPN.

## Como rodar

### Local (sem failover)

```powershell
py servidor.py --nome LOCAL
py cliente_gui.py Pedro
py cliente_gui.py Ana
```

### Completo (com duas VMs)

Requer VirtualBox, VMs `VM1` e `VM2` em host-only, **1 núcleo cada**, chave em
`~/.ssh/forca_vm`. Para montar do zero: `infra/criar_vms.ps1`,
`infra/instalar_vms.ps1`, `infra/provisionar.ps1`.

**1. Agente de fencing, no host** — sem ele a VM2 assume sem confirmar o
desligamento, e o split-brain deixa de ser teórico:

```powershell
py infra\agente_fencing.py --host 192.168.56.1 --porta 5010
```

**2. Backup (VM2)** — entre na VM primeiro, pelo PowerShell:

```powershell
ssh -i "$env:USERPROFILE\.ssh\forca_vm" aluno@192.168.56.12
```

E **dentro da VM**, em uma linha (o `\` de continuação é do bash e quebra se você
colar isso no PowerShell):

```bash
cd ~/JogoDaForca && python3 servidor.py --nome VM2 --porta 5000 --porta-replicacao 5001 --ip-par 192.168.56.11 --agente-fencing 192.168.56.1:5010 --vm-par VM1 --ip-virtual 192.168.56.10/24 --interface proj0
```

`--ip-par` não é opcional na prática: sem ele, qualquer um na rede conecta na porta
de replicação, se passa pelo primário, e ao cair faz o backup desligar a máquina
saudável pelo fencing.

**3. Primário (VM1)** — outra janela:

```powershell
ssh -i "$env:USERPROFILE\.ssh\forca_vm" aluno@192.168.56.11
```

```bash
sudo -n ~/JogoDaForca/infra/assumir_ip.sh 192.168.56.10/24 proj0
cd ~/JogoDaForca && python3 servidor.py --nome VM1 --porta 5000 --par 192.168.56.12:5001
```

**4. Clientes, no host:**

```powershell
py cliente_gui.py Pedro --host 192.168.56.10
py cliente_gui.py Ana   --host 192.168.56.10
```

### Como jogar

Selecione um jogador e clique **Convidar** (ou duplo clique na linha). **Enter** aceita um convite, **Esc** recusa. Durante a partida, clique a letra ou digite no teclado.

## Roteiro de demonstração

O cabeçalho do cliente mostra **qual servidor atende** — é o que torna o failover
visível.

**1. Jogo e semáforo.** Duplo clique no adversário para convidar, **Enter** para
aceitar. Mostrar: os dois bonequinhos aparecem para ambos; o turno alterna a cada
chute; as letras ficam **verdes quando acertam e vermelhas quando erram**; fora da
vez o teclado inteiro fica desabilitado. Com `MAÇÃ`, chutar `A` revela o `Ã`.
No fim aparece a tela de **vitória/derrota com a palavra revelada**, que volta
sozinha para a sala em 12 s (ou no **Enter**).

**2. Sala de espera.** Quatro clientes, duas duplas. A segunda fica `na_fila` e vê a
posição; quando a primeira partida acaba, ela assume o slot sozinha. Mostrar também
convite recusado com **Esc**, convite expirando em 30 s, e dois clientes com o
mesmo nome (o segundo entra como `Joao742`).

**2b. Prazo da jogada.** Suba o servidor com `py servidor.py --nome LOCAL
--prazo-jogada 20` — 3 min não cabem numa apresentação. O contador aparece sob
`SUA VEZ` e fica **vermelho** nos últimos segundos; ao zerar, quem estava na vez
perde e o outro vence, com o placar mostrando que **nenhum erro foi somado**.
Mostrar também que o contador **congela** enquanto a partida está suspensa.

**3. Reconexão, sem failover.** Feche a janela de um cliente e reabra com o mesmo
nome. O adversário vê **PARTIDA SUSPENSA** com o contador regressivo andando; o
cliente reaberto retoma painel, erros, turno e chutadas exatamente como estavam.
Em outra tentativa, deixe o prazo vencer: a partida é anulada.

**4. Replicação.** Com jogadores na partida pelo IP virtual, abra um cliente direto
na VM2 (`py cliente_gui.py Espiao --host 192.168.56.12`): ela lista os jogadores
sem nunca ter falado com eles.

**5. Failover automático.** Com partida em andamento, mate a VM1:

```powershell
& "C:\Program Files\Oracle\VirtualBox\VBoxManage.exe" controlvm VM1 poweroff
```

Na tela do cliente aparece uma **faixa laranja de aviso**, o topo troca de
`servidor: VM1` para `servidor: VM2` **piscando**, e a partida continua. No log da VM2:
`3 batidas perdidas` → `fencing confirmado` → `IP virtual assumido` →
`PROMOÇÃO concluída`. Cerca de **12 s** fora do ar; `py sonda.py --host
192.168.56.10` mede.

**5b. A janela impossível de acertar na mão.** `py infra\armar_falha.py --host
192.168.56.10` faz o servidor morrer **entre** replicar e confirmar — janela de
fração de milissegundo. O cliente não recebe confirmação, reenvia o lance após o
failover, e ele conta **uma única vez**.

## Decisões que sustentam o projeto

- **Replicar antes de confirmar ao cliente.** Replicar depois deixaria o backup
  negando uma letra que os jogadores já viram: divergência permanente. Antes, a
  única dúvida é "meu lance valeu?", que o reenvio com identificador resolve.
- **Timeout de 300 ms na replicação.** Sem ele, um backup que para de responder sem
  fechar a conexão penduraria o primário por ~15 min de retransmissão TCP — e a
  morte do redundante derrubaria o principal.
- **Desconexão suspende, não anula.** Failover desconecta os dois clientes por
  definição; anular na desconexão anularia toda partida que o failover salvasse.
- **Fencing por terceira autoridade.** Com dois nós não existe quórum, e o silêncio
  não diz se o outro morreu ou se a rede partiu. Quem consegue desligar o par pelo
  hypervisor vence.
- **Prazo de 3 min por jogada.** O semáforo garante que só um joga por vez, mas
  não garante que a vez acabe: um jogador conectado e parado congelava a partida
  para sempre, e com ela o slot da próxima dupla. O prazo é o que fecha isso. Ele
  fica com o servidor, não com o cliente — relógio de cliente é relógio que o
  jogador controla. E o estouro **não conta como erro**: o boneco para onde
  parou, para quem lê a tela no fim entender que foi o relógio, não a forca.
- **Event loop de thread única.** Elimina por construção a corrida de
  *check-then-act* na sala, e cumpre "um jogador por vez" literalmente.

## Parâmetros

| Parâmetro | Valor |
|---|---|
| Heartbeat | 2 s |
| Batidas perdidas para declarar morte | 3 (6 s) |
| Timeout de replicação | 300 ms — muito menor que os 6 s de detecção |
| Prazo de reconexão | 30 s — maior que os 6 s de detecção |
| Prazo de convite | 30 s |
| Prazo da jogada | 180 s (3 min) — estourou, perde a partida |
| Erros até a forca completa | 6 |
| Retentativa de replicação | 5 s |
