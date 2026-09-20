#!/usr/bin/env bash
#
# etcd 定时备份：快照 → 校验 → 加密 → 异机留存 → 轮转
#
# ⚠️ 关键设计取舍：为什么用 systemd timer 而不是 K8s CronJob？
#
#   直觉上会想「我有 K8s 啊，写个 CronJob 不就完了」——但这是错的：
#   **etcd 备份的最大价值恰恰在集群已经坏了的时候**（etcd 数据目录损坏、
#   控制面起不来）。而集群坏了的时候，K8s CronJob 根本调度不起来，
#   连 apiserver 都连不上。用 K8s 备份 etcd = 把钥匙锁在要开的那个门里。
#   所以必须跑在宿主机上，用 systemd timer。
#
# ⚠️ 为什么加密：etcd 快照里包含**集群所有 Secret**（ServiceAccount token、
#   TLS 私钥、用户配置的密码……）。异机留存的目的地如果被攻破或者只是
#   「没那么可信」，明文快照等于把整个集群的钥匙交出去。
#
# 用法：
#   sudo /usr/local/bin/etcd-backup.sh              # 正常备份
#   sudo DEEP_VERIFY=0 /usr/local/bin/etcd-backup.sh  # 跳过恢复校验（快一点）
#   sudo /usr/local/bin/etcd-backup.sh --check      # 只检查环境，不备份
#
set -euo pipefail

# ---------------------------------------------------------------- 配置

BACKUP_DIR="${BACKUP_DIR:-/var/backups/etcd}"
KEEP_LOCAL="${KEEP_LOCAL:-14}"          # 本地保留最近多少份
KEEP_OFFHOST="${KEEP_OFFHOST:-7}"       # 异机保留最近多少份
DEEP_VERIFY="${DEEP_VERIFY:-1}"         # 是否做「真恢复一次」的深度校验
CONF_DIR="${CONF_DIR:-/etc/etcd-backup}"
OFFHOST_TARGET_FILE="${OFFHOST_TARGET_FILE:-${CONF_DIR}/offhost-target}"
SSH_KEY="${SSH_KEY:-${CONF_DIR}/id_ed25519}"
PASSPHRASE_FILE="${PASSPHRASE_FILE:-${CONF_DIR}/passphrase}"
VERIFY_DIR=/var/tmp/etcd-restore-verify

ETCD_ENDPOINTS="${ETCD_ENDPOINTS:-https://127.0.0.1:2379}"
ETCD_CACERT=/etc/kubernetes/pki/etcd/ca.crt
ETCD_CERT=/etc/kubernetes/pki/etcd/server.crt
ETCD_KEY=/etc/kubernetes/pki/etcd/server.key

# ⚠️ 日志必须写 stderr，不能写 stdout。
# 因为 take_snapshot 是用 "$(take_snapshot)" 取返回值的，
# 日志如果也走 stdout 会被一起捕获进去（踩过一次：校验拿到的是整行日志而不是文件路径）。
log()  { echo "[$(date -Is)] $*" >&2; }
warn() { echo "[$(date -Is)] WARN: $*" >&2; }
die()  { echo "[$(date -Is)] ERROR: $*" >&2; exit 1; }

# ---------------------------------------------------------------- 前置检查

need_root() { [ "$(id -u)" -eq 0 ] || die "请用 sudo 运行"; }

check_env() {
  for c in etcdctl etcdutl gpg; do
    command -v "$c" >/dev/null || die "缺少命令: $c"
  done
  for f in "$ETCD_CACERT" "$ETCD_CERT" "$ETCD_KEY"; do
    [ -r "$f" ] || die "读不到证书: $f"
  done
  etcdctl --endpoints="$ETCD_ENDPOINTS" \
    --cacert="$ETCD_CACERT" --cert="$ETCD_CERT" --key="$ETCD_KEY" \
    endpoint health >/dev/null 2>&1 || die "etcd 不可达或证书不对: $ETCD_ENDPOINTS"
  mkdir -p "$BACKUP_DIR"
  chmod 700 "$BACKUP_DIR"
}

etcdctl_() {
  etcdctl --endpoints="$ETCD_ENDPOINTS" \
    --cacert="$ETCD_CACERT" --cert="$ETCD_CERT" --key="$ETCD_KEY" "$@"
}

# ---------------------------------------------------------------- 1. 快照

take_snapshot() {
  local ts snap
  ts=$(date +%Y%m%d-%H%M%S)
  snap="${BACKUP_DIR}/etcd-${ts}.db"
  log "采集快照 -> ${snap}"
  # 末尾的 >&2 同样是为了别污染 stdout（这个函数的 stdout 只有那一行路径）
  etcdctl_ snapshot save "$snap" 2>&1 | tail -2 >&2
  [ -s "$snap" ] || die "快照文件为空"
  chmod 600 "$snap"
  echo "$snap"
}

# ---------------------------------------------------------------- 2. 校验

# 不校验的备份等于没有备份。分两层：
#   - snapshot status：确认文件能被解析（便宜）
#   - 真恢复一次到临时目录：确认**真的能恢复**（贵，但这是唯一可信的证明）
verify_snapshot() {
  local snap="$1"

  log "校验快照元数据"
  etcdutl snapshot status "$snap" --write-out=table

  if [ "$DEEP_VERIFY" != "1" ]; then
    warn "DEEP_VERIFY != 1，跳过恢复校验"
    return 0
  fi

  log "深度校验：真恢复一次到临时目录"
  rm -rf "$VERIFY_DIR"
  # 恢复参数必须和 etcd 实际成员信息对齐。这里为了校验可以用占位值，
  # 因为只验证「快照里的数据能不能重建出可用的 etcd 数据目录」。
  if etcdutl snapshot restore "$snap" \
      --name=verify --initial-cluster=verify=http://127.0.0.1:2380 \
      --initial-advertise-peer-urls=http://127.0.0.1:2380 \
      --data-dir="$VERIFY_DIR" >/dev/null 2>&1; then
    [ -f "$VERIFY_DIR/member/snap/db" ] || die "恢复后没有 member/snap/db，快照不可用"
    log "  恢复校验通过（$(du -sh "$VERIFY_DIR" | cut -f1)）"
    rm -rf "$VERIFY_DIR"
  else
    rm -rf "$VERIFY_DIR"
    die "快照恢复校验失败 —— 这份备份不可用！"
  fi
}

# ---------------------------------------------------------------- 3. 异机留存

offhost_push() {
  local snap="$1"

  if [ ! -f "$OFFHOST_TARGET_FILE" ]; then
    warn "未配置异机留存（${OFFHOST_TARGET_FILE} 不存在），跳过"
    warn "  -> 当前备份和 etcd 在同一块盘上，机器挂了备份一起没"
    return 0
  fi

  local target enc remote_dir
  target="$(cat "$OFFHOST_TARGET_FILE")"
  remote_dir="$(echo "$target" | sed 's/^[^:]*://')"
  enc="${snap}.gpg"

  log "加密快照（etcd 快照含全部 Secret，必须加密后才能离机）"
  gpg --batch --yes --symmetric --cipher-algo AES256 \
      --passphrase-file "$PASSPHRASE_FILE" -o "$enc" "$snap"

  log "推送到异机: ${target}"
  scp -q -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 \
      -i "$SSH_KEY" "$enc" "${target}/" \
    || { warn "异机推送失败（网络？密钥？），保留本地副本，本次不算失败"; rm -f "$enc"; return 0; }

  log "远端轮转（保留最近 ${KEEP_OFFHOST} 份）"
  ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 -i "$SSH_KEY" \
      "${target%%:*}" \
      "ls -1t '${remote_dir}'/etcd-*.db.gpg 2>/dev/null | tail -n +$((KEEP_OFFHOST + 1)) | xargs -r rm -f" \
    || warn "远端轮转失败"

  rm -f "$enc"   # 本地不留加密副本，省空间
  log "异机留存完成"
}

# ---------------------------------------------------------------- 4. 轮转

rotate_local() {
  log "本地轮转（保留最近 ${KEEP_LOCAL} 份 etcd-*.db）"
  # 注意只匹配 etcd-*.db，不要误删手工放的 snapshot-before-drill.db 之类
  find "$BACKUP_DIR" -maxdepth 1 -name 'etcd-*.db' -printf '%T@ %p\n' 2>/dev/null \
    | sort -rn | tail -n +$((KEEP_LOCAL + 1)) | cut -d' ' -f2- \
    | while read -r old; do
        log "  删除过期备份: $(basename "$old")"
        rm -f "$old"
      done
}

# ---------------------------------------------------------------- main

main() {
  need_root

  if [ "${1:-}" = "--check" ]; then
    check_env
    log "环境检查通过"
    [ -f "$OFFHOST_TARGET_FILE" ] && log "异机留存: $(cat "$OFFHOST_TARGET_FILE")" \
      || warn "异机留存未配置"
    exit 0
  fi

  check_env
  local snap
  snap="$(take_snapshot)"
  verify_snapshot "$snap"
  offhost_push "$snap"
  rotate_local
  log "备份完成: $(basename "$snap")  当前本地 $(find "$BACKUP_DIR" -maxdepth 1 -name 'etcd-*.db' | wc -l) 份"
}

main "$@"
