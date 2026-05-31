#!/bin/bash
# Bootstrap a fresh Ubuntu VPS as the public reverse proxy for the home media stack.
# Installs WireGuard + Caddy + fail2ban + unattended-upgrades, writes configs, and
# brings the tunnel up. Idempotent — safe to re-run.
#
# Usage:
#   sudo HOME_WG_PUBKEY="<key>" DOMAIN="example.com" ./setup-vps.sh
#
# Optional overrides:
#   JELLYFIN_PORT     (default 8096)
#   JELLYSEERR_PORT   (default 5055)
#   WG_SUBNET_PREFIX  (default 10.10.10)  — VPS uses .1, home uses .2
#   WG_PORT           (default 51820)

set -euo pipefail

[ "$EUID" -eq 0 ] || { echo "Run as root (sudo)." >&2; exit 1; }

: "${HOME_WG_PUBKEY:?Required: HOME_WG_PUBKEY (run setup-home-wg.sh on the home server first to generate one)}"
: "${DOMAIN:?Required: DOMAIN (e.g. example.com — must have A records pointing here for @ and request.)}"

JELLYFIN_PORT="${JELLYFIN_PORT:-8096}"
JELLYSEERR_PORT="${JELLYSEERR_PORT:-5055}"
WG_SUBNET_PREFIX="${WG_SUBNET_PREFIX:-10.10.10}"
WG_PORT="${WG_PORT:-51820}"

echo "==> Installing packages..."
apt update
DEBIAN_FRONTEND=noninteractive apt install -y \
    wireguard ufw curl gnupg \
    debian-keyring debian-archive-keyring apt-transport-https \
    unattended-upgrades fail2ban

echo "==> Configuring firewall..."
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow "${WG_PORT}/udp"
ufw --force enable

echo "==> Enabling IP forwarding..."
echo 'net.ipv4.ip_forward=1' > /etc/sysctl.d/99-wireguard.conf
sysctl -p /etc/sysctl.d/99-wireguard.conf >/dev/null

echo "==> Setting up WireGuard..."
mkdir -p /etc/wireguard
chmod 700 /etc/wireguard
cd /etc/wireguard
if [ ! -f vps_private.key ]; then
    umask 077
    wg genkey | tee vps_private.key | wg pubkey > vps_public.key
fi

cat > /etc/wireguard/wg0.conf <<EOF
[Interface]
PrivateKey = $(cat /etc/wireguard/vps_private.key)
Address = ${WG_SUBNET_PREFIX}.1/24
ListenPort = ${WG_PORT}

[Peer]
# Home server
PublicKey = ${HOME_WG_PUBKEY}
AllowedIPs = ${WG_SUBNET_PREFIX}.2/32
EOF
chmod 600 /etc/wireguard/wg0.conf
systemctl enable wg-quick@wg0 >/dev/null 2>&1 || true
systemctl restart wg-quick@wg0

echo "==> Installing Caddy..."
if ! command -v caddy >/dev/null; then
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        -o /etc/apt/sources.list.d/caddy-stable.list
    apt update
    apt install -y caddy
fi

echo "==> Writing Caddyfile..."
cat > /etc/caddy/Caddyfile <<EOF
${DOMAIN} {
	reverse_proxy ${WG_SUBNET_PREFIX}.2:${JELLYFIN_PORT}
}

request.${DOMAIN} {
	reverse_proxy ${WG_SUBNET_PREFIX}.2:${JELLYSEERR_PORT}
}
EOF
caddy validate --config /etc/caddy/Caddyfile
systemctl enable --now caddy >/dev/null 2>&1 || true
systemctl reload caddy

echo "==> Enabling auto-updates and fail2ban..."
systemctl enable --now unattended-upgrades fail2ban

echo
echo "==> Done."
echo
echo "VPS WireGuard public key (give this to setup-home-wg.sh as VPS_WG_PUBKEY):"
cat /etc/wireguard/vps_public.key
echo
echo "VPS public IP (use as VPS_IP on the home side):"
curl -s -4 ifconfig.me; echo
echo
echo "Don't forget DNS:"
echo "  ${DOMAIN}          A  -> <this VPS's public IP>"
echo "  request.${DOMAIN}  A  -> <this VPS's public IP>"
