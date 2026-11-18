#!/bin/bash
# 每日备份：把 data/ 下的白名单内容打成 zip，放到 BACKUP_DIR，并清理 N 天前的旧包。
#
# 用法（crontab）：
#   0 17 * * * /bin/bash /home/cyn/server-management/server-management-v1/scripts/backup.sh >> /var/log/server-management-backup.log 2>&1
#
# 关键点：
# - 打包前先做一次 sqlite3 的 .backup，避免直接拷正在写入的 app.db 拿到半截文件；
#   机器上没有 sqlite3 命令时退回 cp（会有极小概率不一致，日志里会提示）。
# - 只打 official / users.json / app.db 三项，和系统的「快照目录」扫描规则一致
#   （快照目录里任何 *.zip 都会被识别为「定时备份包」，因此不要往这里塞无关的 zip）。
# - 输出目录、保留天数可用环境变量覆盖，默认对齐现网部署。
set -uo pipefail

APP_DIR="${APP_DIR:-/home/cyn/server-management/server-management-v1}"
BACKUP_DIR="${BACKUP_DIR:-/home/cyn/backup/server-management-zips}"
KEEP_DAYS="${KEEP_DAYS:-5}"
PYTHON_BIN="${PYTHON_BIN:-/home/miniforge3/bin/python}"

DATA_DIR="$APP_DIR/data"
STAMP="$(date +%Y%m%d)"
TARGET="$BACKUP_DIR/data_${STAMP}.zip"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

log "===== 开始备份 ====="
log "源目录 $DATA_DIR/"
log "输出 $TARGET"

if [ ! -d "$DATA_DIR" ]; then
    log "错误：数据目录不存在 $DATA_DIR"
    exit 1
fi

mkdir -p "$BACKUP_DIR" || { log "错误：无法创建 $BACKUP_DIR"; exit 1; }
# 打包根：保持 data/ 前缀，和系统「快照目录」的识别规则一致（zip 里要有 data/official/*.xlsx）
mkdir -p "$TMP_DIR/data"

# 1) app.db 用 sqlite3 .backup 取一致快照（必须落在 $TMP_DIR/data/ 下，否则不会被打进包）
DB_OUT="$TMP_DIR/data/app.db"
if [ -f "$DATA_DIR/app.db" ]; then
    if command -v sqlite3 >/dev/null 2>&1; then
        if sqlite3 "$DATA_DIR/app.db" ".backup '$DB_OUT'"; then
            log "app.db 已用 sqlite3 .backup 导出"
        else
            cp -f "$DATA_DIR/app.db" "$DB_OUT" && log "警告：sqlite3 .backup 失败，退回 cp"
        fi
    elif [ -x "$PYTHON_BIN" ] && "$PYTHON_BIN" - "$DATA_DIR/app.db" "$DB_OUT" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
    s.backup(d)
PY
    then
        log "app.db 已用 python sqlite3.backup 导出"
    else
        cp -f "$DATA_DIR/app.db" "$DB_OUT" && log "警告：无 sqlite3 / python 可备份，退回 cp"
    fi
    # 兜底校验：python 复制失败时可能留下空文件，宁可让整次备份失败也别发残缺包
    if [ ! -s "$DB_OUT" ]; then
        log "错误：app.db 备份结果为空，放弃打包"
        exit 1
    fi
fi

# 2) 归档正式表与用户配置
[ -d "$DATA_DIR/official" ] && cp -a "$DATA_DIR/official" "$TMP_DIR/data/official"
[ -f "$DATA_DIR/users.json" ] && cp -a "$DATA_DIR/users.json" "$TMP_DIR/data/users.json"

if [ ! -d "$TMP_DIR/data/official" ]; then
    log "错误：$DATA_DIR/official 不存在，放弃打包"
    exit 1
fi

# 3) 打包：先写临时文件再 mv，避免扫描端读到半个 zip
ZIP_TMP="$TARGET.tmp"
if ! (cd "$TMP_DIR" && zip -qr "$ZIP_TMP" data); then
    log "错误：zip 打包失败"
    rm -f "$ZIP_TMP"
    exit 1
fi
mv -f "$ZIP_TMP" "$TARGET"
log "打包完成 $(du -h "$TARGET" | cut -f1)"

# 4) 清理 KEEP_DAYS 天前的旧包（只删本脚本产出的 data_*.zip）
log "===== 清理 ${KEEP_DAYS} 天及更早的包 ====="
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'data_*.zip' -mtime "+$((KEEP_DAYS - 1))" -print -delete

log "备份结束：$(ls -1 "$BACKUP_DIR"/data_*.zip 2>/dev/null | wc -l) 个包在 $BACKUP_DIR"
