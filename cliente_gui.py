"""Cliente gráfico do Jogo da Forca, em PyQt6.

=============================================================================
 ESTE É O CLIENTE DA ENTREGA. É ele que deve ser usado na apresentação.
 O `cliente.py`, de terminal, permanece apenas como ferramenta de teste
 automatizado, por ser roteirizável — não é entregável.
=============================================================================

Fala o mesmo protocolo do cliente de terminal e mantém toda a resiliência:
token de sessão em disco, reconexão automática, detecção de servidor mudo e
reenvio de lance com identificador. Sem isso a interface ficaria bonita e o
failover deixaria de funcionar.

A rede usa socket comum lido por um QTimer, e não QTcpSocket: a lógica de
reconexão e enquadramento já estava testada, e reescrevê-la só para ficar
idiomática em Qt seria trocar algo que funciona por algo por testar.

    py cliente_gui.py Pedro --host 192.168.56.10
"""
import argparse
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QKeySequence, QPainter, QPen, QShortcut
from PyQt6.QtWidgets import (QApplication, QFrame, QGridLayout, QHBoxLayout,
                             QHeaderView, QLabel, QMainWindow, QPushButton,
                             QStackedWidget, QTableWidget, QTableWidgetItem,
                             QVBoxLayout, QWidget)

from jogo import normalizar
from protocolo import BufferExcedido, Enquadrador, empacotar

INTERVALO_TICK = 50
JANELA_DETECCAO = 6.0
ESPERA_RECONEXAO = 1.0
MAX_ERROS = 6

FUNDO = "#15161c"
PAINEL = "#1e2029"
BORDA = "#2c2f3a"
TEXTO = "#e9eaee"
APAGADO = "#7f8496"
DESTAQUE = "#57a6ff"
BOM = "#46d38a"
RUIM = "#ff6b6b"
ALERTA = "#ffb457"

ESTILO = f"""
QMainWindow, QWidget {{ background: {FUNDO}; color: {TEXTO};
    font-family: 'Segoe UI'; font-size: 11pt; }}
/* Sem isto os rótulos herdam o fundo escuro da janela e pintam retângulos por
   cima da barra e do rodapé, que têm cor própria. */
QLabel {{ background: transparent; }}
#barra {{ background: {PAINEL}; border-bottom: 1px solid {BORDA}; }}
#rodape {{ background: {PAINEL}; border-top: 1px solid {BORDA}; }}
#servidor {{ font-size: 15pt; font-weight: 700; color: {DESTAQUE}; }}
#titulo {{ font-size: 15pt; font-weight: 700; letter-spacing: 2px; }}
#palavra {{ font-family: 'Consolas'; font-size: 34pt; font-weight: 700;
    letter-spacing: 10px; }}
#turno {{ font-size: 14pt; font-weight: 600; }}
QPushButton {{ background: {PAINEL}; color: {TEXTO}; border: 1px solid {BORDA};
    border-radius: 7px; padding: 9px 16px; }}
QPushButton:hover:enabled {{ border-color: {DESTAQUE}; color: {DESTAQUE}; }}
QPushButton:disabled {{ color: #4a4e5c; border-color: #23252e; }}
QPushButton#letra {{ font-family: 'Consolas'; font-size: 13pt; font-weight: 700;
    min-width: 40px; max-width: 40px; min-height: 40px; max-height: 40px;
    padding: 0px; }}
QTableWidget {{ background: {PAINEL}; border: 1px solid {BORDA};
    border-radius: 8px; gridline-color: {BORDA}; }}
QTableWidget::item {{ padding: 8px; border: none; }}
QTableWidget::item:selected {{ background: {DESTAQUE}; color: {FUNDO}; }}
QHeaderView::section {{ background: {FUNDO}; color: {APAGADO};
    border: none; border-bottom: 1px solid {BORDA}; padding: 8px; }}
"""


class Forca(QWidget):
    """Desenha a forca com QPainter — linhas suavizadas, não caracteres."""

    def __init__(self):
        super().__init__()
        self.erros = 0
        self.setFixedSize(215, 240)

    def definir(self, erros: int):
        if erros != self.erros:
            self.erros = erros
            self.update()

    def paintEvent(self, evento):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(PAINEL))

        def caneta(cor, largura):
            c = QPen(QColor(cor), largura)
            c.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(c)

        caneta("#5d6478", 7)
        p.drawLine(28, 218, 172, 218)     # base
        p.drawLine(60, 218, 60, 22)       # poste
        p.drawLine(60, 22, 140, 22)       # viga
        caneta("#5d6478", 3)
        p.drawLine(140, 22, 140, 48)      # corda

        cor = RUIM if self.erros >= MAX_ERROS else TEXTO
        caneta(cor, 5)
        if self.erros >= 1:
            p.drawEllipse(122, 48, 36, 36)
        if self.erros >= 2:
            p.drawLine(140, 84, 140, 146)
        if self.erros >= 3:
            p.drawLine(140, 98, 113, 126)
        if self.erros >= 4:
            p.drawLine(140, 98, 167, 126)
        if self.erros >= 5:
            p.drawLine(140, 146, 115, 186)
        if self.erros >= 6:
            p.drawLine(140, 146, 165, 186)
        p.end()


class JanelaForca(QMainWindow):
    def __init__(self, nome: str, host: str, porta: int, sessao_nova: bool = False):
        super().__init__()
        self.nome, self.host, self.porta = nome, host, porta

        self.sock: socket.socket | None = None
        self.enquadrador = Enquadrador()
        self.ultimo_contato = 0.0
        self.proxima_tentativa = 0.0
        self.conectado = False

        self.meu_id = None
        self.servidor_atual = None
        self.estado_sala = None
        self.proximo_lance = 1
        self.lance_pendente: tuple[int, str] | None = None
        self.mostrando_resultado = False
        self.segundos_resultado = 0

        # O servidor só manda 'estado' quando algo muda, e numa vez parada nada
        # muda por até 3 min. Então o cliente guarda quanto faltava e a partir de
        # quando, e desenha a contagem sozinho. É só enfeite: quem decide o
        # estouro é o servidor, que é quem tem o relógio que vale.
        self.relogio_falta: float | None = None
        self.relogio_desde = 0.0
        self.relogio_prazo = 0.0

        pasta = Path(tempfile.gettempdir())
        # O nome entra em nome de arquivo. Com `\`, `:` ou `..` ele escreveria
        # fora da pasta temporária, ou o open falharia calado e a sessão nunca
        # seria salva.
        seguro = "".join(c for c in nome if c.isalnum() or c in "-_") or "jogador"
        base = f"forca-{seguro}-{host}-{porta}"
        self.arquivo_token = pasta / f"{base}.token"
        self.arquivo_trava = pasta / f"{base}.lock"
        self._trava = None
        self.exclusivo = self._travar_sessao()
        self.token = None if (sessao_nova or not self.exclusivo) else self._ler_token()
        if sessao_nova and self.exclusivo:
            self._esquecer_token()

        self._montar()
        self._travar_texto_plano()
        self._ligar_teclado()
        if not self.exclusivo:
            self._avisar(f"já existe um cliente '{nome}' nesta máquina — "
                         f"entrando como sessão nova", ALERTA)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(INTERVALO_TICK)

        # Relógio da jogada em timer próprio: o _tick roda a cada 50 ms para a
        # rede responder rápido, e redesenhar o texto 20 vezes por segundo só
        # para mudar de segundo em segundo seria desperdício.
        self.timer_relogio = QTimer(self)
        self.timer_relogio.timeout.connect(self._pintar_relogio)
        self.timer_relogio.start(500)

    # ------------------------------------------------------------ sessão
    def _travar_sessao(self) -> bool:
        try:
            self._trava = open(self.arquivo_trava, "a+b")
        except OSError:
            return True
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._trava.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._trava.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._trava.close()
            self._trava = None
            return False
        return True

    def _ler_token(self):
        try:
            return self.arquivo_token.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def _salvar_token(self, token):
        self.token = token
        if self.exclusivo:
            try:
                self.arquivo_token.write_text(token, encoding="utf-8")
            except OSError:
                pass

    def _esquecer_token(self):
        self.token = None
        if self.exclusivo:
            try:
                self.arquivo_token.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------ layout
    def _montar(self):
        self.setWindowTitle(f"Jogo da Forca — {self.nome}")
        self.setMinimumSize(960, 700)
        self.setStyleSheet(ESTILO)

        central = QWidget()
        self.setCentralWidget(central)
        raiz = QVBoxLayout(central)
        raiz.setContentsMargins(0, 0, 0, 0)
        raiz.setSpacing(0)

        # ---- barra: o nome do servidor mora aqui, grande, porque é o que
        # torna a troca de máquina perceptível para quem assiste.
        barra = QFrame(objectName="barra")
        barra.setFixedHeight(62)
        hb = QHBoxLayout(barra)
        hb.setContentsMargins(22, 0, 22, 0)
        self.lbl_servidor = QLabel("servidor: —", objectName="servidor")
        self.lbl_conexao = QLabel("conectando...")
        self.lbl_conexao.setStyleSheet(f"color: {ALERTA};")
        self.lbl_eu = QLabel("")
        self.lbl_eu.setStyleSheet(f"color: {APAGADO};")
        hb.addWidget(self.lbl_servidor)
        hb.addSpacing(14)
        hb.addWidget(self.lbl_conexao)
        hb.addStretch()
        hb.addWidget(self.lbl_eu)
        raiz.addWidget(barra)

        # Faixa de queda: durante o failover é o que o professor precisa ver, e
        # uma linha discreta no rodapé passava despercebida.
        self.faixa = QLabel("", objectName="faixa")
        self.faixa.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.faixa.setFixedHeight(40)
        self.faixa.hide()
        raiz.addWidget(self.faixa)

        self.pilha = QStackedWidget()
        raiz.addWidget(self.pilha, 1)
        self.pilha.addWidget(self._tela_sala())
        self.pilha.addWidget(self._tela_partida())
        self.pilha.addWidget(self._tela_resultado())

        rodape = QFrame(objectName="rodape")
        rodape.setFixedHeight(56)
        hr = QHBoxLayout(rodape)
        hr.setContentsMargins(22, 0, 22, 0)
        self.lbl_msg = QLabel("")
        hr.addWidget(self.lbl_msg)
        raiz.addWidget(rodape)

    def _tela_sala(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(46, 22, 46, 18)
        v.setSpacing(14)

        v.addWidget(QLabel("SALA DE ESPERA", objectName="titulo"),
                    alignment=Qt.AlignmentFlag.AlignHCenter)

        self.tabela = QTableWidget(0, 3)
        self.tabela.setHorizontalHeaderLabels(["#", "Jogador", "Situação"])
        self.tabela.verticalHeader().setVisible(False)
        self.tabela.setShowGrid(False)
        self.tabela.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self.tabela.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        # Duplo clique convida direto: numa demonstração, um passo a menos.
        self.tabela.doubleClicked.connect(lambda _: self._convidar())
        cab = self.tabela.horizontalHeader()
        cab.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.tabela.setColumnWidth(0, 60)
        cab.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        cab.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        v.addWidget(self.tabela, 1)

        linha = QHBoxLayout()
        linha.addStretch()
        self.bt_convidar = QPushButton("Convidar selecionado")
        self.bt_aceitar = QPushButton("Aceitar  (Enter)")
        self.bt_recusar = QPushButton("Recusar  (Esc)")
        self.bt_convidar.clicked.connect(self._convidar)
        self.bt_aceitar.clicked.connect(self._aceitar)
        self.bt_recusar.clicked.connect(self._recusar)
        for b in (self.bt_convidar, self.bt_aceitar, self.bt_recusar):
            linha.addWidget(b)
        linha.addStretch()
        v.addLayout(linha)

        self.lbl_fila = QLabel("")
        self.lbl_fila.setStyleSheet(f"color: {APAGADO};")
        v.addWidget(self.lbl_fila, alignment=Qt.AlignmentFlag.AlignHCenter)
        return w

    def _tela_partida(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(30, 18, 30, 16)
        v.setSpacing(10)

        bonecos = QHBoxLayout()
        bonecos.addStretch()
        self.forcas, self.lbl_jogador = [], []
        for _ in range(2):
            col = QVBoxLayout()
            col.setSpacing(8)
            lbl = QLabel("")
            lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            lbl.setStyleSheet("font-size: 13pt; font-weight: 600;")
            f = Forca()
            col.addWidget(lbl)
            col.addWidget(f, alignment=Qt.AlignmentFlag.AlignHCenter)
            bonecos.addLayout(col)
            bonecos.addSpacing(40)
            self.forcas.append(f)
            self.lbl_jogador.append(lbl)
        bonecos.addStretch()
        v.addLayout(bonecos)

        self.lbl_palavra = QLabel("", objectName="palavra")
        self.lbl_palavra.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        v.addWidget(self.lbl_palavra)

        self.lbl_turno = QLabel("", objectName="turno")
        self.lbl_turno.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        v.addWidget(self.lbl_turno)

        self.lbl_relogio = QLabel("")
        self.lbl_relogio.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        v.addWidget(self.lbl_relogio)

        grade = QGridLayout()
        grade.setSpacing(6)
        self.botoes_letra = {}
        letras = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        # 9 por linha, e não 13: com 13 a última coluna encostava na borda da
        # janela e as letras M e Z ficavam cortadas.
        for i, letra in enumerate(letras):
            b = QPushButton(letra, objectName="letra")
            b.clicked.connect(lambda _, l=letra: self._chutar(l))
            grade.addWidget(b, i // 9, i % 9)
            self.botoes_letra[letra] = b
        env = QHBoxLayout()
        env.addStretch()
        env.addLayout(grade)
        env.addStretch()
        v.addLayout(env)
        # Sem este esticador no fim, o espaço sobrando é repartido entre os
        # rótulos e empurra o teclado para fora da janela.
        v.addStretch(1)
        return w

    def _tela_resultado(self) -> QWidget:
        """Tela de fim de partida.

        Existe porque o servidor libera o slot no mesmo instante em que anuncia
        o fim — e faz certo, senão a próxima dupla esperaria à toa. Quem precisa
        dar tempo de ler o resultado é o cliente.
        """
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(40, 30, 40, 24)
        v.setSpacing(16)
        v.addStretch(1)

        self.lbl_res_titulo = QLabel("")
        self.lbl_res_titulo.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        v.addWidget(self.lbl_res_titulo)

        legenda = QLabel("a palavra era")
        legenda.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        legenda.setStyleSheet(f"font-size: 11pt; color: {APAGADO};")
        v.addWidget(legenda)

        # Sem letter-spacing na legenda: só a palavra ganha o espaçamento, senão
        # o texto corrido fica arrastado e ilegível.
        self.lbl_res_palavra = QLabel("")
        self.lbl_res_palavra.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.lbl_res_palavra.setStyleSheet(
            f"font-family: 'Consolas'; font-size: 28pt; font-weight: 700;"
            f" letter-spacing: 10px; color: {TEXTO};")
        v.addWidget(self.lbl_res_palavra)

        self.lbl_res_placar = QLabel("")
        self.lbl_res_placar.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.lbl_res_placar.setStyleSheet(f"font-size: 13pt; color: {APAGADO};")
        v.addWidget(self.lbl_res_placar)

        v.addSpacing(10)
        linha = QHBoxLayout()
        linha.addStretch()
        self.bt_voltar = QPushButton("Voltar à sala   (Enter)")
        self.bt_voltar.clicked.connect(self._voltar_para_sala)
        linha.addWidget(self.bt_voltar)
        linha.addStretch()
        v.addLayout(linha)

        self.lbl_res_contagem = QLabel("")
        self.lbl_res_contagem.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.lbl_res_contagem.setStyleSheet(f"font-size: 10pt; color: {APAGADO};")
        v.addWidget(self.lbl_res_contagem)
        v.addStretch(2)
        return w

    def _pintar_relogio(self):
        if self.pilha.currentIndex() != 1 or self.relogio_falta is None:
            self.lbl_relogio.setText("")
            return
        falta = max(0.0, self.relogio_falta - (time.monotonic() - self.relogio_desde))
        # Vermelho no último sexto do prazo: serve igual com 3 min de jogo e com
        # os poucos segundos que a demonstração usa.
        apertado = falta <= max(5.0, self.relogio_prazo / 6)
        self.lbl_relogio.setText(f"{int(falta) // 60}:{int(falta) % 60:02d} para jogar")
        self.lbl_relogio.setStyleSheet(
            f"font-size: 11pt; font-weight: {'700' if apertado else '400'};"
            f" color: {RUIM if apertado else APAGADO};")

    def _mostrar_resultado(self, e: dict):
        venceu = e.get("vencedor") == e.get("voce")
        self.lbl_res_titulo.setText("VOCÊ VENCEU!" if venceu else "VOCÊ PERDEU")
        self.lbl_res_titulo.setStyleSheet(
            f"font-size: 30pt; font-weight: 700; letter-spacing: 3px;"
            f" color: {BOM if venceu else RUIM};")
        self.lbl_res_palavra.setText(e.get("palavra") or "?")
        self.lbl_res_placar.setText("     ".join(
            f"{j.get('nome', '?')}  {j.get('erros', 0)}/{e.get('max_erros', MAX_ERROS)} erros"
            for j in (e.get("jogadores") or [])))

        self.mostrando_resultado = True
        self.segundos_resultado = 12
        self.pilha.setCurrentIndex(2)
        self._tique_resultado()

    def _tique_resultado(self):
        if not self.mostrando_resultado:
            return
        if self.segundos_resultado <= 0:
            self._voltar_para_sala()
            return
        self.lbl_res_contagem.setText(
            f"volta para a sala em {self.segundos_resultado}s")
        self.segundos_resultado -= 1
        QTimer.singleShot(1000, self._tique_resultado)

    def _voltar_para_sala(self):
        self.mostrando_resultado = False
        self.pilha.setCurrentIndex(0)
        if self.estado_sala:
            self._mostrar_sala(self.estado_sala)

    def _travar_texto_plano(self):
        """Nenhum rótulo interpreta marcação.

        QLabel nasce em AutoText e desenha HTML quando o texto parece HTML.
        Como nome de jogador, aviso e palavra vêm todos da rede, um
        `<img src=http://x/y>` viraria uma requisição feita pelo meu cliente.
        O servidor já sanitiza o nome; isto aqui é a segunda tranca, para o
        caso de o servidor do outro lado não ser o nosso.
        """
        for etiqueta in self.findChildren(QLabel):
            etiqueta.setTextFormat(Qt.TextFormat.PlainText)

    # ------------------------------------------------------------ teclado
    def _ligar_teclado(self):
        """QShortcut, e não keyPressEvent: a tabela da sala fica com o foco e
        consome Enter antes de o evento chegar à janela. Atalho de janela vale
        independente de quem está focado."""
        def atalho(sequencia, funcao):
            s = QShortcut(QKeySequence(sequencia), self)
            s.setContext(Qt.ShortcutContext.WindowShortcut)
            s.activated.connect(funcao)

        atalho("Return", self._aceitar_se_puder)
        atalho("Enter", self._aceitar_se_puder)
        atalho("Esc", self._recusar_se_puder)
        for letra in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            atalho(letra, lambda l=letra: self._chutar_se_puder(l))

    def _aceitar_se_puder(self):
        if self.mostrando_resultado:
            self._voltar_para_sala()
        elif self.bt_aceitar.isEnabled():
            self._aceitar()

    def _recusar_se_puder(self):
        if self.bt_recusar.isEnabled():
            self._recusar()

    def _chutar_se_puder(self, letra: str):
        b = self.botoes_letra.get(letra)
        if b and b.isEnabled():
            self._chutar(letra)

    # ------------------------------------------------------------ rede
    def _conectar(self):
        try:
            self.sock = socket.create_connection((self.host, self.porta), timeout=5)
            self.sock.setblocking(False)
        except OSError as erro:
            self.sock = None
            self.proxima_tentativa = time.monotonic() + ESPERA_RECONEXAO
            self._status(f"sem conexão ({erro.strerror or erro})", RUIM)
            return

        # Buffer novo por conexão: sobra da conexão morta viraria JSON inválido.
        self.enquadrador = Enquadrador()
        self.ultimo_contato = time.monotonic()
        self.conectado = True

        if self.token:
            self._status("retomando sessão...", ALERTA)
            self._mandar({"tipo": "reconectar", "token": self.token})
        else:
            self._status("conectado", BOM)
            self._mandar({"tipo": "entrar", "nome": self.nome})

    def _mandar(self, mensagem: dict):
        if not self.sock:
            return
        try:
            self.sock.sendall(empacotar(mensagem))
        except OSError:
            self._cair("falha ao enviar")

    def _cair(self, motivo: str):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.conectado = False
        self.proxima_tentativa = time.monotonic() + ESPERA_RECONEXAO
        self._status(f"reconectando ({motivo})", ALERTA)

    def _tick(self):
        agora = time.monotonic()
        if not self.conectado:
            if agora >= self.proxima_tentativa:
                self._conectar()
            return

        try:
            while True:
                dados = self.sock.recv(65536)
                if not dados:
                    self._cair("servidor fechou")
                    return
                for m in self.enquadrador.alimentar(dados):
                    try:
                        self._tratar(m)
                    except Exception as erro:
                        # Exceção que escapa de um slot do Qt não vira erro na
                        # tela: vira abort do processo, e a janela some sem dizer
                        # nada. Mensagem que eu não sei ler é mensagem
                        # descartada, e o aviso deixa isso visível em vez de
                        # silencioso.
                        self._avisar(
                            f"mensagem do servidor ignorada "
                            f"({type(erro).__name__}: {erro})", ALERTA)
        except BlockingIOError:
            pass
        except (OSError, ValueError, BufferExcedido) as erro:
            self._cair(type(erro).__name__)
            return

        if agora - self.ultimo_contato > JANELA_DETECCAO:
            # Silêncio sem erro de socket: é assim que uma máquina que evapora
            # se manifesta — ninguém avisa nada.
            self._cair(f"servidor mudo há {agora - self.ultimo_contato:.0f}s")

    # ------------------------------------------------------------ mensagens
    def _tratar(self, m: dict):
        self.ultimo_contato = time.monotonic()
        tipo = m.get("tipo")

        servidor = m.get("servidor")
        if servidor and servidor != self.servidor_atual:
            anterior = self.servidor_atual
            self.servidor_atual = servidor
            self.lbl_servidor.setText(f"servidor: {servidor}")
            if anterior:
                self._avisar(f"o servidor mudou: {anterior} → {servidor}", BOM)
                self._piscar()

        if tipo == "sessao":
            self._salvar_token(m.get("token") or "")
            self.meu_id = m.get("id")
            atribuido = m.get("nome")
            if atribuido and atribuido != self.nome:
                self._avisar(f"o nome '{self.nome}' já estava em uso — "
                             f"você entrou como '{atribuido}'", ALERTA)
                self.nome = atribuido
                self.setWindowTitle(f"Jogo da Forca — {self.nome}")
            self.lbl_eu.setText(f"{self.nome}   (jogador {self.meu_id})")
            self._status("conectado", BOM)

        elif tipo == "recusado":
            self._avisar(f"sessão anterior perdida ({m.get('msg')}) — "
                         f"entrando de novo", ALERTA)
            self._esquecer_token()
            self._mandar({"tipo": "entrar", "nome": self.nome})

        elif tipo == "substituido":
            self._avisar(f"{m.get('msg')} — vou entrar como jogador novo", ALERTA)
            # Esquecer o token é o que quebra o laço de expulsão: sem ele
            # as duas conexões reconectam com a mesma identidade e se
            # derrubam em turnos, uma vez por segundo.
            self._esquecer_token()

        elif tipo == "sala":
            self.estado_sala = m
            self._mostrar_sala(m)

        elif tipo == "estado":
            aceito = m.get("meu_lance")
            # Confere o tipo: um instantâneo hostil pode plantar qualquer coisa
            # em ultimo_lance, e o servidor devolve isso aqui como meu_lance.
            if isinstance(aceito, bool) or not isinstance(aceito, int):
                aceito = None
            if aceito is not None and aceito >= self.proximo_lance:
                self.proximo_lance = aceito + 1
            if self.lance_pendente is not None:
                if aceito == self.lance_pendente[0]:
                    self.lance_pendente = None
                else:
                    self._avisar(f"reenviando o chute '{self.lance_pendente[1]}'",
                                 ALERTA)
                    self._enviar_chute()
            self.estado_sala = None
            if m.get("encerrada"):
                self._mostrar_resultado(m)
            else:
                self._mostrar_partida(m)

        elif tipo == "erro":
            self.lance_pendente = None
            self._avisar(m.get("msg", ""), RUIM)

        elif tipo == "aviso":
            self._avisar(m.get("msg", ""), TEXTO)

    # ------------------------------------------------------------ telas
    def _mostrar_sala(self, s: dict):
        # Com o resultado na tela, a sala é atualizada por baixo mas não rouba a
        # vista: o servidor manda 'sala' milissegundos depois do fim da partida.
        if not self.mostrando_resultado:
            self.pilha.setCurrentIndex(0)
        legivel = {"avulso": "livre", "convidou": "convidou alguém",
                   "convidado": "foi convidado", "na_fila": "na fila",
                   "jogando": "em partida", "suspenso": "caiu, aguardando"}

        jogadores = s.get("jogadores") or []
        self.tabela.setRowCount(len(jogadores))
        for linha, j in enumerate(jogadores):
            eu = j.get("id") == s.get("voce")
            estado = j.get("estado") or "?"
            valores = (str(j.get("id", "?")),
                       str(j.get("nome") or "?") + ("   ← você" if eu else ""),
                       legivel.get(estado, estado))
            for col, txt in enumerate(valores):
                item = QTableWidgetItem(txt)
                if eu:
                    item.setForeground(QColor(DESTAQUE))
                    f = item.font(); f.setBold(True); item.setFont(f)
                self.tabela.setItem(linha, col, item)

        partes = []
        if s.get("sua_posicao"):
            partes.append(f"sua dupla está na posição {s['sua_posicao']} da fila")
        if s.get("partida_ativa"):
            partes.append("uma partida está em andamento — aguarde o slot liberar")
        self.lbl_fila.setText("      ".join(partes))

        convite = s.get("convite") or None
        recebido = bool(convite and convite.get("direcao") == "recebido")
        self.bt_aceitar.setEnabled(recebido)
        self.bt_recusar.setEnabled(recebido)
        self.bt_convidar.setEnabled(s.get("seu_estado") == "avulso")

        if recebido:
            self._avisar(f"{convite.get('nome', '?')} te convidou — "
                         f"{convite.get('faltam', '?')}s para responder", DESTAQUE)
        elif convite:
            self._avisar(f"aguardando resposta de {convite.get('nome', '?')} "
                         f"({convite.get('faltam', '?')}s)", APAGADO)

    def _mostrar_partida(self, e: dict):
        # Uma partida nova manda na tela. Sem apagar a flag, a contagem regressiva
        # do resultado anterior ainda dispararia e jogaria o jogador para a sala
        # no meio do jogo novo.
        self.mostrando_resultado = False
        self.pilha.setCurrentIndex(1)
        jogadores = e.get("jogadores") or []
        max_erros = e.get("max_erros", MAX_ERROS)

        for lado, j in enumerate(jogadores[:2]):
            eu = j.get("id") == e.get("voce")
            self.lbl_jogador[lado].setText(
                f"{j.get('nome', '?')}{'  (você)' if eu else ''}"
                f"     {j.get('erros', 0)}/{max_erros}")
            self.lbl_jogador[lado].setStyleSheet(
                f"font-size: 13pt; font-weight: 600; "
                f"color: {DESTAQUE if eu else TEXTO};")
            self.forcas[lado].definir(j.get("erros", 0))

        self.lbl_palavra.setText(e.get("painel") or "")
        self.relogio_falta = e.get("falta_jogada")
        self.relogio_desde = time.monotonic()
        self.relogio_prazo = e.get("prazo_jogada") or 0
        self._pintar_relogio()

        # Letra já chutada e letra indisponível por não ser a vez ficavam
        # idênticas na tela. Agora a chutada mostra o resultado: verde se a
        # revelou no painel, vermelho se não.
        usadas = set(e.get("chutadas") or [])
        revelado = set(normalizar((e.get("painel") or "").replace(" ", "")))
        minha_vez = (not e.get("encerrada") and not e.get("suspensa")
                     and e.get("turno") == e.get("voce"))

        for letra, b in self.botoes_letra.items():
            b.setEnabled(minha_vez and letra not in usadas)
            if letra in usadas:
                cor = BOM if letra in revelado else RUIM
                b.setStyleSheet(
                    f"QPushButton#letra {{ color: {cor}; border-color: {cor};"
                    f" background: transparent; }}")
            else:
                b.setStyleSheet("")

        if e.get("encerrada"):
            venceu = e.get("vencedor") == e.get("voce")
            self.lbl_turno.setText(
                ("VOCÊ VENCEU!" if venceu else "você perdeu.")
                + f"     a palavra era {e.get('palavra')}")
            self.lbl_turno.setStyleSheet(
                f"font-size: 14pt; font-weight: 600; color: {BOM if venceu else RUIM};")
        elif e.get("suspensa"):
            self.lbl_turno.setText(
                f"PARTIDA SUSPENSA — {e.get('nome_ausente')} caiu. "
                f"anula em {e.get('falta_voltar')}s se não voltar")
            self.lbl_turno.setStyleSheet(
                f"font-size: 14pt; font-weight: 600; color: {ALERTA};")
        elif minha_vez:
            self.lbl_turno.setText("SUA VEZ — escolha uma letra")
            self.lbl_turno.setStyleSheet(
                f"font-size: 14pt; font-weight: 600; color: {BOM};")
        else:
            outro = next((j.get("nome", "?") for j in jogadores
                          if j.get("id") == e.get("turno")), "?")
            self.lbl_turno.setText(f"aguardando {outro}...")
            self.lbl_turno.setStyleSheet(
                f"font-size: 14pt; font-weight: 600; color: {APAGADO};")

    # ------------------------------------------------------------ ações
    def _convidar(self):
        linha = self.tabela.currentRow()
        if linha < 0 or not self.estado_sala:
            self._avisar("selecione um jogador na lista primeiro", ALERTA)
            return
        alvo = int(self.tabela.item(linha, 0).text())
        if alvo == self.estado_sala["voce"]:
            self._avisar("não dá para convidar a si mesmo", RUIM)
            return
        self._mandar({"tipo": "convidar", "alvo": alvo})

    def _aceitar(self):
        self._mandar({"tipo": "aceitar"})

    def _recusar(self):
        self._mandar({"tipo": "recusar"})

    def _chutar(self, letra: str):
        self.lance_pendente = (self.proximo_lance, letra)
        self.proximo_lance += 1
        self._enviar_chute()

    def _enviar_chute(self):
        if self.lance_pendente is None:
            return
        numero, letra = self.lance_pendente
        self._mandar({"tipo": "chute", "letra": letra, "lance": numero})

    # ------------------------------------------------------------ avisos
    def _status(self, texto: str, cor: str):
        self.lbl_conexao.setText(texto)
        self.lbl_conexao.setStyleSheet(f"color: {cor};")
        if cor == BOM:
            self.faixa.hide()
        else:
            self.faixa.setText(f"⚠  {texto.upper()}")
            self.faixa.setStyleSheet(
                f"#faixa {{ background: {cor}; color: {FUNDO};"
                f" font-size: 12pt; font-weight: 700; letter-spacing: 1px; }}")
            self.faixa.show()

    def _avisar(self, texto: str, cor: str = TEXTO):
        if texto:
            self.lbl_msg.setText(texto)
            self.lbl_msg.setStyleSheet(f"color: {cor};")

    def _piscar(self, restantes: int = 6):
        """Chama atenção para a troca de servidor, que é o ponto da demo."""
        if restantes <= 0:
            self.lbl_servidor.setStyleSheet(
                f"font-size: 15pt; font-weight: 700; color: {DESTAQUE};")
            return
        cor = BOM if restantes % 2 else PAINEL
        self.lbl_servidor.setStyleSheet(
            f"font-size: 15pt; font-weight: 700; color: {cor};")
        QTimer.singleShot(180, lambda: self._piscar(restantes - 1))

    def closeEvent(self, evento):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        evento.accept()


def main():
    parser = argparse.ArgumentParser(description="Cliente gráfico do jogo da forca")
    parser.add_argument("nome")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--porta", type=int, default=5000)
    parser.add_argument("--sessao-nova", action="store_true",
                        help="ignora o token salvo e entra como jogador novo")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 10))
    janela = JanelaForca(args.nome, args.host, args.porta, args.sessao_nova)

    # Centraliza: com duas ou três janelas abertas na demonstração, o padrão do
    # Windows empilha todas no mesmo canto.
    tela = app.primaryScreen().availableGeometry()
    janela.resize(1000, 820)
    quadro = janela.frameGeometry()
    quadro.moveCenter(tela.center())
    janela.move(quadro.topLeft())
    janela.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
