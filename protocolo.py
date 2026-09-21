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
            if not linha.strip():
                continue

            mensagem = json.loads(linha.decode("utf-8"))
            # `[1,2,3]`, `"texto"` e `null` são JSON válido mas não são mensagem.
            # Sem este teste, o primeiro acesso a `.get("tipo")` levantava
            # AttributeError e derrubava o laço de eventos inteiro — um cliente
            # mandando um único byte de lixo tirava o servidor do ar.
            if not isinstance(mensagem, dict):
                raise ValueError(f"mensagem não é objeto JSON: {type(mensagem).__name__}")
            mensagens.append(mensagem)

        if len(self._buf) > LIMITE_BUFFER:
            raise BufferExcedido(
                f"{len(self._buf)} bytes acumulados sem delimitador"
            )

        return mensagens


def empacotar(mensagem: dict) -> bytes:
    return (json.dumps(mensagem, ensure_ascii=False) + "\n").encode("utf-8")
