#!/bin/bash
# Set up the home-server side of the WireGuard tunnel to the public VPS reverse proxy.
# Idempotent — safe to re-run.
#
# Two-phase usage:
#   Phase 1 (no env vars):  generates keys, prints the public key, exits.
#       sudo ./setup-home-wg.sh
#
#   Phase 2 (after running setup-vps.sh):  writes config and starts tunnel.
#       sudo VPS_IP="1.2.3.4" VPS_WG_PUBKEY="<key>" ./setup-home-wg.sh
#
# Optional overrides:
#   WG_SUBNET_PREFIX  (default 10.10.10)
#   WG_PORT           (default 51820)

set -euo pipefail

[ "$EUID" -eq 0 ] || { echo "Run as root (sudo)." >&2; exit 1; }

WG_SUBNET_PREFIX="${WG_SUBNET_PREFIX:-10.10.10}"
WG_PORT="${WG_PORT:-51820}"

echo "==> Installing wireguard-tools..."
if command -v pacman >/dev/null; then
    pacman -S --needed --noconfirm wireguard-tools
elif command -v apt >/dev/null; then
    apt update && DEBIAN_FRONTEND=noninteractive apt install -y wireguard
elif command -v dnf >/dev/null; then
    dnf install -y wireguard-tools
else
    echo "Unsupported distro — install wireguard-tools manually." >&2
    exit 1
fi

echo "==> Generating WireGuard keys (if missing)..."
mkdir -p /etc/wireguard
chmod 700 /etc/wireguard
cd /etc/wireguard
if [ ! -f home_private.key ]; then
    umask 077
    wg genkey | tee home_private.key | wg pubkey > home_public.key
fi

if [ -z "${VPS_IP:-}" ] || [ -z "${VPS_WG_PUBKEY:-}" ]; then
    echo
    echo "==> Phase 1 complete — keys ready."
    echo
    echo "Home WireGuard public key (pass to setup-vps.sh as HOME_WG_PUBKEY):"
    cat /etc/wireguard/home_public.key
    echo
    echo "Now run setup-vps.sh on your VPS, then come back and re-run this script with:"
    echo "  sudo VPS_IP=<vps-public-ip> VPS_WG_PUBKEY=<vps-public-key> $0"
    exit 0
fi

echo "==> Writing wg0.conf..."
cat > /etc/wireguard/wg0.conf <<EOF
[Interface]
PrivateKey = $(cat /etc/wireguard/home_private.key)
Address = ${WG_SUBNET_PREFIX}.2/24

[Peer]
# VPS reverse proxy
PublicKey = ${VPS_WG_PUBKEY}
Endpoint = ${VPS_IP}:${WG_PORT}
AllowedIPs = ${WG_SUBNET_PREFIX}.1/32
PersistentKeepalive = 25
EOF
chmod 600 /etc/wireguard/wg0.conf

systemctl enable wg-quick@wg0 >/dev/null 2>&1 || true
systemctl restart wg-quick@wg0
sleep 2

echo
echo "==> Done. Tunnel status:"
wg show
echo
echo "Verify from the VPS:  ping -c 3 ${WG_SUBNET_PREFIX}.2"
