# etcd 定时备份

etcd 是 kubeadm 集群唯一的状态存储。它丢了，整个集群的状态就没了（所有 Deployment、
Service、Secret、RBAC……）。这个目录是它的备份方案。

报告：[`docs/postmortems/2026-09-21-etcd-snapshot-restore.md`](../../docs/postmortems/2026-09-21-etcd-snapshot-restore.md)
（含一次真实恢复演练，实测不可用约 90 秒）

## ⭐ 两个关键设计取舍

### 一、为什么用 systemd timer 而不是 K8s CronJob？

直觉上会想：「我有 K8s 啊，写个 CronJob 不就完了？」

**这是错的。** etcd 备份的最大价值恰恰在于**集群已经坏了的时候**（etcd 数据目录损坏、
控制面起不来）。而集群坏了的时候，K8s CronJob 根本调度不起来，连 apiserver 都连不上。

**用 K8s 备份 etcd = 把钥匙锁在要开的那个门里。**

所以备份必须跑在宿主机上，用 systemd timer。这也是为什么这一整套东西放在
`ops/` 而不是 `k8s/`。

### 二、为什么必须加密才能离机？

etcd 快照里包含**集群所有的 Secret**：ServiceAccount token、TLS 私钥、
用户配置的各种密码。所以：

- 异机留存的目的地必须**私有**（绝不能推到公开仓库）
- 传输/存放前必须**加密**（目的地被攻破时明文快照等于交出整个集群）

这里用 gpg 对称加密，口令存在 `/etc/etcd-backup/passphrase`（0600，仅 root 可读）。

> **更理想的做法是非对称加密**：备份机只持公钥，私钥由运维本人离线保管。
> 这样即使备份机被完全攻破，攻击者也解不开历史备份。
> 当前用对称是为了减少「私钥该放哪」的运维负担，属于务实的折中。

## 备份链路

```
快照 (etcdctl snapshot save)
  ↓
校验元数据 (etcdutl snapshot status)
  ↓
⭐ 深度校验：真恢复一次到临时目录 (etcdutl snapshot restore)
  ↓
加密 (gpg AES256)
  ↓
推送异机 (scp) + 远端轮转
  ↓
本地轮转（保留最近 14 份）
```

### 「深度校验」为什么值得做

只跑 `snapshot status` 只能证明**文件能被解析**，不能证明**真的能恢复**。
有些损坏（比如 WAL 不一致）在 status 阶段看不出来，真要恢复才发现。

所以脚本每次都会把快照 `restore` 到 `/var/tmp/etcd-restore-verify`，
确认能重建出可用的 `member/snap/db`，然后删掉。多花十几秒，换来「这份备份一定能用」。

## 安装

```bash
sudo install -m 700 etcd-backup.sh /usr/local/bin/etcd-backup.sh
sudo install -m 644 etcd-backup.service /etc/systemd/system/
sudo install -m 644 etcd-backup.timer  /etc/systemd/system/

# 配置目录：SSH 密钥 + 加密口令
sudo install -d -m 700 /etc/etcd-backup
sudo ssh-keygen -t ed25519 -N "" -C "etcd-backup@<节点名>" -f /etc/etcd-backup/id_ed25519
openssl rand -base64 32 | sudo tee /etc/etcd-backup/passphrase >/dev/null
sudo chmod 600 /etc/etcd-backup/passphrase

sudo systemctl daemon-reload
sudo systemctl enable --now etcd-backup.timer
```

**前置依赖**（kubeadm 不带）：

```bash
curl -fsSL -o /tmp/etcd.tgz \
  "https://github.com/etcd-io/etcd/releases/download/v<版本>/etcd-v<版本>-linux-<arch>.tar.gz"
tar xzf /tmp/etcd.tgz -C /tmp
sudo install -m 755 /tmp/etcd-v<版本>-linux-<arch>/{etcdctl,etcdutl} /usr/local/bin/
```

> ⚠️ **etcd 3.5+ 把离线操作拆到了 `etcdutl`**：`snapshot status` / `snapshot restore`
> 已经不在 `etcdctl` 里了（`etcdctl` 只剩需要连服务端的 `snapshot save`）。
> 用 `etcdctl` 跑会只打印 help，很容易误以为是自己参数写错了。

版本要和实际运行的 etcd 对齐：
```bash
kubectl -n kube-system get pod etcd-<节点名> -o jsonpath="{.spec.containers[0].image}"
```

## 配置异机留存

异机留存是**可选**的：没配 `offhost-target` 时脚本会打 WARN 并跳过，
不会让整个备份失败（优雅降级）。

```bash
# 1. 把公钥加到目标机
sudo cat /etc/etcd-backup/id_ed25519.pub
#    -> 追加到目标机的 ~/.ssh/authorized_keys

# 2. 在目标机建目录
#    ssh <target> 'mkdir -p ~/etcd-backups && chmod 700 ~/etcd-backups'

# 3. 写上目标（格式：user@host:/绝对路径）
echo 'ubuntu@<目标机IP>:/home/ubuntu/etcd-backups' | sudo tee /etc/etcd-backup/offhost-target
sudo chmod 600 /etc/etcd-backup/offhost-target

# 4. 验证
sudo /usr/local/bin/etcd-backup.sh
```

**为什么选同一云厂商的另一台机器 / 另一家云的另一台机器**：
「异机」的意义是**不共享故障域**。同机房同块物理盘上的两份备份，
在一次磁盘故障里会一起没。理想是跨可用区甚至跨云。

## 日常操作

```bash
# 环境检查（不备份）
sudo /usr/local/bin/etcd-backup.sh --check

# 手动跑一次
sudo /usr/local/bin/etcd-backup.sh

# 跳过深度校验（快一点）
sudo DEEP_VERIFY=0 /usr/local/bin/etcd-backup.sh

# 看定时器
systemctl list-timers etcd-backup.timer

# 看历史执行
journalctl -u etcd-backup.service --since "7 days ago"

# 手动触发一次
sudo systemctl start etcd-backup.service
```

## 保留策略

| 位置 | 份数 | 说明 |
|---|---|---|
| 本地 `/var/backups/etcd` | 14 | 一份约 31 MB，共约 430 MB |
| 异机 | 7 | 只有加密副本 |

> 只有 14 份是**按份数**轮转，不是按天数。如果哪天改成更频繁的备份
> （比如每小时），要重新算一下保留份数和磁盘占用。

## 恢复

完整步骤见 Postmortem 报告。要点：

1. **先备份 manifest**：`sudo cp /etc/kubernetes/manifests/*.yaml /root/kubeadm-manifest-backup/`
2. **停控制面**：移走 `/etc/kubernetes/manifests/{etcd,kube-apiserver,kube-controller-manager,kube-scheduler}.yaml`
3. **数据目录只挪不删**：`sudo mv /var/lib/etcd /var/lib/etcd.corrupted-$(date +%s)`
4. **恢复参数逐字对齐**原 manifest（`--name` / `--initial-cluster` / `--initial-advertise-peer-urls`）
5. 放回 manifest，等 kubelet 重建静态 Pod

加密副本要先解密：
```bash
gpg --batch --decrypt --passphrase-file /etc/etcd-backup/passphrase \
    -o /tmp/restore.db etcd-YYYYmmdd-HHMMSS.db.gpg
```
