#!/bin/sh
# Libera o IP virtual desta VM. Rode como root.
#   ./liberar_ip.sh 192.168.0.10/24 enp0s3
set -e

IP="${1:?uso: liberar_ip.sh <ip/prefixo> <interface>}"
IFACE="${2:?uso: liberar_ip.sh <ip/prefixo> <interface>}"

ip addr del "$IP" dev "$IFACE" 2>/dev/null || true
echo "liberou ${IP%%/*} de $IFACE"
