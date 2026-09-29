FROM node:22-alpine AS build
WORKDIR /src
COPY webapp/frontend/package*.json ./
RUN npm install
COPY webapp/frontend/ ./
RUN npm run build

FROM nginx:1.30.5-alpine3.24
COPY webapp/nginx/default.conf /etc/nginx/conf.d/default.conf
COPY --from=build /src/dist /usr/share/nginx/html
