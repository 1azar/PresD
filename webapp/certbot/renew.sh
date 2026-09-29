#!/bin/sh
set -eu

: "${PUBLIC_IP:?PUBLIC_IP is required}"
: "${TLS_EMAIL:?TLS_EMAIL is required}"
if ! printf '%s\n' "$PUBLIC_IP" | awk -F. '
    NF != 4 { exit 1 }
    { for (i = 1; i <= 4; i++) if ($i !~ /^[0-9]+$/ || $i > 255) exit 1 }
'; then
    echo "PUBLIC_IP must be a valid IPv4 address" >&2
    exit 1
fi

trap 'exit 0' TERM INT

while :; do
    if certbot certonly --non-interactive --agree-tos \
        --email "$TLS_EMAIL" \
        --preferred-profile shortlived \
        --webroot --webroot-path /var/www/certbot \
        --ip-address "$PUBLIC_IP" --cert-name "$PUBLIC_IP" \
        --keep-until-expiring; then
        delay=43200
    else
        delay=300
    fi
    sleep "$delay" &
    wait $!
done
