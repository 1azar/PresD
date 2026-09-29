#!/bin/sh
set -eu

: "${PUBLIC_IP:?PUBLIC_IP is required}"
if ! printf '%s\n' "$PUBLIC_IP" | awk -F. '
    NF != 4 { exit 1 }
    { for (i = 1; i <= 4; i++) if ($i !~ /^[0-9]+$/ || $i > 255) exit 1 }
'; then
    echo "PUBLIC_IP must be a valid IPv4 address" >&2
    exit 1
fi

active_config=/etc/nginx/conf.d/default.conf
bootstrap_template=/etc/nginx/templates/bootstrap.conf.template
https_template=/etc/nginx/templates/https.conf.template
certificate=/etc/letsencrypt/live/${PUBLIC_IP}/fullchain.pem
private_key=/etc/letsencrypt/live/${PUBLIC_IP}/privkey.pem

render_config() {
    template="$bootstrap_template"
    if [ -s "$certificate" ] && [ -s "$private_key" ]; then
        template="$https_template"
    fi
    envsubst '${PUBLIC_IP}' < "$template" > "$active_config"
    nginx -t
}

render_config

(
    previous=""
    while sleep 60; do
        current="bootstrap"
        if [ -s "$certificate" ] && [ -s "$private_key" ]; then
            current="$(sha256sum "$certificate" | cut -d ' ' -f 1)"
        fi
        if [ "$current" != "$previous" ]; then
            if render_config; then
                nginx -s reload
                previous="$current"
            fi
        fi
    done
) &

exec nginx -g 'daemon off;'
