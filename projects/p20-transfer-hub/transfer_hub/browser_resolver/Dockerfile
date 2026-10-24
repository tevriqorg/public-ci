FROM mcr.microsoft.com/playwright:v1.55.0-noble
WORKDIR /app
COPY browser_resolver/package.json /app/package.json
RUN npm install --omit=dev --ignore-scripts --no-audit --no-fund
COPY --chown=pwuser:pwuser browser_resolver/server.mjs /app/server.mjs
RUN chown -R pwuser:pwuser /app
USER pwuser
CMD ["node", "/app/server.mjs"]
