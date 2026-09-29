FROM nginx:1.30.5-alpine3.24

RUN apk add --no-cache gettext

COPY webapp/gateway/entrypoint.sh /entrypoint.sh
COPY webapp/gateway/bootstrap.conf.template /etc/nginx/templates/bootstrap.conf.template
COPY webapp/gateway/https.conf.template /etc/nginx/templates/https.conf.template

RUN chmod 755 /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
