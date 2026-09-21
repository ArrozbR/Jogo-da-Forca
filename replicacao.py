import socket
import time

from protocolo import Enquadrador, empacotar

# Muito menor que a janela de detecção do cliente (3 heartbeats de 2s = 6s): mesmo
# cortando o prazo em todo lance, o laço nunca para tempo suficiente para os
# clientes concluírem que o primário morreu.
TIMEOUT_REPLICACAO = 0.3

INTERVALO_RETENTATIVA = 5.0


class Replicador:
    """Canal do primário para o backup: envia o estado e espera confirmação.

    Síncrono e com prazo curto de propósito. O estado precisa chegar ao backup
    *antes* de o cliente receber a confirmação do lance — replicar depois deixaria
    uma divergência permanente, porque o jogador já teria visto a letra. Mas o
    laço de eventos tem thread única, e um `send` sem prazo para uma máquina
    morta pendura o servidor inteiro pelos ~15 minutos de retransmissão do TCP.
    """

    def __init__(self, host: str, porta: int, timeout: float = TIMEOUT_REPLICACAO,
                 log=None):
        self.host = host
        self.porta = porta
        self.timeout = timeout
        self.log = log or (lambda _: None)
        self.sock: socket.socket | None = None
        self.enquadrador = Enquadrador()
        self.seq = 0
        self.proxima_tentativa = 0.0

    @property
    def ativo(self) -> bool:
        return self.sock is not None

    def conectar(self) -> bool:
        if self.ativo:
            return True
        try:
            self.sock = socket.create_connection((self.host, self.porta),
                                                 timeout=self.timeout)
            self.sock.settimeout(self.timeout)
            self.enquadrador = Enquadrador()
            self.log(f"replicação ligada a {self.host}:{self.porta}")
            return True
        except OSError as erro:
            self.sock = None
            self.proxima_tentativa = time.monotonic() + INTERVALO_RETENTATIVA
            self.log(f"backup inacessível ({erro})")
            return False

    def manutencao(self) -> bool:
        """Retenta em segundo plano. True quando o backup acabou de voltar.

        Não tenta a cada lance de propósito: seriam 300ms perdidos por lance
        enquanto o backup estiver fora.
        """
        if self.ativo or time.monotonic() < self.proxima_tentativa:
            return False
        return self.conectar()

    def replicar(self, estado: dict) -> bool:
        return self._trocar({"tipo": "estado", "dados": estado})

    def bater(self) -> bool:
        return self._trocar({"tipo": "bat"})

    def _trocar(self, mensagem: dict) -> bool:
        if not self.ativo:
            return False
        self.seq += 1
        mensagem = {**mensagem, "seq": self.seq}
        try:
            self.sock.sendall(empacotar(mensagem))
            resposta = self._ler_resposta()
            if resposta.get("seq") != self.seq:
                raise OSError(f"confirmação fora de ordem: {resposta.get('seq')}")
            return True
        except (OSError, ValueError, TimeoutError) as erro:
            self._cair(f"{type(erro).__name__}: {erro}")
            return False

    def _ler_resposta(self) -> dict:
        fim = time.monotonic() + self.timeout
        while True:
            restante = fim - time.monotonic()
            if restante <= 0:
                raise TimeoutError("backup não confirmou no prazo")
            self.sock.settimeout(restante)
            dados = self.sock.recv(4096)
            if not dados:
                raise OSError("backup fechou a conexão")
            mensagens = self.enquadrador.alimentar(dados)
            if mensagens:
                return mensagens[0]

    def _cair(self, motivo: str):
        self.log(f"replicação caiu ({motivo}) — seguindo SEM backup")
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.enquadrador = Enquadrador()
        self.proxima_tentativa = time.monotonic() + INTERVALO_RETENTATIVA

    def fechar(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
