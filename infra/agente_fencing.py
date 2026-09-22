"""Agente de fencing: roda no host Windows, desliga VMs a pedido.

Por que existe: o backup é uma VM Linux e não pode executar VBoxManage — o
hypervisor está no host. Então o backup precisa falar com alguém que possa
desligar o primário de verdade.

Isso não é um contorno: é exatamente como fencing funciona em sistemas reais. O
nó que quer assumir não mata o outro por conta própria, ele pede a um
**dispositivo de fencing** com autoridade sobre o hardware. Aqui o hypervisor é
esse dispositivo, e o agente é a interface dele.

E é essa autoridade que substitui o quórum: com apenas dois nós não existe
maioria, então quem consegue falar com o hypervisor vence, e quem não consegue
deveria se calar.

    python infra/agente_fencing.py --host 192.168.56.1 --porta 5010
"""
import argparse
import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

VBOXMANAGE = Path(r"C:\Program Files\Oracle\VirtualBox\VBoxManage.exe")
PERMITIDAS = {"VM1", "VM2"}

# Curto de propósito. Este é o prazo para o pedido CHEGAR, não para a VM
# morrer: quem conecta e não fala nada solta a linha em 5s em vez de 30.
TIMEOUT_PEDIDO = 5.0


def log(texto: str):
    print(f"[{time.strftime('%H:%M:%S')}] [fencing] {texto}", flush=True)


def estado_da_vm(nome: str) -> str:
    saida = subprocess.run(
        [str(VBOXMANAGE), "showvminfo", nome, "--machinereadable"],
        capture_output=True, text=True, timeout=20,
    ).stdout
    for linha in saida.splitlines():
        if linha.startswith("VMState="):
            return linha.split("=", 1)[1].strip('"')
    return "desconhecido"


def desligar(nome: str) -> tuple[bool, str]:
    """Desliga a VM na força bruta e CONFIRMA. Sem confirmação não há fencing."""
    if nome not in PERMITIDAS:
        return False, f"vm '{nome}' não está na lista permitida"

    antes = estado_da_vm(nome)
    if antes == "poweroff":
        return True, "já estava desligada"

    subprocess.run([str(VBOXMANAGE), "controlvm", nome, "poweroff"],
                   capture_output=True, text=True, timeout=30)

    # Confirmar importa mais que mandar: um comando enviado não é uma máquina morta.
    for _ in range(20):
        if estado_da_vm(nome) == "poweroff":
            return True, "desligamento confirmado"
        time.sleep(0.25)
    return False, f"não confirmou o desligamento (estado: {estado_da_vm(nome)})"


def atender(conexao: socket.socket, endereco, permitidos=None):
    conexao.settimeout(TIMEOUT_PEDIDO)
    try:
        if permitidos and endereco[0] not in permitidos:
            log(f"{endereco[0]} recusado (fora da lista permitida)")
            return

        bruto = b""
        while b"\n" not in bruto and len(bruto) < 4096:
            pedaco = conexao.recv(1024)
            if not pedaco:
                return
            bruto += pedaco

        pedido = json.loads(bruto.split(b"\n", 1)[0].decode("utf-8"))
        alvo = str(pedido.get("vm", ""))
        acao = pedido.get("acao", "desligar")
        log(f"{endereco[0]} pediu '{acao}' em '{alvo}'")

        if acao == "estado":
            # Os parênteses importam: sem eles o `if` valia só para o segundo
            # item da tupla e `ok` saía True mesmo para uma VM negada.
            ok = alvo in PERMITIDAS
            motivo = estado_da_vm(alvo) if ok else "vm fora da lista permitida"
        else:
            ok, motivo = desligar(alvo)

        log(f"  -> {'ok' if ok else 'falhou'}: {motivo}")
        conexao.sendall(
            (json.dumps({"ok": ok, "motivo": motivo}, ensure_ascii=False) + "\n").encode()
        )
    except (OSError, ValueError, subprocess.TimeoutExpired) as erro:
        log(f"  -> erro: {type(erro).__name__}: {erro}")
    finally:
        try:
            conexao.close()
        except OSError:
            pass


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Agente de fencing do jogo da forca")
    parser.add_argument("--host", default="192.168.56.1",
                        help="endereço host-only do Windows")
    parser.add_argument("--porta", type=int, default=5010)
    parser.add_argument("--ip-permitido", action="append", metavar="IP",
                        help="só aceita pedidos destes endereços; repita para "
                             "vários. Sem isto, qualquer um na rede desliga VMs.")
    args = parser.parse_args()

    if not VBOXMANAGE.exists():
        print(f"VBoxManage não encontrado em {VBOXMANAGE}")
        return 1

    ouvinte = socket.socket()
    ouvinte.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ouvinte.bind((args.host, args.porta))
    ouvinte.listen()
    permitidos = set(args.ip_permitido or [])
    log(f"ouvindo em {args.host}:{args.porta} — vms permitidas: {sorted(PERMITIDAS)}"
        + (f", origens: {sorted(permitidos)}" if permitidos else ""))

    try:
        while True:
            conexao, endereco = ouvinte.accept()
            # Uma thread por pedido. Atendendo em série, dentro do próprio laço
            # de accept, bastava UMA conexão calada para o agente parar de
            # responder — e um dispositivo de fencing que não responde é lido
            # pelo backup como "não consegui confirmar a morte", que é
            # justamente a porta de entrada do split-brain.
            threading.Thread(target=atender,
                             args=(conexao, endereco, permitidos),
                             daemon=True).start()
    except KeyboardInterrupt:
        print()
    finally:
        ouvinte.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
