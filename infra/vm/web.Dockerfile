# Frontend image: build apps/web (Vite) -> serve the static dist via nginx,
# which also reverse-proxies the API + WebSocket (single origin, no CORS).
# Build context = REPO ROOT:
#   docker build -f infra/vm/web.Dockerfile -t zagent-web:0.1.0 .
FROM node:22-alpine AS build
WORKDIR /src
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci
COPY apps/web ./
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /src/dist /usr/share/nginx/html
COPY infra/vm/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
