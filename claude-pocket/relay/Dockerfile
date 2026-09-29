FROM node:22-alpine
WORKDIR /app
COPY server.mjs ./
COPY public ./public
ENV PORT=8787 DATA_DIR=/data
EXPOSE 8787
CMD ["node", "server.mjs"]
