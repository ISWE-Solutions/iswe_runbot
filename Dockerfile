# --- Tailwind CSS (standalone CLI, no Node) ---
FROM python:3.12-slim AS css
ARG TARGETARCH
RUN python -c "import os,urllib.request; a={'amd64':'x64','arm64':'arm64'}[os.environ['TARGETARCH']]; urllib.request.urlretrieve(f'https://github.com/tailwindlabs/tailwindcss/releases/download/v3.4.17/tailwindcss-linux-{a}', '/usr/local/bin/tailwindcss')" \
    && chmod +x /usr/local/bin/tailwindcss
WORKDIR /src
COPY tailwind.config.js ./
COPY previewer/web ./previewer/web
RUN tailwindcss -c tailwind.config.js -i previewer/web/static/src.css -o /out/app.css --minify

# --- App + worker (same image, different command) ---
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Outside previewer/ so the dev bind mount of the source does not hide it
COPY --from=css /out/app.css /app/build/app.css
ENV PYTHONUNBUFFERED=1 CSS_PATH=/app/build/app.css
CMD ["sh", "scripts/start-app.sh"]
