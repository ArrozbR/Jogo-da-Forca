import unicodedata

MAX_ERROS = 6


def normalizar(texto: str) -> str:
    decomposto = unicodedata.normalize("NFD", texto)
    return "".join(c for c in decomposto if unicodedata.category(c) != "Mn").upper()


class Partida:
    """Palavra compartilhada, turno estritamente alternado, erros por jogador.

    A palavra é guardada em duas formas alinhadas por índice: `palavra` para
    exibir (com acento) e `comparacao` para casar o chute. Assim o chute A
    revela o Ã de MAÇÃ sem que o jogador veja a palavra escrita errada.
    """

    def __init__(self, palavra: str, slots: list[str]):
        self.palavra = palavra.upper()
        self.comparacao = normalizar(palavra)
        self.slots = list(slots)
        self.chutadas: set[str] = set()
        self.erros = {slot: 0 for slot in slots}
        self.turno = self.slots[0]
        self.vencedor: str | None = None
        self.encerrada = False

    def painel(self) -> str:
        visivel = []
        for exibir, comparar in zip(self.palavra, self.comparacao):
            if not comparar.isalpha() or comparar in self.chutadas:
                visivel.append(exibir)
            else:
                visivel.append("_")
        return " ".join(visivel)

    def completa(self) -> bool:
        return all(
            not c.isalpha() or c in self.chutadas for c in self.comparacao
        )

    def chutar(self, slot: str, letra: str) -> tuple[bool, str]:
        if self.encerrada:
            return False, "a partida já terminou"
        if slot != self.turno:
            return False, "não é a sua vez"

        letra = normalizar(letra)
        if len(letra) != 1 or not letra.isalpha():
            return False, "envie uma única letra"
        if letra in self.chutadas:
            return False, f"a letra {letra} já foi chutada"

        self.chutadas.add(letra)
        acertou = letra in self.comparacao

        if not acertou:
            self.erros[slot] += 1

        if self.completa():
            self.vencedor = slot
            self.encerrada = True
        elif self.erros[slot] >= MAX_ERROS:
            self.vencedor = self._adversario(slot)
            self.encerrada = True
        else:
            self.turno = self._adversario(slot)

        return True, "acertou" if acertou else "errou"

    def _adversario(self, slot: str) -> str:
        return self.slots[1] if slot == self.slots[0] else self.slots[0]

    def para_dict(self) -> dict:
        # `comparacao` fica fora: é derivada de `palavra` e o construtor a refaz.
        return {
            "palavra": self.palavra,
            "slots": list(self.slots),
            "chutadas": sorted(self.chutadas),
            "erros": {str(k): v for k, v in self.erros.items()},
            "turno": self.turno,
            "vencedor": self.vencedor,
            "encerrada": self.encerrada,
        }

    @classmethod
    def de_dict(cls, d: dict) -> "Partida":
        p = cls(d["palavra"], d["slots"])
        p.chutadas = set(d["chutadas"])
        # Chaves de JSON são sempre texto; os slots aqui são ids inteiros.
        p.erros = {int(k): v for k, v in d["erros"].items()}
        p.turno = d["turno"]
        p.vencedor = d["vencedor"]
        p.encerrada = d["encerrada"]
        return p
