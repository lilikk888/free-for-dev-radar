# free-for-dev 变更雷达

盯着 [ripienaar/free-for-dev](https://github.com/ripienaar/free-for-dev) 的 README，自动抓出三类变化：

| 变更类型 | 含义 |
|---|---|
| `added` | 新收录的免费服务 |
| `removed` | 已下线 / 被移除的服务 |
| `updated` | **免费额度描述发生变化** —— 最有价值的一类 |

为什么需要它：free-for-dev 是社区维护的清单，服务商随时会砍免费层，但清单不会主动通知你。
比如 Oracle 把 Always Free 的 ARM 额度从 4 OCPU / 24 GB 砍到 2 / 12，
网站上的数字和官方文档会各自领先或落后一段时间。**谁先发现，谁少踩坑。**

## 工作方式

```
GitHub raw README ──► 解析器 ──► 逐条指纹比对 ──► SQLite ──► JSON API ──► 网页
                      (parser)   (collector)      (db)       (web)
```

- **解析**：按缩进区分「顶级服务」和「子条目」，免费额度写在子条目里，所以追踪粒度到子条目
- **比对**：对每个条目算一个 `fingerprint`（分类 + 路径 + 链接 + 描述），
  描述变了指纹就变 → 记为 `updated`。条目 key 是「分类 + 完整路径」，所以章节重组不会误报
- **兜底**：解析结果为空会直接报错，不会静默清空历史（防止上游改格式后把数据洗掉）

## 本地跑

```bash
pip install -r requirements-dev.txt

# 抓一次，写入 ./data/radar.db
python -m app.cli collect

# 起网页，浏览器打开 http://localhost:8000
uvicorn app.web:app --reload

# 跑测试
pytest -v
```

用 Docker 也行：

```bash
docker compose up --build
```

## 部署到 Kubernetes（kubeadm）

清单在 `k8s/`，用 kustomize（`kubectl` 内置）渲染，镜像地址和 tag 都不用硬编码。

### 集群需要预先装好的东西

kubeadm 装出来的是一个"空壳"集群，下面这些组件**必须自己装**——这正是它和 k3s 最大的差别
（k3s 会把这些全打包好，便利但也把运维细节一起藏掉了）：

| 组件 | 用途 | 安装方式 |
|---|---|---|
| Calico v3.30.0 | CNI 网络插件 | `kubectl apply -f` 官方 manifest |
| ingress-nginx | Ingress 控制器（替代 k3s 内置的 Traefik） | Helm，DaemonSet + hostPort 80/443 |
| local-path-provisioner | 提供 `local-path` StorageClass | `kubectl apply -f` 官方 manifest |

控制面是 4 个独立 static pod（etcd / kube-apiserver / kube-scheduler / kube-controller-manager），
存储用真 etcd；容器运行时是 containerd（`SystemdCgroup = true`），kubelet 走 systemd cgroup driver。

### 一次性准备

给部署用户配好 kubeconfig（流水线里用的是**不带 sudo** 的 `kubectl`）：

```bash
mkdir -p ~/.kube
sudo cp -f /etc/kubernetes/admin.conf ~/.kube/config
sudo chown $(id -u):$(id -g) ~/.kube/config
```

**部署前必须做的三件事：**

1. **改 owner**：把 `k8s/*.yaml` 里的 `ghcr.io/owner/` 换成你的 GitHub 用户名。
   （流水线会自动 sed，但手动 `kubectl apply` 时需要你自己改对）
2. **把 GHCR 包设为公开**：GitHub 仓库 → 右下角 Packages → 选镜像 →
   Package settings → Change visibility → Public。
   否则节点拉不动镜像（私有仓库需要额外的 imagePullSecret）
3. **配 3 个仓库 Secret**（Settings → Secrets and variables → Actions）：

   | Secret | 值 |
   |---|---|
   | `K3S_HOST` | 节点 IP（名字里的 K3S 是历史遗留，就是这台 k8s 节点） |
   | `K3S_USER` | `ubuntu` |
   | `K3S_SSH_KEY` | 部署用私钥的全文 |

   建议专门生成一把部署密钥，别用你日常登录的那把：

   ```bash
   ssh-keygen -t ed25519 -C "github-actions-deploy" -f ~/.ssh/radar_deploy -N ""
   ssh-copy-id -i ~/.ssh/radar_deploy.pub ubuntu@<你的服务器IP>
   # 把 radar_deploy（私钥）全文粘到 K3S_SSH_KEY
   ```

然后推代码到 `main` 分支，流水线自动跑完三步：测试 → 构建 `linux/amd64,linux/arm64` → 滚动更新。

手动渲染看一眼清单：

```bash
kubectl kustomize k8s/
```

## 排错

| 现象 | 原因 |
|---|---|
| Pod 一直 `ErrImagePull` / `ImagePullBackOff` | GHCR 包还是私有的，按上面第 2 条改公开 |
| Pod 卡在 `ContainerCreating` 很久 | 先看节点 CPU 是否被占满（比如实例上的保活脚本） |
| 网页能开但列表是空的 | 还没采集过，跑一次 `python -m app.cli collect` |
| `Rollout` 卡住不动 | PVC 是 RWO，Deployment 已设 `strategy: Recreate`，检查是否被别的 Pod 占着卷 |
| 解析结果为空报错 | 上游 README 格式变了，去 `app/parser.py` 更新规则 |

## 结构

```
app/
  parser.py       README -> 结构化条目（纯函数，好测）
  collector.py    抓取 + 指纹比对，产出变更记录
  db.py           SQLite schema 与连接
  web.py          只读 JSON API + 静态首页
  cli.py          CronJob 入口
  static/         前端单页
k8s/              kustomize 管理的部署清单
tests/            解析器与变更检测的单元测试
```
