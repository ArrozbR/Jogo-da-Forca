"""Sonda o IP virtual e relata qual servidor atende.

Instrumento da etapa 0: abre uma conexão nova a cada ciclo, que é exatamente o
que um cliente reconectando faz, e mede quanto tempo o serviço fica sem atender
durante o takeover.

    python sonda.py --host 192.168.0.10
"""

import argparse
import socket
import sys
import time

from protocolo import Enquadrador, empacotar


def sondar(host: str, porta: int, limite: float) -> str | None:
    try:
        with socket.create_connection((host, porta), timeout=limite) as sock:
            sock.sendall(empacotar({"tipo": "ping"}))
            enquadrador = Enquadrador()
            fim = time.monotonic() + limite
            while time.monotonic() < fim:
                sock.settimeout(max(0.05, fim - time.monotonic()))
                dados = sock.recv(4096)
                if not dados:
                    return None
                for mensagem in enquadrador.alimentar(dados):
                    if mensagem.get("servidor"):
                        return mensagem["servidor"]
    except (OSError, ValueError):
        return None
    return None


def main():
    parser = argparse.ArgumentParser(description="Sonda o IP virtual do jogo da forca")
    parser.add_argument("--host", required=True, help="IP virtual (ex: 192.168.0.10)")
    parser.add_argument("--porta", type=int, default=5000)
    parser.add_argument("--intervalo", type=float, default=0.5)
    parser.add_argument("--limite", type=float, default=1.0, help="timeout por sondagem")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(f"sondando {args.host}:{args.porta} a cada {args.intervalo}s — Ctrl+C encerra\n")

    anterior = "inicio"
    ultimo_ok = None
    quedas = []

    try:
        while True:
            atual = sondar(args.host, args.porta, args.limite)
            agora = time.monotonic()
            etiqueta = atual or "SEM RESPOSTA"

            if etiqueta != anterior:
                marca = ""
                if atual and ultimo_ok is not None:
                    fora = agora - ultimo_ok
                    quedas.append((anterior, atual, fora))
                    marca = f"  (indisponível por {fora:.1f}s)"
                print(f"[{time.strftime('%H:%M:%S')}] {anterior} -> {etiqueta}{marca}")
                anterior = etiqueta

            if atual:
                ultimo_ok = agora

            time.sleep(args.intervalo)
    except KeyboardInterrupt:
        print("\n--- resumo ---")
        if not quedas:
            print("nenhuma troca de servidor observada")
        for de, para, fora in quedas:
            print(f"{de} -> {para}: {fora:.1f}s sem atender")


if __name__ == "__main__":
    main()
