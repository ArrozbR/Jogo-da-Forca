"""Sonda os servidores e relata qual deles atende.

Instrumento da etapa 0: abre uma conexão nova a cada ciclo, que é exatamente o
que um cliente reconectando faz, e mede quanto tempo o serviço fica sem atender
durante o failover.

Com a lista de servidores, "no ar" quer dizer "alguém da lista atende". O backup
em espera responde ao ping, mas avisa que não atende, e por isso não conta.

    py sonda.py --host 192.168.43.20,192.168.43.31
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
                    if mensagem.get("tipo") != "pong":
                        continue
                    # Servidor antigo não manda 'atendendo'; ausente conta como sim.
                    if mensagem.get("atendendo") is False:
                        return None
                    return mensagem.get("servidor") or f"{host}:{porta}"
    except (OSError, ValueError):
        return None
    return None


def interpretar(texto: str, porta_padrao: int) -> list[tuple[str, int]]:
    enderecos = []
    for parte in texto.split(","):
        parte = parte.strip()
        if not parte:
            continue
        host, separador, porta = parte.rpartition(":")
        if separador and porta.isdigit():
            enderecos.append((host, int(porta)))
        else:
            enderecos.append((parte, porta_padrao))
    return enderecos


def main():
    parser = argparse.ArgumentParser(description="Sonda os servidores do jogo da forca")
    parser.add_argument("--host", required=True,
                        help="um endereço ou vários separados por vírgula (host ou host:porta)")
    parser.add_argument("--porta", type=int, default=5000)
    parser.add_argument("--intervalo", type=float, default=0.5)
    parser.add_argument("--limite", type=float, default=1.0, help="timeout por sondagem")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    enderecos = interpretar(args.host, args.porta)
    lista = ", ".join(f"{h}:{p}" for h, p in enderecos)
    print(f"sondando {lista} a cada {args.intervalo}s — Ctrl+C encerra")
    print()

    anterior = "inicio"
    ultimo_ok = None
    quedas = []

    try:
        while True:
            atual = next((nome for nome in (sondar(h, p, args.limite)
                                            for h, p in enderecos) if nome), None)
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
        print()
        print("--- resumo ---")
        if not quedas:
            print("nenhuma troca de servidor observada")
        for de, para, fora in quedas:
            print(f"{de} -> {para}: {fora:.1f}s sem atender")


if __name__ == "__main__":
    main()
