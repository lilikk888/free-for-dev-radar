---
# PostgreSQL 备份的恢复手册。
#
# 为什么单独写这个文档：
#   **没有验证过恢复流程的备份，等于没有备份。**
#   备份的价值不在"每天生成一个文件"，而在于"出事时真的能救回来"。
#   所以这里把恢复步骤写清楚，并且要求定期实际演练一次。
#
# 备份位置：PVC `radar-backup`（挂在集群节点上），文件形如
#   /backups/radar-20261002-0310.dump
# 保留策略：最近 7 份，每天 03:10 由 CronJob 自动执行

# 一、看有哪些备份

```bash
# 起一个临时 pod 把备份卷挂进来（不污染线上）
kubectl run -n radar pg-backup-ls --rm -it --restart=Never \
  --image=docker.io/library/postgres:17-alpine \
  --overrides='{"spec":{"containers":[{"name":"c","image":"docker.io/library/postgres:17-alpine","command":["ls","-lht","/backups"],"volumeMounts":[{"name":"b","mountPath":"/backups"}]}],"volumes":[{"name":"b","persistentVolumeClaim":{"claimName":"radar-backup"}}]}}'
```

# 二、恢复整库（最常用：数据被误删 / 改坏）

> ⚠️ 恢复会**覆盖现有数据**。如果只是想找回某几条，看第四节用「只恢复一张表」。

```bash
# 1. 先停掉写数据的任务，避免一边恢复一边被写脏
kubectl -n radar patch cronjob radar-collect radar-check-links radar-digest \
  -p '{"spec":{"suspend":true}}'
kubectl -n radar scale deployment radar --replicas=0

# 2. 把备份卷和脚本挂进一个 pod，执行恢复
kubectl run -n radar pg-restore --rm -it --restart=Never \
  --image=docker.io/library/postgres:17-alpine \
  --env="RADAR_DB_URL=$(kubectl -n radar get secret radar-pg -o jsonpath='{.data.RADAR_DB_URL}' | base64 -d)" \
  --overrides='{"spec":{"containers":[{"name":"c","image":"docker.io/library/postgres:17-alpine","command":["sh"],"stdin":true,"tty":true,"env":[{"name":"RADAR_DB_URL","value":"'"$(kubectl -n radar get secret radar-pg -o jsonpath='{.data.RADAR_DB_URL}' | base64 -d)"'"}],"volumeMounts":[{"name":"b","mountPath":"/backups"}]}],"volumes":[{"name":"b","persistentVolumeClaim":{"claimName":"radar-backup"}}]}}'

# 进了 pod 之后：
#   pg_restore --clean --if-exists --no-owner --dbname "$RADAR_DB_URL" /backups/radar-20261002-0310.dump

# 3. 把应用和任务放回来
kubectl -n radar scale deployment radar --replicas=2
kubectl -n radar patch cronjob radar-collect radar-check-links radar-digest \
  -p '{"spec":{"suspend":false}}'
kubectl -n radar patch cronjob radar-digest -p '{"spec":{"suspend":true}}'   # 周报保持暂停
```

# 三、恢复后必须校验

```bash
kubectl -n radar exec radar-postgres-0 -- psql -U radar -d radar -c "
SELECT 'snapshots' t, count(*) FROM snapshots
UNION ALL SELECT 'entries', count(*) FROM entries
UNION ALL SELECT 'changes', count(*) FROM changes
UNION ALL SELECT 'link_checks', count(*) FROM link_checks
UNION ALL SELECT 'mine', count(*) FROM mine;"

# 再打一次接口，确认应用真的能读到数据
curl -s http://<站点>/api/stats
```

# 四、只恢复一张表（更常用）

`-Fc` 格式的好处就是能挑表恢复：

```bash
# 例如只把 mine 表恢复到出事之前（先恢复到临时库，再拷回来，避免直接覆盖）
pg_restore --clean --if-exists --no-owner -t mine --dbname "$RADAR_DB_URL" /backups/radar-xxx.dump
```

# 五、恢复序列（容易忘的一步）

⚠️ 用 `pg_restore` 恢复带 id 的表之后，**自增序列可能仍是旧值**，
下一次插入就会主键冲突（这个坑线上真踩过：巡检任务连续 10 天失败）。
恢复完执行：

```sql
SELECT setval('snapshots_id_seq',   COALESCE((SELECT MAX(id) FROM snapshots), 1));
SELECT setval('changes_id_seq',     COALESCE((SELECT MAX(id) FROM changes), 1));
SELECT setval('link_checks_id_seq', COALESCE((SELECT MAX(id) FROM link_checks), 1));
```

（`app/migrate.py` 里的 `_reset_sequences()` 做的是同一件事。）

# 六、当前没有覆盖到的场景

| 场景 | 现在的备份能救吗 |
|---|---|
| 误删数据 / 改错数据 | ✅ 能（恢复到昨天） |
| 单张表被写坏 | ✅ 能（只恢复那张表） |
| **PG 的 PVC 损坏** | ❌ **不能**（备份和 PG 在同一个集群的卷上） |
| 整个集群挂了 | ❌ 不能 |

要把后两种也覆盖，需要把备份传到集群外（对象存储 / 另一台机器）。
这是已知的待办，不是"已经做好了"。
