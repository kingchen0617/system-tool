#!/usr/bin/env bash
# Codespaces 啟動腳本：啟動 system-tool（不含 Ollama），並灌入幾個示範事故
set -euo pipefail
cd "$(dirname "$0")/.."

# 1. 準備 .env（Codespaces 預設關閉 LLM，改用規則式 RCA；要用 AI 請見 README）
if [ ! -f .env ]; then
  cp .env.example .env
  sed -i 's/^LLM_ENABLED=.*/LLM_ENABLED=false/' .env
fi

# 通知裡的連結指向 Codespaces 的公開網址
if [ -n "${CODESPACE_NAME:-}" ]; then
  url="https://${CODESPACE_NAME}-8000.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}"
  sed -i "s#^PUBLIC_URL=.*#PUBLIC_URL=${url}#" .env
fi

# 2. 等 Docker 就緒
for _ in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 2; done

# 3. 啟動服務（不啟動 ollama，避免 Codespace 資源不足）
docker compose up -d --build rca-engine prometheus loki grafana

# 4. 等 API 起來
echo "等待 system-tool 啟動..."
for _ in $(seq 1 60); do
  curl -fs localhost:8000/health >/dev/null 2>&1 && break
  sleep 2
done

# 5. 第一次啟動時灌入示範事故，打開首頁就有資料可看
if ! curl -fs localhost:8000/incidents | grep -q '"id"'; then
  for s in provider-timeout db-latency redis-latency deployment-regression multi-root-redis-db; do
    curl -fs -X POST localhost:8000/test/incident \
      -H 'Content-Type: application/json' \
      -d "{\"scenario\":\"${s}\",\"notify\":false}" >/dev/null || echo "（略過 ${s}）"
  done
fi

echo ""
echo "✅ system-tool 已啟動！請到下方「連接埠 (Ports)」分頁打開 8000。"
