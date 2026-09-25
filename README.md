# Jogo da Forca cliente/servidor com servidor resiliente

Trabalho de Sistemas Distribuídos. Dois jogadores disputam a mesma palavra, e o
serviço continua funcionando quando **o computador** que o atende morre: cada VM
roda num notebook diferente.

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
| Servidor resiliente (se cair, outro assume) — uma VM por computador | `replicacao.py`, `promocao.py`, `infra/agente_fencing.py`, lista de servidores no `cliente_gui.py` |
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
| `promocao.py` | Fencing e promoção, no backup |
| `sonda.py` | Mede indisponibilidade no failover |
| `infra/` | VMs, agente de fencing, injeção de falha |

## Montagem

| Notebook | Roda | Por quê |
|---|---|---|
| **A** (do colega) | VM1 (primário) + agente de fencing | é o que "cai" na demonstração; só ele pode desligar a VM1 |
| **B** (o seu) | VM2 (backup) + os clientes | é onde a partida continua, na frente da banca |

**Rede:** hotspot de um celular, com os dois notebooks nele. Não a Wi-Fi da
faculdade — redes assim costumam isolar os aparelhos uns dos outros. O jogo só
troca pacotes entre os notebooks: não gasta dados móveis.

**As VMs ficam atrás de NAT**, com as portas do notebook redirecionadas para
elas. Cada VM é alcançada pelo **IP do próprio notebook** no hotspot. Nada de
IP virtual: o hotspot sorteia uma faixa nova a cada vez que é ligado, e anunciar
"esse IP agora é meu" por Wi-Fi é justamente o que redes bloqueiam.

## Como rodar

### Local (sem failover)

```powershell
py servidor.py --nome LOCAL
py cliente_gui.py Pedro
py cliente_gui.py Ana
```

### Completo (dois notebooks)

Cada notebook tem VirtualBox e a sua VM (1 núcleo, 1 GB), com a placa 2 em NAT.
Entra-se na VM pela placa 1, host-only, com a chave `~/.ssh/forca_vm`. Depois de
qualquer mudança no código: `infra\provisionar.ps1` com a VM ligada — o
`git pull` atualiza o Windows, não o que está dentro da VM.

**0. Endereços.** Os dois no hotspot; em cada um, `ipconfig`, e anote o IPv4 da
Wi-Fi. Daqui em diante: **`IP_A`** e **`IP_B`**. Escolha também um segredo, o
mesmo nos dois servidores, no lugar de `SEGREDO`.

**1. Portas → VM**, uma vez em cada notebook, com a VM ligada:

```powershell
$vbm = "C:\Program Files\Oracle\VirtualBox\VBoxManage.exe"
& $vbm controlvm VM1 natpf2 "jogo,tcp,,5000,,5000"      # no A
& $vbm controlvm VM2 natpf2 "jogo,tcp,,5000,,5000"      # no B
& $vbm controlvm VM2 natpf2 "repl,tcp,,5001,,5001"      # no B
```

Na primeira vez o Firewall do Windows pergunta: **permita em rede pública** — é
como ele classifica o hotspot. Se bloquear, nada chega à VM.

**2. Agente de fencing, no A** — sem ele a VM2 assume sem confirmar a morte da VM1:

```powershell
py infra\agente_fencing.py --host IP_A --porta 5010 --ip-permitido IP_B
```

**3. Backup, na VM2 (notebook B)**, em uma linha:

```bash
cd ~/JogoDaForca && python3 servidor.py --nome VM2 --porta 5000 --porta-replicacao 5001 --segredo SEGREDO --agente-fencing IP_A:5010 --vm-par VM1 --prazo-jogada 30
```

**4. Primário, na VM1 (notebook A):**

```bash
cd ~/JogoDaForca && python3 servidor.py --nome VM1 --porta 5000 --par IP_B:5001 --segredo SEGREDO --prazo-jogada 30
```

Sinal de que deu certo: na VM1, `backup aceitou o segredo`; na VM2,
`primário conectado ... (identificado pelo segredo)`.

**5. Clientes, no B** — com **os dois** endereços:

```powershell
py cliente_gui.py Pedro --host IP_A,IP_B
py cliente_gui.py Ana   --host IP_A,IP_B
```

`--prazo-jogada 30` precisa ser igual nos dois: depois do failover, quem define o
prazo de cada jogada nova é a flag da VM2. O valor de produção é 180 s.

**Ensaio num computador só:** as duas VMs no mesmo host, com portas do host
diferentes (`7000→5000` na VM1; `7100→5000` e `7101→5001` na VM2), e
`--host 127.0.0.1:7000,127.0.0.1:7100` nos clientes.

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

**4. Replicação e backup em espera.** No terminal da VM2, cada lance aparece como
`cópia atualizada: ... painel B _ N _, erros Ana 0, Bia 1, vez de Ana` — ela tem o
jogo inteiro sem falar com nenhum cliente. Abra um cliente só com o IP do B
(`--host IP_B`): ele é **recusado** (`recusei ...: sou o backup`). Backup em
espera não atende, senão o jogador jogaria numa cópia que o primário apaga.

**5. Failover: a VM1 morre, o notebook A segue vivo.** Com partida em andamento,
no A: `VBoxManage controlvm VM1 poweroff`. O cliente mostra a **faixa laranja**, o
topo troca para `servidor: VM2` **piscando**, e a partida continua. No log da
VM2: `primário mudo há 7s` → `fencing confirmado` → `PROMOÇÃO concluída` →
`assumi com 2 jogador(es) da partida fora: suspensos` → as duas reconexões.

**5c. Failover: o notebook A inteiro morre** — o caso do requisito. Desligue o A
à força (segurar o botão, ou tirar da tomada). **Não desligue a Wi-Fi nem puxe
nada da rede**: isso encena o outro caso, o da rede partida, com os dois vivos.
No log da VM2 sai `fencing NÃO confirmado` — o agente morreu junto com o A — e ela
assume assim mesmo. Aqui isso está **certo**: a VM1 morreu de verdade.
`py sonda.py --host IP_A,IP_B` mede o tempo fora do ar (cerca de 10 s).

**5b. A janela impossível de acertar na mão.** `py infra\armar_falha.py --host
IP_A` faz o servidor morrer **entre** replicar e confirmar — janela de fração de
milissegundo. O cliente não recebe confirmação, reenvia o lance após o failover,
e ele conta **uma única vez**.

## Decisões que sustentam o projeto

- **Replicar antes de confirmar ao cliente.** Replicar depois deixaria o backup
  negando uma letra que os jogadores já viram: divergência permanente. Antes, a
  única dúvida é "meu lance valeu?", que o reenvio com identificador resolve.
- **Timeout de 300 ms na replicação.** Sem ele, um backup que para de responder sem
  fechar a conexão penduraria o primário por ~15 min de retransmissão TCP — e a
  morte do redundante derrubaria o principal.
- **Desconexão suspende, não anula.** Failover desconecta os dois clientes por
  definição; anular na desconexão anularia toda partida que o failover salvasse.
- **Failover pela lista de servidores, e não por IP virtual.** O cliente conhece
  os dois endereços e, quando o atual cai ou responde que é backup, tenta o outro
  — como fazem os clientes de Kafka e MongoDB. O IP virtual dependia de a rede
  aceitar um aparelho anunciando o endereço de outro, que redes que não são
  nossas tratam como ataque (ARP spoofing).
- **Backup em espera não atende.** Com a lista, um cliente pode chegar ao backup
  com o primário vivo; se ele atendesse, o jogador jogaria numa cópia que o
  próximo instantâneo apaga. Ele recusa, e o cliente vai para o outro.
- **Segredo na replicação.** Atrás do NAT, toda conexão chega à VM vinda do
  gateway do VirtualBox, e o IP de origem deixa de identificar o primário. Sem
  o segredo, um impostor conectado na porta de replicação faria o backup concluir
  que o primário caiu e desligá-lo pelo fencing.
- **Fencing pelo hypervisor do outro computador.** Com dois nós não existe quórum.
  Quem desliga a VM1 é o agente no notebook dela, a pedido da VM2. **Limitação
  declarada:** se o notebook A morre, o agente morre junto e a VM2 assume sem
  confirmar — certo, porque a VM1 morreu. Mas se a *rede* entre os dois cai com
  ambos vivos, cada lado acha que o outro morreu: **split-brain**. Com dois
  computadores, "o outro morreu" e "a rede caiu" são indistinguíveis; só um
  terceiro computador como testemunha resolveria.
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
| Prazo do cliente para conectar | 2 s — sem ele, um notebook desligado prende a tentativa |
