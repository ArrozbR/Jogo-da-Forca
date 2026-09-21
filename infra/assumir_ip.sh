#!/bin/sh
# Assume o IP virtual nesta VM. Rode como root.
#   ./assumir_ip.sh 192.168.0.10/24 enp0s3
set -e

IP="${1:?uso: assumir_ip.sh <ip/prefixo> <interface>}"
IFACE="${2:?uso: assumir_ip.sh <ip/prefixo> <interface>}"
NU="${IP%%/*}"

if ip -brief addr show dev "$IFACE" | grep -q "$NU"; then
    echo "$NU ja esta nesta maquina"
    exit 0
fi

ip addr add "$IP" dev "$IFACE"

# ARP gratuito: anuncia sem ter sido perguntado que o MAC dono deste IP mudou.
# Sem isto, switches e clientes continuam enviando quadros para o MAC da VM
# morta ate a entrada expirar naturalmente, o que pode levar minutos.
arping -U -c 3 -I "$IFACE" "$NU" 2>/dev/null || \
    echo "aviso: arping ausente (apt install iputils-arping) - o ARP vai demorar"

echo "assumiu $NU em $IFACE"
