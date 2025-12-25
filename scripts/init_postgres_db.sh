#!/bin/bash
# PostgreSQL数据库初始化脚本
# 用于Atlas项目的PostgreSQL数据库和用户创建

set -e  # 遇到错误立即退出

echo "=========================================="
echo "Atlas PostgreSQL 数据库初始化"
echo "=========================================="
echo ""

# 检查是否以postgres用户运行
if [ "$USER" != "postgres" ]; then
    echo "⚠️  请使用postgres用户运行此脚本"
    echo "   sudo -u postgres bash $0"
    echo ""
    exit 1
fi

# 数据库配置（从环境变量读取，使用默认值）
DB_NAME="${ATLAS_POSTGRES_DB:-atlas_db}"
DB_USER="${ATLAS_POSTGRES_USER:-atlas_user}"
DB_PASSWORD="${ATLAS_POSTGRES_PASSWORD:-atlas_password}"

echo "📋 数据库配置："
echo "   数据库名称: $DB_NAME"
echo "   用户名称: $DB_USER"
echo "   密码: **** (已设置)"
echo ""

# 确认继续
read -p "确认创建？(y/N): " -n 1 -r
echo ""
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "❌ 操作已取消"
    exit 1
fi

# 1. 创建用户（如果不存在）
echo "🔧 创建数据库用户..."
psql -c "DO \$\$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = '$DB_USER') THEN
        CREATE USER $DB_USER WITH ENCRYPTED PASSWORD '$DB_PASSWORD';
    END IF;
END
\$\$;" 2>/dev/null || echo "⚠️  用户可能已存在"

# 2. 创建数据库（如果不存在）
echo "🔧 创建数据库..."
psql -c "SELECT 'CREATE DATABASE $DB_NAME OWNER $DB_USER'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '$DB_NAME')\\gexec" 2>/dev/null

# 3. 授予权限
echo "🔧 授予用户权限..."
psql -c "GRANT ALL PRIVILEGES ON DATABASE $DB_NAME TO $DB_USER;" 2>/dev/null

# 4. 创建扩展（可选）
echo "🔧 创建数据库扩展..."
psql -d $DB_NAME -c "CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\";" 2>/dev/null || true
psql -d $DB_NAME -c "CREATE EXTENSION IF NOT EXISTS \"pg_trgm\";" 2>/dev/null || true

# 5. 验证
echo ""
echo "✅ 验证创建结果..."
echo ""
echo "数据库列表:"
psql -c "\l" | grep -E "($DB_NAME|Name)" || true

echo ""
echo "用户列表:"
psql -c "\du" | grep -E "($DB_USER|User name)" || true

echo ""
echo "=========================================="
echo "✅ 数据库初始化完成！"
echo "=========================================="
echo ""
echo "📝 请在.env.local中配置以下环境变量："
echo "   ATLAS_DATABASE_TYPE=postgresql"
echo "   ATLAS_DATABASE_HOST=localhost"
echo "   ATLAS_DATABASE_PORT=5432"
echo "   ATLAS_DATABASE_USER=$DB_USER"
echo "   ATLAS_DATABASE_PASSWORD=$DB_PASSWORD"
echo "   ATLAS_DATABASE_NAME=$DB_NAME"
echo ""
echo "测试连接："
echo "  PGPASSWORD=$DB_PASSWORD psql -h localhost -U $DB_USER -d $DB_NAME"
echo ""
