#!/usr/bin/env bash
set -e

# ==============================================================================
# R20 Quantum Trader - Docker One-click Launcher
# ==============================================================================

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "🐳 [R20 Docker Launcher] Pre-flight checks..."

# 1. 确保运行时挂载目录存在
mkdir -p "$ROOT_DIR/data" "$ROOT_DIR/logs" "$ROOT_DIR/backups"

# 2. 检查 .env 配置文件（防范 docker mount 把 .env 误当作目录创建）
if [ -d "$ROOT_DIR/.env" ]; then
    echo "⚠️ Warning: .env was found as a directory. Correcting..."
    rm -rf "$ROOT_DIR/.env"
fi

if [ ! -f "$ROOT_DIR/.env" ]; then
    if [ -f "$ROOT_DIR/env.example" ]; then
        echo "📝 Creating initial .env from env.example..."
        cp "$ROOT_DIR/env.example" "$ROOT_DIR/.env"
        chmod 600 "$ROOT_DIR/.env"
        echo "⚠️ Note: Default .env created. Please configure your API keys if needed."
    else
        echo "❌ Error: Neither .env nor env.example found."
        exit 1
    fi
else
    chmod 600 "$ROOT_DIR/.env"
fi

# 2.5 双容器部署硬前提：.env 是配置唯一事实源（SSOT），其值会覆盖 compose 注入的
# R20_STANDALONE_GATEWAY。若该键留空，backend 容器会在 lifespan 里重复拉起网关
# worker，抢走共享卷上的 flock → gateway 容器抢锁失败秒退，陷入 exit 0 重启循环。
if grep -qE '^[[:space:]]*R20_STANDALONE_GATEWAY[[:space:]]*=[[:space:]]*$' "$ROOT_DIR/.env"; then
    echo "🔧 Enforcing R20_STANDALONE_GATEWAY=true for compose dual-container deployment..."
    sed -i 's/^[[:space:]]*R20_STANDALONE_GATEWAY[[:space:]]*=.*/R20_STANDALONE_GATEWAY=true/' "$ROOT_DIR/.env"
fi

# 3. 检查 Docker 与 Docker Compose 命令
if command -v docker &> /dev/null && docker compose version &> /dev/null; then
    COMPOSE_CMD="docker compose"
elif command -v docker-compose &> /dev/null; then
    COMPOSE_CMD="docker-compose"
else
    echo "❌ Error: Neither 'docker compose' nor 'docker-compose' is installed."
    echo "Please install Docker and Docker Compose plugin first."
    exit 1
fi

echo "🚀 Building and starting R20 Quantum Trader stack..."
$COMPOSE_CMD up -d --build

echo "✅ R20 Docker Stack successfully launched!"
echo "--------------------------------------------------------"
echo "🖥️  Web Dashboard:  http://localhost:8080"
echo "⚙️  Admin Console:  http://localhost:8080/admin/login"
echo "📜 View Logs:      $COMPOSE_CMD logs -f"
echo "🛑 Stop Stack:     $COMPOSE_CMD down"
echo "--------------------------------------------------------"
