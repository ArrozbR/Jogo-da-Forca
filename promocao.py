import json
import socket
import subprocess
import time

# Dois prazos, e não um. "Não alcanço o agente" tem de falhar rápido, senão o
# takeover fica parado esperando uma máquina que sumiu. "O agente atendeu e está
# desligando a VM" tem de ter tempo: o desligamento envolve um controlvm e até
# 20 sondagens do estado, e um prazo único de 5s expirava no meio disso — o
# backup então registrava "fencing NÃO confirmado" e assumia o IP assim mesmo,
# degradando em silêncio para o caminho inseguro.
TIMEOUT_CONEXAO = 3.0
TIMEOUT_RESPOSTA = 25.0


class Promotor:
    """No backup: mata o primário pelo hypervisor e assume o IP virtual.

    A ordem importa. Primeiro o fencing, depois o IP: assumir o endereço antes de
    garantir que o outro morreu é o caminho direto para duas máquinas atendendo o
    mesmo IP com estados divergindo.
    """

    def __init__(self, agente: tuple[str, int], vm_alvo: str, ip_virtual: str,
                 interface: str, script_assumir: str, log=None):
        self.agente = agente
        self.vm_alvo = vm_alvo
        self.ip_virtual = ip_virtual
        self.interface = interface
        self.script_assumir = script_assumir
        self.log = log or (lambda _: None)
        self.promovido = False

    def _pedir_ao_agente(self, acao: str) -> tuple[bool, str]:
        try:
            with socket.create_connection(self.agente, timeout=TIMEOUT_CONEXAO) as s:
                s.settimeout(TIMEOUT_RESPOSTA)
                s.sendall((json.dumps({"acao": acao, "vm": self.vm_alvo}) + "\n").encode())
                bruto = b""
                while b"\n" not in bruto and len(bruto) < 4096:
                    pedaco = s.recv(1024)
                    if not pedaco:
                        break
                    bruto += pedaco
            if not bruto:
                return False, "agente não respondeu"
            resposta = json.loads(bruto.split(b"\n", 1)[0].decode("utf-8"))
            return bool(resposta.get("ok")), str(resposta.get("motivo", ""))
        except (OSError, ValueError) as erro:
            return False, f"{type(erro).__name__}: {erro}"

    def _assumir_ip(self) -> bool:
        try:
            r = subprocess.run(
                ["sudo", "-n", self.script_assumir, self.ip_virtual, self.interface],
                capture_output=True, text=True, timeout=20,
            )
            if r.returncode != 0:
                self.log(f"assumir_ip falhou: {r.stderr.strip() or r.stdout.strip()}")
                return False
            self.log(f"IP virtual {self.ip_virtual} assumido em {self.interface}")
            return True
        except (OSError, subprocess.TimeoutExpired) as erro:
            self.log(f"assumir_ip não executou: {erro}")
            return False

    def promover(self) -> bool:
        if self.promovido:
            return True

        self.log(f"PROMOÇÃO iniciada — fencing de {self.vm_alvo}")
        morto, motivo = self._pedir_ao_agente("desligar")

        if morto:
            self.log(f"fencing confirmado: {motivo}")
        else:
            # Decisão consciente, registrada como limitação: se o hypervisor
            # também estiver inalcançável, assumimos sem confirmar. Isso troca
            # correção no caso raro por disponibilidade no caso comum — e permite
            # split-brain se o primário estiver vivo e isolado.
            self.log(f"fencing NÃO confirmado ({motivo}) — assumindo mesmo assim; "
                     f"risco de split-brain se o primário estiver vivo e isolado")

        # Sem IP virtual, promover é só começar a atender: os clientes já
        # conhecem este endereço pela lista e vêm sozinhos. É o modo usado com
        # as máquinas em redes que não são nossas, onde anunciar um IP que era
        # de outro aparelho seria bloqueado como ARP spoofing.
        if not self.ip_virtual:
            self.log("sem IP virtual: os clientes chegam pela lista de servidores")
        elif not self._assumir_ip():
            self.log("promoção abortada: não foi possível assumir o IP virtual")
            return False

        self.promovido = True
        self.log("PROMOÇÃO concluída — este servidor agora atende "
                 + ("o IP virtual" if self.ip_virtual else "os jogadores"))
        return True
