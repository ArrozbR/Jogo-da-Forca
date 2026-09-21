"""Arma a injeção de falha no servidor: ele morre no próximo lance, depois de
replicar e antes de confirmar ao cliente.

Essa janela dura fração de milissegundo. Não existe como acertá-la puxando um
cabo — então o servidor a cronometra por nós, e o teste passa a ser repetível.

    python infra/armar_falha.py --host 192.168.56.10
"""
import argparse
import json
import socket
import sys


def main():
    parser = argparse.ArgumentParser(description="Arma a injeção de falha no servidor")
    parser.add_argument("--host", default="192.168.56.10")
    parser.add_argument("--porta", type=int, default=5000)
    args = parser.parse_args()

    pedido = json.dumps({"tipo": "crash_apos_replicar"}) + "\n"
    try:
        with socket.create_connection((args.host, args.porta), timeout=6) as s:
            s.sendall(pedido.encode("utf-8"))
            s.settimeout(3)
            try:
                resposta = s.recv(4096).decode("utf-8", "replace").strip()
            except socket.timeout:
                resposta = "(sem resposta)"
    except OSError as erro:
        print(f"não foi possível falar com {args.host}:{args.porta} — {erro}")
        return 1

    print(f"falha armada em {args.host}:{args.porta}")
    print(f"resposta: {resposta}")
    print("o próximo chute de qualquer jogador derruba este servidor")
    return 0


if __name__ == "__main__":
    sys.exit(main())
