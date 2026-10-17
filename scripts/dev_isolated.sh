#!/usr/bin/env bash
# 在一次性数据库和日志目录里运行项目的 Python。
# 导入 app 模块会初始化并迁移数据库，默认路径就是正在使用的数据库，临时脚本和测试都应该走这个包装。
#
# 用法：scripts/dev_isolated.sh -m unittest discover -s tests -p 'test_*.py'
#       scripts/dev_isolated.sh -c "import app.reply_server"
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/bygoofish-isolated-XXXXXX")"
chmod 700 "$TMP"
trap 'rm -rf "$TMP"' EXIT
cd "$ROOT" || exit 1
DB_PATH="$TMP/isolated.db" APP_LOG_PATH="$TMP/app.log" SQL_LOG_ENABLED=false \
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$ROOT" "${PYTHON:-$ROOT/.venv/bin/python}" -B "$@"
