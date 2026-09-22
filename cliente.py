"""Cliente de terminal — FERRAMENTA DE TESTE, não é a entrega.

A entrega é o `cliente_gui.py`, em PyQt6. Este cliente continua no projeto por
ser roteirizável: dá para dirigi-lo por script e verificar o protocolo, a
reconexão e o failover de forma automatizada, o que não se faz com uma janela.
"""
import argparse
import os
import queue
import selectors
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

from protocolo import BufferExcedido, Enquadrador, empacotar

ESPERA_SELECT = 0.2
JANELA_DETECCAO = 6.0
ESPERA_RECONEXAO = 1.0
LARGURA = 16

AJUDA = """comandos:
  /c <id>   convidar o jogador <id>
  /s        aceitar o convite recebido
  /n        recusar o convite recebido
  /?        mostrar esta ajuda
  <letra>   chutar uma letra (durante a partida)"""


def bonequinho(erros: int) -> list[str]:
    cabeca = "O" if erros >= 1 else " "
    tronco = "|" if erros >= 2 else " "
    braco_e = "/" if erros >= 3 else " "
    braco_d = "\\" if erros >= 4 else " "
    perna_e = "/" if erros >= 5 else " "
    perna_d = "\\" if erros >= 6 else " "
    return [
        " +---+",
        " |   |",
        f" {cabeca}   |",
        f"{braco_e}{tronco}{braco_d}  |",
        f"{perna_e} {perna_d}  |",
        "     |",
        "=======",
    ]


def ler_teclado(fila: queue.Queue):
    for linha in sys.stdin:
        fila.put(linha.strip())


class Cliente:
    def __init__(self, nome: str, host: str, porta: int, sessao_nova: bool = False):
        self.nome = nome
        self.host = host
        self.porta = porta
        self.fila_teclado: queue.Queue[str] = queue.Queue()
        self.meu_id = None
        self.servidor_atual = None
        self.na_partida = False
        self.deslocado = False

        # Cada chute leva um número. Se a confirmação não chegar, o mesmo número é
        # reenviado, e o servidor reconhece o reenvio em vez de contar duas vezes.
        self.proximo_lance = 1
        self.lance_pendente: tuple[int, str] | None = None

        # Em disco, e não só em memória: fechar a janela é a primeira coisa que
        # alguém tenta, e sem isto a sessão se perderia junto com o processo.
        # Montado com f-string, e não com with_suffix: o nome contém os pontos do
        # IP, e with_suffix trocaria ".1-5090" por ".token", fazendo endereços
        # diferentes colidirem no mesmo arquivo de sessão.
        pasta = Path(tempfile.gettempdir())
        base = f"forca-{nome}-{host}-{porta}"
        self.arquivo_token = pasta / f"{base}.token"
        self.arquivo_trava = pasta / f"{base}.lock"
        self._trava = None

        # Sem exclusividade, duas instâncias de mesmo nome leriam o mesmo token,
        # assumiriam a mesma sessão, e ficariam se expulsando uma à outra em laço.
        self.exclusivo = self._travar_sessao()
        if not self.exclusivo:
            print(f"  -- já existe um cliente '{nome}' nesta máquina; "
                  f"entrando como sessão nova e independente")
            self.token = None
        else:
            self.token = None if sessao_nova else self._ler_token()
            if sessao_nova:
                self._esquecer_token()

    def _travar_sessao(self) -> bool:
        """Uso exclusivo do arquivo de sessão. O SO solta a trava se o processo morrer."""
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

    def _ler_token(self) -> str | None:
        try:
            return self.arquivo_token.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def _salvar_token(self, token: str):
        self.token = token
        # Sem a trava, o arquivo é de outra instância: guarda só em memória.
        if not self.exclusivo:
            return
        try:
            self.arquivo_token.write_text(token, encoding="utf-8")
        except OSError:
            pass

    def _esquecer_token(self):
        self.token = None
        if not self.exclusivo:
            return
        try:
            self.arquivo_token.unlink()
        except OSError:
            pass

    def executar(self):
        threading.Thread(target=ler_teclado, args=(self.fila_teclado,), daemon=True).start()
        print(AJUDA)

        while True:
            try:
                if self._sessao():
                    return
            except (ConnectionError, OSError) as erro:
                print(f"  !! conexão caiu: {erro or 'socket fechado'}")

            if self.token is None and not self.deslocado:
                print("  !! sem sessão para retomar — encerrando")
                return
            # Deslocado por outra conexão: reconecta, mas como jogador novo, para
            # não disputar a mesma sessão em laço com quem tomou o lugar.
            self.deslocado = False
            print(f"  -- reconectando em {ESPERA_RECONEXAO:.0f}s...")
            time.sleep(ESPERA_RECONEXAO)

    def _sessao(self) -> bool:
        """Roda uma conexão até ela cair. True quando não há por que reconectar."""
        sock = socket.create_connection((self.host, self.porta), timeout=8)
        sock.setblocking(False)
        seletor = selectors.DefaultSelector()
        seletor.register(sock, selectors.EVENT_READ)

        # Buffer novo por conexão: um pedaço de mensagem que ficou na conexão morta
        # não pode contaminar a nova, ou a primeira leitura vira JSON inválido.
        self.enquadrador = Enquadrador()
        self.ultimo_contato = time.monotonic()
        self.avisou_queda = False

        if self.token:
            print(f"  -- retomando sessão em {self.host}:{self.porta}")
            sock.sendall(empacotar({"tipo": "reconectar", "token": self.token}))
        else:
            print(f"conectado a {self.host}:{self.porta} como {self.nome}")
            sock.sendall(empacotar({"tipo": "entrar", "nome": self.nome}))

        try:
            while True:
                for _ in seletor.select(timeout=ESPERA_SELECT):
                    dados = sock.recv(4096)
                    if not dados:
                        print("  !! servidor fechou a conexão")
                        return self.token is None
                    for mensagem in self.enquadrador.alimentar(dados):
                        self._tratar(sock, mensagem)

                self._drenar_teclado(sock)
                if self._desistir_da_conexao():
                    return False
        finally:
            seletor.close()
            sock.close()

    def _enviar_chute(self, sock):
        if self.lance_pendente is None:
            return
        numero, letra = self.lance_pendente
        sock.sendall(empacotar({"tipo": "chute", "letra": letra, "lance": numero}))

    def _desistir_da_conexao(self) -> bool:
        silencio = time.monotonic() - self.ultimo_contato
        if silencio > JANELA_DETECCAO:
            print(f"  !! servidor mudo há {silencio:.0f}s — vou reconectar")
            return True
        return False

    def _drenar_teclado(self, sock):
        while True:
            try:
                entrada = self.fila_teclado.get_nowait()
            except queue.Empty:
                return
            if not entrada:
                continue

            if entrada.startswith("/"):
                partes = entrada[1:].split()
                cmd = partes[0].lower() if partes else ""
                if cmd == "c" and len(partes) > 1 and partes[1].isdigit():
                    sock.sendall(empacotar({"tipo": "convidar", "alvo": int(partes[1])}))
                elif cmd == "s":
                    sock.sendall(empacotar({"tipo": "aceitar"}))
                elif cmd == "n":
                    sock.sendall(empacotar({"tipo": "recusar"}))
                elif cmd == "?":
                    print(AJUDA)
                else:
                    print("  !! comando inválido — /? mostra a ajuda")
            elif self.na_partida:
                self.lance_pendente = (self.proximo_lance, entrada[0])
                self.proximo_lance += 1
                self._enviar_chute(sock)
            else:
                print("  !! você não está numa partida — /? mostra a ajuda")

    def _tratar(self, sock, mensagem: dict):
        self.ultimo_contato = time.monotonic()

        servidor = mensagem.get("servidor")
        if servidor != self.servidor_atual:
            if self.servidor_atual is not None:
                print(f"\n>>> servidor mudou: {self.servidor_atual} -> {servidor} <<<\n")
            self.servidor_atual = servidor

        tipo = mensagem.get("tipo")
        if tipo == "sessao":
            self._salvar_token(mensagem["token"])
            self.meu_id = mensagem["id"]
            atribuido = mensagem.get("nome")
            if atribuido and atribuido != self.nome:
                print(f"  -- o nome '{self.nome}' já estava em uso; "
                      f"você entrou como '{atribuido}'")
                self.nome = atribuido
        elif tipo == "substituido":
            print(f"  !! {mensagem.get('msg')} — vou entrar como jogador novo")
            self._esquecer_token()
            self.deslocado = True
            self.na_partida = False
        elif tipo == "recusado":
            # O token não vale mais: entra como jogador novo em vez de insistir.
            print(f"  !! sessão anterior perdida ({mensagem.get('msg')}) — entrando de novo")
            self._esquecer_token()
            self.na_partida = False
            sock.sendall(empacotar({"tipo": "entrar", "nome": self.nome}))
        elif tipo == "estado":
            self.na_partida = not mensagem.get("encerrada")

            # O contador reinicia em 1 quando o processo reabre, mas o servidor
            # lembra do último lance aceito. Sem sincronizar, o primeiro chute
            # depois de reabrir seria confundido com reenvio e descartado.
            aceito = mensagem.get("meu_lance")
            if aceito is not None and aceito >= self.proximo_lance:
                self.proximo_lance = aceito + 1

            if self.lance_pendente is not None:
                if mensagem.get("meu_lance") == self.lance_pendente[0]:
                    self.lance_pendente = None      # o servidor aplicou
                else:
                    # O estado chegou sem o meu lance: ele se perdeu junto com a
                    # conexão anterior. Reenvia com o mesmo número.
                    print(f"  -- reenviando o chute '{self.lance_pendente[1]}'")
                    self._enviar_chute(sock)
            self._mostrar_partida(mensagem)
        elif tipo == "sala":
            self.na_partida = False
            self._mostrar_sala(mensagem)
        elif tipo == "erro":
            # O servidor recusou explicitamente: não faz sentido reenviar.
            self.lance_pendente = None
            print(f"  !! {mensagem.get('msg')}")
        elif tipo == "aviso":
            print(f"  -- {mensagem.get('msg')}")

    def _cabecalho(self, mensagem: dict) -> str:
        return f"\n--- {time.strftime('%H:%M:%S')} --- servidor: {mensagem['servidor']} ---"

    def _mostrar_sala(self, sala: dict):
        print(self._cabecalho(sala))
        print(f"SALA DE ESPERA — você é o jogador {sala['voce']} ({sala['seu_estado']})")

        for j in sala["jogadores"]:
            marca = " <- você" if j["id"] == sala["voce"] else ""
            print(f"  [{j['id']}] {j['nome']:<14} {j['estado']}{marca}")

        if sala["duplas_na_fila"]:
            print(f"  duplas na fila: {sala['duplas_na_fila']}")
        if sala["sua_posicao"]:
            print(f"  sua dupla está na posição {sala['sua_posicao']} da fila")
        if sala["partida_ativa"]:
            print("  uma partida está em andamento — aguarde o slot liberar")

        convite = sala.get("convite")
        if convite and convite["direcao"] == "recebido":
            print(f"\n>>> {convite['nome']} te convidou! /s aceita, /n recusa "
                  f"({convite['faltam']}s restantes)")
        elif convite:
            print(f"\n... aguardando resposta de {convite['nome']} ({convite['faltam']}s)")
        elif sala["seu_estado"] == "avulso":
            livres = [str(j["id"]) for j in sala["jogadores"]
                      if j["estado"] == "avulso" and j["id"] != sala["voce"]]
            if livres:
                print(f"\nconvide alguém: /c {livres[0]}   (livres: {', '.join(livres)})")
            else:
                print("\naguardando outro jogador livre entrar...")

    def _mostrar_partida(self, estado: dict):
        print(self._cabecalho(estado))
        jogadores = estado["jogadores"]

        titulos, colunas = [], []
        for j in jogadores:
            marca = " (você)" if j["id"] == estado["voce"] else ""
            titulos.append(f"{j['nome']}{marca}")
            colunas.append(bonequinho(j["erros"]))

        print("  ".join(t.ljust(LARGURA) for t in titulos))
        for linhas in zip(*colunas):
            print("  ".join(l.ljust(LARGURA) for l in linhas))
        print("  ".join(
            f"erros: {j['erros']}/{estado['max_erros']}".ljust(LARGURA) for j in jogadores
        ))

        print(f"\npalavra:  {estado['painel']}")
        if estado["chutadas"]:
            print(f"chutadas: {', '.join(estado['chutadas'])}")

        if estado["encerrada"]:
            venceu = estado["vencedor"] == estado["voce"]
            print(f"\n*** {'você venceu!' if venceu else 'você perdeu.'} "
                  f"a palavra era {estado['palavra']} ***")
        elif estado.get("suspensa"):
            print(f"\n[PARTIDA SUSPENSA] {estado['nome_ausente']} caiu. "
                  f"Anula em {estado['falta_voltar']}s se não voltar.")
        elif estado["turno"] == estado["voce"]:
            print("\n>>> sua vez. digite uma letra e Enter:")
        else:
            adversario = next((j["nome"] for j in jogadores if j["id"] == estado["turno"]), "?")
            print(f"\naguardando {adversario}...")


def main():
    # O console do Windows usa cp1252 por padrão e exibiria MAÇÃ como MA??.
    for fluxo in (sys.stdout, sys.stdin):
        fluxo.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Cliente do jogo da forca")
    parser.add_argument("nome")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--porta", type=int, default=5000)
    parser.add_argument("--sessao-nova", action="store_true",
                        help="ignora o token salvo e entra como jogador novo")
    args = parser.parse_args()

    try:
        Cliente(args.nome, args.host, args.porta, args.sessao_nova).executar()
    except (KeyboardInterrupt, BufferExcedido) as erro:
        print(f"\n[fim: {erro or 'interrompido'}]")


if __name__ == "__main__":
    main()
