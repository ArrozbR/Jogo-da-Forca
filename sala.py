import secrets
import time

AVULSO = "avulso"
CONVIDOU = "convidou"
CONVIDADO = "convidado"
NA_FILA = "na_fila"
JOGANDO = "jogando"
SUSPENSO = "suspenso"

PRAZO_CONVITE = 30.0

# Tem de ser maior que a janela de detecção do cliente (3 heartbeats de 2s = 6s),
# senão o prazo venceria antes de o cliente sequer perceber que caiu, e toda queda
# anularia a partida — inclusive um failover, que desconecta os dois por definição.
PRAZO_RECONEXAO = 30.0


class Jogador:
    def __init__(self, ident: int, nome: str, token: str):
        self.id = ident
        self.nome = nome
        self.token = token
        self.estado = AVULSO
        self.prazo_reconexao: float | None = None


class Sala:
    """Sala de espera: jogadores se convidam, e as duplas formadas entram numa fila.

    Existe uma partida por vez (leitura literal de "um jogador por vez" do
    enunciado), então a fila guarda **duplas já formadas**, não jogadores soltos.
    Quem termina uma partida volta a avulso e, ao se emparelhar de novo, entra no
    fim da fila de duplas.
    """

    def __init__(self, prazo_convite: float = PRAZO_CONVITE,
                 prazo_reconexao: float = PRAZO_RECONEXAO):
        self.prazo = prazo_convite
        self.prazo_reconexao = prazo_reconexao
        self.jogadores: dict[int, Jogador] = {}
        self.fila: list[tuple[int, int]] = []
        self.convites: dict[int, tuple[int, float]] = {}
        self._proximo_id = 1

    def _nome_livre(self, nome: str) -> str:
        """Nomes repetidos ganham 3 dígitos: dois 'Joao' na sala são indistinguíveis,
        tanto na lista quanto no painel da partida."""
        usados = {j.nome for j in self.jogadores.values()}
        if nome not in usados:
            return nome
        for _ in range(60):
            candidato = f"{nome}{secrets.randbelow(900) + 100}"
            if candidato not in usados:
                return candidato
        return f"{nome}{secrets.token_hex(2)}"

    def entrar(self, nome: str) -> tuple[int, str]:
        nome = self._nome_livre(nome)
        ident = self._proximo_id
        self._proximo_id += 1
        # Opaco e imprevisível de propósito: um id sequencial permitiria a qualquer
        # um adivinhar o token de outro jogador e herdar a partida dele.
        token = secrets.token_hex(16)
        self.jogadores[ident] = Jogador(ident, nome, token)
        return ident, token

    def por_token(self, token: str) -> Jogador | None:
        if not token:
            return None
        return next((j for j in self.jogadores.values() if j.token == token), None)

    def suspender(self, ident: int) -> bool:
        """Marca o jogador como ausente, preservando a partida dele."""
        j = self.jogadores.get(ident)
        if j is None:
            return False
        j.estado = SUSPENSO
        j.prazo_reconexao = time.monotonic() + self.prazo_reconexao
        return True

    def reconectar(self, token: str) -> tuple[int | None, str]:
        j = self.por_token(token)
        if j is None:
            return None, "token desconhecido"
        if j.estado == SUSPENSO:
            if j.prazo_reconexao is not None and time.monotonic() > j.prazo_reconexao:
                return None, "prazo de reconexão vencido"
            j.estado = JOGANDO
            j.prazo_reconexao = None
            return j.id, "partida retomada"
        # Reconexão com o registro ainda intacto: o servidor nem notou a queda.
        # A conexão nova substitui a antiga, que pode ser um socket zumbi.
        return j.id, "sessão retomada"

    def suspensos_vencidos(self) -> list[int]:
        agora = time.monotonic()
        return [
            j.id for j in self.jogadores.values()
            if j.estado == SUSPENSO and j.prazo_reconexao is not None
            and agora > j.prazo_reconexao
        ]

    def falta_para_voltar(self, ident: int) -> int | None:
        j = self.jogadores.get(ident)
        if j is None or j.estado != SUSPENSO or j.prazo_reconexao is None:
            return None
        return max(0, round(j.prazo_reconexao - time.monotonic()))

    def sair(self, ident: int) -> list[int]:
        """Remove o jogador e devolve os ids que ficaram livres por causa disso."""
        if self.jogadores.pop(ident, None) is None:
            return []

        afetados = []
        for de, (para, _) in list(self.convites.items()):
            if ident in (de, para):
                del self.convites[de]
                outro = para if de == ident else de
                if outro in self.jogadores:
                    self.jogadores[outro].estado = AVULSO
                    afetados.append(outro)

        for dupla in list(self.fila):
            if ident in dupla:
                self.fila.remove(dupla)
                parceiro = dupla[0] if dupla[1] == ident else dupla[1]
                if parceiro in self.jogadores:
                    self.jogadores[parceiro].estado = AVULSO
                    afetados.append(parceiro)

        return afetados

    def _porque_voce_nao_pode(self, jogador: Jogador) -> str:
        outro = self.convites.get(jogador.id)
        if jogador.estado == CONVIDOU and outro:
            nome = self.jogadores[outro[0]].nome if outro[0] in self.jogadores else "alguém"
            return f"você já tem um convite aguardando resposta de {nome}"
        de = self.quem_convidou(jogador.id)
        if jogador.estado == CONVIDADO and de is not None:
            nome = self.jogadores[de].nome if de in self.jogadores else "alguém"
            return f"responda o convite de {nome} primeiro: /s aceita, /n recusa"
        if jogador.estado == NA_FILA:
            return "você já está em uma dupla, aguardando a vez na fila"
        if jogador.estado == JOGANDO:
            return "você está em partida"
        return "você não pode convidar agora"

    def _porque_ele_nao_pode(self, jogador: Jogador) -> str:
        if jogador.estado in (CONVIDOU, CONVIDADO):
            return f"{jogador.nome} já tem um convite pendente"
        if jogador.estado == NA_FILA:
            return f"{jogador.nome} já está em uma dupla na fila"
        if jogador.estado == JOGANDO:
            return f"{jogador.nome} está em partida"
        return f"{jogador.nome} não está disponível"

    def convidar(self, ident: int, alvo: int) -> tuple[bool, str]:
        se = self.jogadores.get(ident)
        ele = self.jogadores.get(alvo)
        if se is None:
            return False, "você não está na sala"
        if ele is None:
            return False, f"não existe jogador {alvo} na sala"
        if alvo == ident:
            return False, "não dá para convidar a si mesmo"
        if se.estado != AVULSO:
            return False, self._porque_voce_nao_pode(se)
        if ele.estado != AVULSO:
            return False, self._porque_ele_nao_pode(ele)

        se.estado = CONVIDOU
        ele.estado = CONVIDADO
        self.convites[ident] = (alvo, time.monotonic() + self.prazo)
        return True, f"convite enviado para {ele.nome}"

    def quem_convidou(self, ident: int) -> int | None:
        """Id de quem convidou este jogador, ou None se não há convite pendente."""
        return next((d for d, (para, _) in self.convites.items() if para == ident), None)

    def responder(self, ident: int, aceita: bool) -> tuple[bool, str, tuple[int, int] | None]:
        de = self.quem_convidou(ident)
        if de is None:
            return False, "você não tem convite pendente", None

        del self.convites[de]
        convidante = self.jogadores.get(de)
        convidado = self.jogadores.get(ident)

        if not aceita:
            for j in (convidante, convidado):
                if j:
                    j.estado = AVULSO
            return True, "convite recusado", None

        # Se o convidante caiu entre o convite e a resposta, não há dupla a formar.
        if convidante is None:
            if convidado:
                convidado.estado = AVULSO
            return False, "quem convidou saiu da sala", None

        convidante.estado = NA_FILA
        convidado.estado = NA_FILA
        dupla = (de, ident)
        self.fila.append(dupla)
        return True, "dupla formada", dupla

    def expirar(self, agora: float | None = None) -> list[tuple[int, int]]:
        """Derruba convites vencidos e devolve os pares (convidante, convidado)."""
        agora = agora if agora is not None else time.monotonic()
        vencidos = []
        for de, (para, prazo) in list(self.convites.items()):
            if agora >= prazo:
                del self.convites[de]
                for ident in (de, para):
                    j = self.jogadores.get(ident)
                    if j:
                        j.estado = AVULSO
                vencidos.append((de, para))
        return vencidos

    def proxima_dupla(self) -> tuple[int, int] | None:
        """Retira a primeira dupla da fila e marca os dois como jogando."""
        while self.fila:
            a, b = self.fila.pop(0)
            if a in self.jogadores and b in self.jogadores:
                self.jogadores[a].estado = JOGANDO
                self.jogadores[b].estado = JOGANDO
                return (a, b)
        return None

    def encerrar_partida(self, dupla: tuple[int, int]) -> None:
        for ident in dupla:
            j = self.jogadores.get(ident)
            if j and j.estado == JOGANDO:
                j.estado = AVULSO

    def para_dict(self) -> dict:
        """Instantâneo para replicação.

        Prazos viajam como **duração restante**, nunca como instante absoluto:
        time.monotonic() tem origem própria em cada processo, então um prazo
        absoluto da VM1 não significaria nada na VM2.
        """
        agora = time.monotonic()
        return {
            "proximo_id": self._proximo_id,
            "jogadores": [
                {
                    "id": j.id,
                    "nome": j.nome,
                    "token": j.token,
                    "estado": j.estado,
                    "falta_reconexao": (
                        max(0.0, j.prazo_reconexao - agora)
                        if j.prazo_reconexao is not None else None
                    ),
                }
                for j in self.jogadores.values()
            ],
            "fila": [list(d) for d in self.fila],
            "convites": [
                {"de": de, "para": para, "faltam": max(0.0, prazo - agora)}
                for de, (para, prazo) in self.convites.items()
            ],
        }

    def aplicar_dict(self, d: dict) -> None:
        agora = time.monotonic()
        self._proximo_id = d["proximo_id"]

        self.jogadores = {}
        for reg in d["jogadores"]:
            j = Jogador(reg["id"], reg["nome"], reg["token"])
            j.estado = reg["estado"]
            j.prazo_reconexao = (
                agora + reg["falta_reconexao"]
                if reg["falta_reconexao"] is not None else None
            )
            self.jogadores[j.id] = j

        self.fila = [tuple(par) for par in d["fila"]]
        self.convites = {
            c["de"]: (c["para"], agora + c["faltam"]) for c in d["convites"]
        }

    def estado_para(self, ident: int, partida_ativa: bool) -> dict:
        agora = time.monotonic()
        convite = None

        recebido = next(((d, p) for d, (para, p) in self.convites.items() if para == ident), None)
        if recebido:
            de, prazo = recebido
            convite = {
                "direcao": "recebido",
                "outro": de,
                "nome": self.jogadores[de].nome if de in self.jogadores else "?",
                "faltam": max(0, round(prazo - agora)),
            }
        elif ident in self.convites:
            para, prazo = self.convites[ident]
            convite = {
                "direcao": "enviado",
                "outro": para,
                "nome": self.jogadores[para].nome if para in self.jogadores else "?",
                "faltam": max(0, round(prazo - agora)),
            }

        posicao = next(
            (i + 1 for i, dupla in enumerate(self.fila) if ident in dupla), None
        )

        return {
            "tipo": "sala",
            "voce": ident,
            "seu_estado": self.jogadores[ident].estado if ident in self.jogadores else None,
            "jogadores": [
                {"id": j.id, "nome": j.nome, "estado": j.estado}
                for j in sorted(self.jogadores.values(), key=lambda x: x.id)
            ],
            "duplas_na_fila": [list(d) for d in self.fila],
            "sua_posicao": posicao,
            "convite": convite,
            "partida_ativa": partida_ativa,
        }
