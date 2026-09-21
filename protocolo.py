import json

LIMITE_BUFFER = 64 * 1024


class BufferExcedido(Exception):
    pass


class Enquadrador:
    """Acumula bytes de um socket e devolve apenas mensagens completas.

    O buffer é mantido em bytes, e nunca em str: um caractere acentuado em UTF-8
    ocupa mais de um byte e pode ser partido entre dois recv, o que faria o
    decode levantar exceção no meio de uma palavra como MAÇÃ.
    """

    def __init__(self):
        self._buf = bytearray()

    def alimentar(self, dados: bytes) -> list[dict]:
        self._buf.extend(dados)
        mensagens = []

        while True:
            fim = self._buf.find(b"\n")
            if fim < 0:
                break
            linha = bytes(self._buf[:fim])
            del self._buf[: fim + 1]
            if linha.strip():
                mensagens.append(json.loads(linha.decode("utf-8")))

        if len(self._buf) > LIMITE_BUFFER:
            raise BufferExcedido(
                f"{len(self._buf)} bytes acumulados sem delimitador"
            )

        return mensagens


def empacotar(mensagem: dict) -> bytes:
    return (json.dumps(mensagem, ensure_ascii=False) + "\n").encode("utf-8")
