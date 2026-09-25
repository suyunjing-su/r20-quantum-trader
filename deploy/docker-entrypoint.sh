#!/usr/bin/env bash
set -e

# ==============================================================================
# R20 Quantum Trader - Container Entrypoint
# ==============================================================================

ROOT_DIR="/app"
cd "$ROOT_DIR"
MODE="${1:-backend}"

# 1. 确保必要运行时目录存在
mkdir -p "$ROOT_DIR/data" "$ROOT_DIR/logs" "$ROOT_DIR/backups"

# 2. 如果缺少 .env，从 env.example 自动生成一份最小兜底（提醒用户尽快配置）
if [ -d "$ROOT_DIR/.env" ]; then
    echo "⚠️ [Entrypoint] Warning: /app/.env is mounted as a directory! Please mount a file instead."
elif [ ! -f "$ROOT_DIR/.env" ] && [ -f "$ROOT_DIR/env.example" ]; then
    echo "⚠️ [Entrypoint] .env not found. Generating default .env from env.example..."
    cp "$ROOT_DIR/env.example" "$ROOT_DIR/.env"
    chmod 600 "$ROOT_DIR/.env"
fi

# 2.5 双容器部署兜底：.env 是配置唯一事实源（SSOT），其空值键也会覆盖 compose 注入
# 的环境变量。gateway 模式下若 R20_STANDALONE_GATEWAY 在 .env 中留空，backend 容器
# 会重复拉起网关 worker 抢走共享卷 flock → 本容器抢锁失败秒退、无限重启。
if [ "$MODE" = "gateway" ] || [ "$MODE" = "worker" ]; then
    if [ -f "$ROOT_DIR/.env" ] && grep -qE '^[[:space:]]*R20_STANDALONE_GATEWAY[[:space:]]*=[[:space:]]*$' "$ROOT_DIR/.env"; then
        echo "🔧 [Entrypoint] Enforcing R20_STANDALONE_GATEWAY=true in .env (SSOT override guard)..."
        sed -i 's/^[[:space:]]*R20_STANDALONE_GATEWAY[[:space:]]*=.*/R20_STANDALONE_GATEWAY=true/' "$ROOT_DIR/.env" || true
    fi
fi

# 3. 初始化默认标的池（如果不存在，避免冷启动阻断）
if [ ! -f "$ROOT_DIR/data/instrument_pool.json" ]; then
    echo "📋 [Entrypoint] Initializing default instrument pool in data/..."
    python3 -c "from scripts.instrument_pool import save_instruments, DEFAULT_INSTRUMENTS; save_instruments(DEFAULT_INSTRUMENTS)" 2>/dev/null || true
fi

case "$MODE" in
    backend|web)
        echo "✨ [R20] Starting Web Engine & Control Plane on 0.0.0.0:8080..."
        exec python3 -m uvicorn r20_backend.app:app --host 0.0.0.0 --port 8080
        ;;
    gateway|worker)
        echo "🚀 [R20] Starting Quantitative Gateway & Dispatch Worker..."
        exec python3 -m r20_gateway.worker
        ;;
    all)
        echo "✨ [R20] Starting All-in-One Mode (Web + Gateway Worker)..."
        python3 -m uvicorn r20_backend.app:app --host 0.0.0.0 --port 8080 &
        BACKEND_PID=$!
        python3 -m r20_gateway.worker &
        GATEWAY_PID=$!

        trap 'echo "🛑 Stopping services..."; kill -TERM $BACKEND_PID $GATEWAY_PID 2>/dev/null' TERM INT
        wait -n $BACKEND_PID $GATEWAY_PID
        EXIT_CODE=$?
        kill -TERM $BACKEND_PID $GATEWAY_PID 2>/dev/null || true
        exit $EXIT_CODE
        ;;
    *)
        exec "$@"
        ;;
esac
