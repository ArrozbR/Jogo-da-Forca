import argparse
import os
import random
import selectors
import socket
import sys
import time
from pathlib import Path

from jogo import MAX_ERROS, Partida
from protocolo import BufferExcedido, Enquadrador, empacotar
from promocao import Promotor
from replicacao import Replicador
from sala import PRAZO_RECONEXAO, SUSPENSO, Sala

INTERVALO_HEARTBEAT = 2.0
ESPERA_SELECT = 0.5
JANELA_DETECCAO_PAR = 6.0   # 3 batidas perdidas, igual à do cliente


class Conexao:
    def __init__(self, sock, endereco):
        self.sock = sock
        self.endereco = endereco
        self.enquadrador = Enquadrador()
        self.jogador_id = None


class ConexaoReplica:
    """No backup: a conexão que o primário usa para empurrar estado."""

    def __init__(self, sock, endereco):
        self.sock = sock
        self.endereco = endereco
        self.enquadrador = Enquadrador()


class Servidor:
    def __init__(self, nome: str, host: str, porta: int, palavras: list[str],
                 prazo_reconexao: float = PRAZO_RECONEXAO,
                 par: tuple[str, int] | None = None,
                 porta_replicacao: int | None = None,
                 ip_par: str | None = None):
        self.nome = nome
        self.host = host
        self.porta = porta
        self.palavras = palavras
        self.seletor = selectors.DefaultSelector()
        self.conexoes: dict[socket.socket, Conexao] = {}
        self.por_id: dict[int, Conexao] = {}
        self.sala = Sala(prazo_reconexao=prazo_reconexao)
        self.partida: Partida | None = None
        self.dupla: tuple[int, int] | None = None
        self.proximo_heartbeat = 0.0

        # Último lance aceito de cada jogador, para reconhecer reenvio. Faz parte
        # do estado replicado: é o que impede o backup de contar o mesmo chute
        # duas vezes quando o cliente reenvia por não ter recebido a confirmação.
        self.ultimo_lance: dict[int, int] = {}

        self.porta_replicacao = porta_replicacao
        self.ip_par = ip_par
        self.replicador = (
            Replicador(par[0], par[1], log=lambda m: self._log(f"[repl] {m}"))
            if par else None
        )
        self.replica: ConexaoReplica | None = None
        self.ultimo_contato_par: float | None = None
        self.avisou_par_mudo = False
        self.promotor: Promotor | None = None

        # Gancho de injeção de falha. A janela entre replicar e confirmar ao
        # cliente dura fração de milissegundo: não há como acertá-la puxando o
        # cabo. Comandando a falha, o servidor a cronometra por nós.
        self.morrer_apos_replicar = False

    def _abrir_ouvinte(self, porta: int, marca: str):
        ouvinte = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ouvinte.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        ouvinte.bind((self.host, porta))
        ouvinte.listen()
        ouvinte.setblocking(False)
        self.seletor.register(ouvinte, selectors.EVENT_READ, marca)
        self._log(f"ouvindo {marca} em {self.host}:{porta}")

    def executar(self):
        self._abrir_ouvinte(self.porta, "jogo")
        if self.porta_replicacao:
            self._abrir_ouvinte(self.porta_replicacao, "replicacao")
        if self.replicador:
            self.replicador.conectar()

        while True:
            for chave, _ in self.seletor.select(timeout=ESPERA_SELECT):
                if chave.data == "jogo":
                    self._aceitar(chave.fileobj)
                elif chave.data == "replicacao":
                    self._aceitar_replica(chave.fileobj)
                elif isinstance(chave.data, ConexaoReplica):
                    self._ler_replica(chave.data)
                else:
                    self._ler(chave.data)

            # O select acorda por timeout mesmo sem tráfego: é o que mantém o
            # heartbeat saindo e os prazos de convite sendo cobrados.
            self._cobrar_prazos()
            if time.monotonic() >= self.proximo_heartbeat:
                self._transmitir({"tipo": "heartbeat"})
                # Com a partida suspensa o adversário vê um contador regressivo, e
                # ele só anda se o estado for reenviado periodicamente.
                if self._suspensa():
                    self._transmitir_partida()
                self._cuidar_do_par()
                self.proximo_heartbeat = time.monotonic() + INTERVALO_HEARTBEAT

    def _aceitar(self, ouvinte):
        sock, endereco = ouvinte.accept()
        sock.setblocking(False)
        conexao = Conexao(sock, endereco)
        self.conexoes[sock] = conexao
        self.seletor.register(sock, selectors.EVENT_READ, conexao)

    def _ler(self, conexao: Conexao):
        try:
            dados = conexao.sock.recv(4096)
        except ConnectionError:
            self._encerrar(conexao, "conexão perdida")
            return

        # recv vazio é fechamento limpo pelo par. Sem este teste o selector segue
        # marcando o socket como legível e o laço gira consumindo CPU à toa.
        if not dados:
            self._encerrar(conexao, "desconectou")
            return

        try:
            mensagens = conexao.enquadrador.alimentar(dados)
        except (BufferExcedido, ValueError) as erro:
            self._encerrar(conexao, f"mensagem malformada: {erro}")
            return

        for mensagem in mensagens:
            self._tratar(conexao, mensagem)

    def _tratar(self, conexao: Conexao, mensagem: dict):
        tipo = mensagem.get("tipo")

        if tipo == "ping":
            self._enviar(conexao, {"tipo": "pong"})
        elif tipo == "crash_apos_replicar":
            self.morrer_apos_replicar = True
            self._log("INJEÇÃO DE FALHA armada: morro no próximo lance, "
                      "depois de replicar")
            self._enviar(conexao, {"tipo": "aviso", "msg": "falha armada"})
        elif tipo == "entrar":
            self._entrar(conexao, str(mensagem.get("nome") or "anônimo")[:20])
        elif tipo == "reconectar":
            self._reconectar(conexao, str(mensagem.get("token") or ""))
        elif tipo == "convidar":
            self._convidar(conexao, mensagem.get("alvo"))
        elif tipo in ("aceitar", "recusar"):
            self._responder(conexao, tipo == "aceitar")
        elif tipo == "chute":
            self._chutar(conexao, str(mensagem.get("letra", "")),
                         mensagem.get("lance"))
        else:
            self._enviar(conexao, {"tipo": "erro", "msg": f"tipo desconhecido: {tipo}"})

    def _entrar(self, conexao: Conexao, nome: str):
        if conexao.jogador_id is not None:
            self._enviar(conexao, {"tipo": "erro", "msg": "você já entrou"})
            return
        ident, token = self.sala.entrar(nome)
        # A sala pode ter renomeado para desambiguar, então o nome final vem dela.
        nome_final = self.sala.jogadores[ident].nome
        conexao.jogador_id = ident
        self.por_id[ident] = conexao
        self._log(f"{nome_final} entrou como jogador {ident}")
        self._enviar(conexao, {"tipo": "sessao", "id": ident, "token": token,
                               "nome": nome_final})
        self._tentar_iniciar()
        self._publicar()

    def _reconectar(self, conexao: Conexao, token: str):
        if conexao.jogador_id is not None:
            self._enviar(conexao, {"tipo": "erro", "msg": "você já está nesta sessão"})
            return

        ident, motivo = self.sala.reconectar(token)
        if ident is None:
            # O cliente cai de volta para 'entrar' ao receber isto.
            self._enviar(conexao, {"tipo": "recusado", "msg": motivo})
            return

        # A conexão nova substitui a antiga: a velha pode ser um socket zumbi que
        # ainda parece aberto para o sistema operacional.
        antiga = self.por_id.get(ident)
        if antiga is not None and antiga is not conexao:
            # Avisar antes de cortar importa: uma conexão viva que for cortada sem
            # explicação tenta reconectar com o mesmo token, e as duas passam a se
            # expulsar em laço. Sabendo o motivo, ela entra como jogador novo.
            self._log(f"jogador {ident} reconectou, descartando conexão antiga")
            self._enviar(antiga, {"tipo": "substituido",
                                  "msg": "outra conexão assumiu esta sessão"})
            self._descartar(antiga)

        conexao.jogador_id = ident
        self.por_id[ident] = conexao
        nome = self.sala.jogadores[ident].nome
        self._log(f"{nome} (jogador {ident}) reconectou: {motivo}")
        self._enviar(conexao, {"tipo": "sessao", "id": ident,
                               "token": self.sala.jogadores[ident].token,
                               "nome": nome})
        self._enviar(conexao, {"tipo": "aviso", "msg": motivo})

        if self._jogando(ident):
            self._avisar_adversario(ident, f"{nome} voltou")
        self._publicar()

    def _convidar(self, conexao: Conexao, alvo):
        if conexao.jogador_id is None:
            self._enviar(conexao, {"tipo": "erro", "msg": "entre antes de convidar"})
            return
        try:
            alvo = int(alvo)
        except (TypeError, ValueError):
            self._enviar(conexao, {"tipo": "erro", "msg": "id de alvo inválido"})
            return

        ok, motivo = self.sala.convidar(conexao.jogador_id, alvo)
        self._enviar(conexao, {"tipo": "erro" if not ok else "aviso", "msg": motivo})

        # Só transmite quando algo mudou. Reenviar a sala depois de uma recusa
        # reimprimia o painel inteiro e enterrava a linha de erro na tela.
        if not ok:
            return
        self._avisar(alvo, f"{self.sala.jogadores[conexao.jogador_id].nome} te convidou")
        self._publicar()

    def _responder(self, conexao: Conexao, aceita: bool):
        if conexao.jogador_id is None:
            self._enviar(conexao, {"tipo": "erro", "msg": "entre antes de responder"})
            return

        # Quem convidou tem de ser descoberto antes: responder() apaga o convite.
        convidante = self.sala.quem_convidou(conexao.jogador_id)
        eu = self.sala.jogadores[conexao.jogador_id].nome

        ok, motivo, dupla = self.sala.responder(conexao.jogador_id, aceita)
        self._enviar(conexao, {"tipo": "erro" if not ok else "aviso", "msg": motivo})

        # Sem convite pendente nada mudou, então não há painel para reenviar.
        if convidante is None:
            return

        if ok:
            self._avisar(convidante, f"{eu} {'aceitou' if aceita else 'recusou'} seu convite")
        if dupla:
            self._log(f"dupla formada: {dupla}")
            self._tentar_iniciar()
        self._publicar()

    def _chutar(self, conexao: Conexao, letra: str, lance=None):
        ident = conexao.jogador_id
        if ident is None:
            self._enviar(conexao, {"tipo": "erro", "msg": "entre antes de chutar"})
            return

        # Reenvio do mesmo lance. Acontece quando o primário replicou e morreu
        # antes de confirmar: o cliente não soube se o chute valeu e manda de
        # novo. Sem este teste, o backup contaria o chute duas vezes.
        if lance is not None and self.ultimo_lance.get(ident) == lance:
            self._log(f"jogador {ident} reenviou o lance {lance} — já aplicado")
            if self._jogando(ident):
                self._enviar(conexao, self._estado_partida(ident))
            return
        if self.partida is None or not self.dupla or ident not in self.dupla:
            self._enviar(conexao, {"tipo": "erro", "msg": "você não está na partida"})
            return

        # Chutar com o adversário ausente seria jogar contra uma tela que ninguém
        # está vendo, então a partida fica congelada até ele voltar ou o prazo vencer.
        if self._suspensa():
            self._enviar(conexao, {"tipo": "erro",
                                   "msg": "partida suspensa, aguardando o adversário"})
            return

        aceito, motivo = self.partida.chutar(ident, letra)
        if not aceito:
            self._enviar(conexao, {"tipo": "erro", "msg": motivo})
            return

        if lance is not None:
            self.ultimo_lance[ident] = lance
        self._log(f"jogador {ident} chutou {letra.upper()}: {motivo}")
        self._publicar_partida()

        if self.partida.encerrada:
            self._log(f"partida encerrada, vencedor {self.partida.vencedor}")
            self.sala.encerrar_partida(self.dupla)
            self.partida = None
            self.dupla = None
            self._tentar_iniciar()
            self._publicar()

    def _tentar_iniciar(self):
        if self.partida is not None:
            return
        dupla = self.sala.proxima_dupla()
        if dupla is None:
            return
        palavra = random.choice(self.palavras)
        self.partida = Partida(palavra, list(dupla))
        self.dupla = dupla
        nomes = " x ".join(self.sala.jogadores[i].nome for i in dupla)
        self._log(f"partida iniciada: {nomes}, palavra {palavra}")

    def _suspensa(self) -> bool:
        if not self.dupla:
            return False
        return any(
            self.sala.jogadores[i].estado == SUSPENSO
            for i in self.dupla if i in self.sala.jogadores
        )

    def _ausente(self) -> int | None:
        if not self.dupla:
            return None
        return next(
            (i for i in self.dupla
             if i in self.sala.jogadores and self.sala.jogadores[i].estado == SUSPENSO),
            None,
        )

    def _cobrar_prazos(self):
        mudou = False

        for de, para in self.sala.expirar():
            self._log(f"convite {de} -> {para} expirou")
            for ident in (de, para):
                self._avisar(ident, "o convite expirou")
            mudou = True

        for ident in self.sala.suspensos_vencidos():
            nome = self.sala.jogadores[ident].nome
            self._log(f"prazo de reconexão de {nome} venceu")
            if self._jogando(ident):
                self._avisar_adversario(ident, f"{nome} não voltou — partida anulada")
                dupla = self.dupla
                self.sala.encerrar_partida(dupla)
                self.partida = None
                self.dupla = None
            self.sala.sair(ident)
            self._tentar_iniciar()
            mudou = True

        if mudou:
            self._publicar()

    def _avisar(self, ident: int, msg: str):
        conexao = self.por_id.get(ident)
        if conexao:
            self._enviar(conexao, {"tipo": "aviso", "msg": msg})

    def _estado_partida(self, ident: int) -> dict:
        ausente = self._ausente()
        return {
            "tipo": "estado",
            "voce": ident,
            "suspensa": ausente is not None,
            "ausente": ausente,
            "nome_ausente": self.sala.jogadores[ausente].nome if ausente else None,
            "falta_voltar": self.sala.falta_para_voltar(ausente) if ausente else None,
            "jogadores": [
                {
                    "id": i,
                    "nome": self.sala.jogadores[i].nome if i in self.sala.jogadores else "?",
                    "erros": self.partida.erros[i],
                }
                for i in self.dupla
            ],
            "max_erros": MAX_ERROS,
            # Diz ao cliente qual foi o último lance dele que o servidor aceitou,
            # para ele saber quando parar de reenviar.
            "meu_lance": self.ultimo_lance.get(ident),
            "painel": self.partida.painel(),
            "turno": self.partida.turno,
            "chutadas": sorted(self.partida.chutadas),
            "encerrada": self.partida.encerrada,
            "vencedor": self.partida.vencedor,
            "palavra": self.partida.palavra if self.partida.encerrada else None,
        }

    def _jogando(self, ident: int) -> bool:
        return bool(self.partida and self.dupla and ident in self.dupla)

    def _transmitir_partida(self):
        """Só os dois da partida. Um chute não muda nada para quem espera."""
        if not self.dupla:
            return
        for ident in self.dupla:
            conexao = self.por_id.get(ident)
            if conexao:
                self._enviar(conexao, self._estado_partida(ident))

    def _transmitir_sala(self):
        for ident, conexao in list(self.por_id.items()):
            if ident in self.sala.jogadores and not self._jogando(ident):
                self._enviar(conexao, self.sala.estado_para(ident, self.partida is not None))

    def _transmitir_tudo(self):
        self._transmitir_partida()
        self._transmitir_sala()

    # ------------------------- replicação -------------------------

    def _instantaneo(self) -> dict:
        return {
            "sala": self.sala.para_dict(),
            "partida": self.partida.para_dict() if self.partida else None,
            "dupla": list(self.dupla) if self.dupla else None,
            "ultimo_lance": {str(k): v for k, v in self.ultimo_lance.items()},
        }

    def _aplicar_instantaneo(self, d: dict):
        """Aplica o estado recebido do primário — ou nada.

        Tudo é construído em variáveis locais antes de qualquer atribuição: um
        instantâneo truncado ou corrompido levanta a exceção *antes* de tocar o
        estado real, e o backup continua com o último estado válido em vez de
        ficar meio atualizado.
        """
        nova_sala = Sala(prazo_reconexao=self.sala.prazo_reconexao)
        nova_sala.aplicar_dict(d["sala"])
        nova_partida = Partida.de_dict(d["partida"]) if d.get("partida") else None
        nova_dupla = tuple(d["dupla"]) if d.get("dupla") else None
        novo_lance = {int(k): v for k, v in (d.get("ultimo_lance") or {}).items()}

        self.sala = nova_sala
        self.partida = nova_partida
        self.dupla = nova_dupla
        self.ultimo_lance = novo_lance

    def _replicar(self):
        """Empurra o estado para o backup ANTES de confirmar ao cliente.

        Se falhar, o lance vale de todo jeito e seguimos sem proteção — decisão
        consciente: recusar o lance deixaria o jogo injogável sempre que a
        máquina redundante estivesse fora, o que inverteria o propósito dela.
        """
        if self.replicador and self.replicador.ativo:
            self.replicador.replicar(self._instantaneo())

    def _publicar(self):
        self._replicar()
        self._transmitir_tudo()

    def _publicar_partida(self):
        self._replicar()
        self._talvez_morrer()
        self._transmitir_partida()

    def _talvez_morrer(self):
        """Morre entre replicar e confirmar, se pedido.

        os._exit e não sys.exit: sys.exit levanta SystemExit, roda a limpeza e
        FECHA os sockets, o que manda FIN e o cliente detecta a queda na hora.
        Isso é o oposto da falha que queremos reproduzir — uma máquina que
        evapora não avisa ninguém.
        """
        if not self.morrer_apos_replicar:
            return
        self._log("INJEÇÃO DE FALHA: morrendo agora, depois de replicar e "
                  "antes de confirmar ao cliente")
        sys.stdout.flush()
        os._exit(9)

    def _cuidar_do_par(self):
        if self.replicador:
            if self.replicador.manutencao():
                # Voltou depois de um período fora: perdeu lances, então recebe o
                # estado completo, e não um incremento.
                self._log("[repl] backup voltou — enviando estado completo")
                self._replicar()
            elif self.replicador.ativo:
                self.replicador.bater()

        if self.porta_replicacao and self.ultimo_contato_par is not None:
            silencio = time.monotonic() - self.ultimo_contato_par
            if silencio > JANELA_DETECCAO_PAR and not self.avisou_par_mudo:
                self._log(f"[repl] primário mudo há {silencio:.0f}s "
                          f"({int(silencio // INTERVALO_HEARTBEAT)} batidas perdidas)")
                self.avisou_par_mudo = True

                # O silêncio é indistinguível entre 'primário morreu' e 'rede
                # partida' — a ausência de mensagem não carrega a causa dela. É
                # por isso que existe o fencing: ele decide em vez de adivinhar.
                if self.promotor and not self.promotor.promovido:
                    if self.promotor.promover():
                        self._fechar_replica()
                        self._transmitir_tudo()

    def _aceitar_replica(self, ouvinte):
        sock, endereco = ouvinte.accept()

        # Só o primário configurado entra. Sem este teste, qualquer um na rede
        # conectava aqui, era aceito como primário, o canal verdadeiro era
        # descartado, e ao morrer essa conexão o backup concluía que o primário
        # havia caído — e DESLIGAVA a máquina saudável pelo fencing. Lixo numa
        # linha derrubava fisicamente o servidor bom.
        if self.ip_par and endereco[0] != self.ip_par:
            self._log(f"[repl] conexão de {endereco[0]} recusada "
                      f"(só {self.ip_par} pode replicar)")
            sock.close()
            return

        # Defesa em profundidade: um canal que deu sinal de vida agora há pouco não
        # é substituído. Mesmo vindo do endereço certo, ninguém desloca o canal ativo.
        if self.replica is not None and self.ultimo_contato_par is not None:
            silencio = time.monotonic() - self.ultimo_contato_par
            if silencio < JANELA_DETECCAO_PAR:
                self._log(f"[repl] conexão de {endereco[0]} recusada: canal atual "
                          f"deu sinal há {silencio:.1f}s")
                sock.close()
                return

        sock.setblocking(False)
        if self.replica is not None:
            self._log("[repl] primário reconectou, descartando canal antigo")
            self._fechar_replica()
        self.replica = ConexaoReplica(sock, endereco)
        self.seletor.register(sock, selectors.EVENT_READ, self.replica)
        self.ultimo_contato_par = time.monotonic()
        self.avisou_par_mudo = False
        self._log(f"[repl] primário conectado de {endereco[0]}")

    def _fechar_replica(self):
        if self.replica is None:
            return
        try:
            self.seletor.unregister(self.replica.sock)
        except (KeyError, ValueError):
            pass
        try:
            self.replica.sock.close()
        except OSError:
            pass
        self.replica = None

    def _ler_replica(self, conexao: ConexaoReplica):
        try:
            dados = conexao.sock.recv(65536)
        except ConnectionError:
            dados = b""

        if not dados:
            self._log("[repl] canal com o primário caiu")
            self._fechar_replica()
            return

        self.ultimo_contato_par = time.monotonic()
        self.avisou_par_mudo = False
        try:
            mensagens = conexao.enquadrador.alimentar(dados)
        except (BufferExcedido, ValueError) as erro:
            self._log(f"[repl] mensagem malformada: {erro}")
            self._fechar_replica()
            return

        for m in mensagens:
            if m.get("tipo") == "estado":
                # A porta de replicação não tem autenticação, então qualquer um na
                # rede alcança este caminho. Um `{"tipo":"estado"}` sem a chave
                # `dados` derrubava o servidor inteiro com uma linha.
                try:
                    dados = m["dados"]
                    if not isinstance(dados, dict):
                        raise ValueError(f"'dados' não é objeto: {type(dados).__name__}")
                    self._aplicar_instantaneo(dados)
                except (KeyError, TypeError, ValueError, AttributeError) as erro:
                    self._log(f"[repl] estado inválido descartado "
                              f"({type(erro).__name__}: {erro})")
                    self._fechar_replica()
                    return

            try:
                conexao.sock.sendall(empacotar({"tipo": "ok", "seq": m.get("seq")}))
            except OSError:
                self._fechar_replica()
                return

    def _transmitir(self, mensagem: dict):
        for conexao in list(self.conexoes.values()):
            self._enviar(conexao, mensagem)

    def _enviar(self, conexao: Conexao, mensagem: dict):
        # O nome do servidor viaja em toda mensagem: com IP virtual o cliente não
        # tem outro jeito de perceber que trocou de máquina no failover.
        try:
            conexao.sock.sendall(empacotar({**mensagem, "servidor": self.nome}))
        except OSError:
            self._encerrar(conexao, "falha ao enviar")

    def _descartar(self, conexao: Conexao):
        """Fecha o socket sem tocar no estado do jogador."""
        if conexao.sock not in self.conexoes:
            return
        try:
            self.seletor.unregister(conexao.sock)
        except (KeyError, ValueError):
            pass
        conexao.sock.close()
        del self.conexoes[conexao.sock]

    def _encerrar(self, conexao: Conexao, motivo: str):
        if conexao.sock not in self.conexoes:
            return
        ident = conexao.jogador_id
        self._log(f"jogador {ident or conexao.endereco[0]}: {motivo}")
        self._descartar(conexao)

        if ident is None:
            return

        # Se este socket já não era o atual do jogador, ele é a conexão velha que
        # uma reconexão substituiu. Descartar e sair: o jogador está vivo.
        if self.por_id.get(ident) is not conexao:
            return
        del self.por_id[ident]

        if self._jogando(ident):
            nome = self.sala.jogadores[ident].nome
            self.sala.suspender(ident)
            falta = self.sala.falta_para_voltar(ident)
            self._log(f"partida SUSPENSA: {nome} tem {falta}s para voltar")
            self._avisar_adversario(ident, f"{nome} caiu — aguardando reconexão")
            self._publicar()
            return

        for outro in self.sala.sair(ident):
            self._avisar(outro, "seu par saiu da sala")
        self._publicar()

    def _avisar_adversario(self, ident: int, msg: str):
        if not self.dupla:
            return
        for outro in self.dupla:
            if outro != ident:
                self._avisar(outro, msg)

    def _log(self, texto: str):
        print(f"[{time.strftime('%H:%M:%S')}] [{self.nome}] {texto}", flush=True)


def carregar_palavras(caminho: Path) -> list[str]:
    linhas = caminho.read_text(encoding="utf-8").splitlines()
    return [linha.strip() for linha in linhas if linha.strip()]


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Servidor do jogo da forca")
    parser.add_argument("--nome", default="VM1")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--porta", type=int, default=5000)
    parser.add_argument("--palavras", type=Path, default=Path(__file__).parent / "palavras.txt")
    parser.add_argument("--prazo-reconexao", type=float, default=PRAZO_RECONEXAO,
                        help="segundos para um jogador caído voltar antes de anular")
    parser.add_argument("--par", metavar="HOST:PORTA",
                        help="endereço de replicação do backup (quem passa isto é o primário)")
    parser.add_argument("--porta-replicacao", type=int,
                        help="porta onde receber estado do primário (quem passa isto é o backup)")
    parser.add_argument("--ip-par", metavar="IP",
                        help="único endereço aceito na porta de replicação; sem isto, "
                             "qualquer um na rede pode se passar pelo primário")
    parser.add_argument("--agente-fencing", metavar="HOST:PORTA",
                        help="agente de fencing no host; habilita promoção automática")
    parser.add_argument("--vm-par", default="VM1",
                        help="nome da VM do primário, para o fencing desligar")
    parser.add_argument("--ip-virtual", default="192.168.56.10/24")
    parser.add_argument("--interface", default="proj0")
    parser.add_argument("--script-assumir",
                        default=str(Path(__file__).parent / "infra" / "assumir_ip.sh"))
    args = parser.parse_args()

    par = None
    if args.par:
        host, _, porta = args.par.rpartition(":")
        par = (host, int(porta))

    servidor = Servidor(args.nome, args.host, args.porta,
                        carregar_palavras(args.palavras), args.prazo_reconexao,
                        par, args.porta_replicacao, args.ip_par)

    if args.agente_fencing:
        host, _, porta = args.agente_fencing.rpartition(":")
        servidor.promotor = Promotor(
            (host, int(porta)), args.vm_par, args.ip_virtual, args.interface,
            args.script_assumir, log=lambda m: servidor._log(f"[promo] {m}"),
        )
    try:
        servidor.executar()
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
