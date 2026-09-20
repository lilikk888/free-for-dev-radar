#!/usr/bin/env bash
#
# 让 kubeadm 集群的控制面指标可被 Prometheus 抓取。
#
# 背景：kubeadm 默认把 kube-scheduler / kube-controller-manager 的 metrics 只绑在
# 127.0.0.1，etcd 的 metrics 也只监听 127.0.0.1，kube-proxy 的 metricsBindAddress
# 干脆是空字符串（= 关闭）。结果就是 Prometheus 里这几个 job 永远是 DOWN。
#
# 本脚本把它们改成可被抓取的地址，并保留原有监听地址不变。
# 幂等：重复执行不会重复改（用 if grep 判断）。
#
# 用法：
#   sudo NODE_IP=10.0.2.134 ./enable-control-plane-metrics.sh
#
# 回滚：
#   sudo cp /home/ubuntu/kubeadm-manifest-backup/*.yaml /etc/kubernetes/manifests/
#   （kube-proxy 用 kubectl -n kube-system rollout restart daemonset kube-proxy 恢复）
#
set -euo pipefail

# ⚠️ 本脚本要用 sudo 跑（要改 /etc/kubernetes/manifests），
# 但 root 没有 ~/.kube/config，kubectl 会去连 localhost:8080 然后失败。
# 显式指定 root 可读的 admin.conf。
if [ "$(id -u)" -eq 0 ] && [ -z "${KUBECONFIG:-}" ] && [ -f /etc/kubernetes/admin.conf ]; then
  export KUBECONFIG=/etc/kubernetes/admin.conf
  echo "==> 以 root 运行，已设置 KUBECONFIG=/etc/kubernetes/admin.conf"
fi

NODE_IP="${NODE_IP:-$(hostname -I | awk '{print $1}')}"
MANIFEST_DIR=/etc/kubernetes/manifests
BACKUP_DIR="${BACKUP_DIR:-/home/ubuntu/kubeadm-manifest-backup}"

echo "==> 节点 IP: ${NODE_IP}"

echo "==> 1/4 备份 static pod manifest 到 ${BACKUP_DIR}"
mkdir -p "${BACKUP_DIR}"
for f in kube-scheduler.yaml kube-controller-manager.yaml etcd.yaml; do
  if [ ! -f "${BACKUP_DIR}/${f}" ]; then
    cp -v "${MANIFEST_DIR}/${f}" "${BACKUP_DIR}/${f}"
  else
    echo "    ${f} 已有备份，跳过"
  fi
done

echo "==> 2/4 kube-scheduler / kube-controller-manager: bind-address 127.0.0.1 -> 0.0.0.0"
# 注意：绑 0.0.0.0 而不是 ${NODE_IP}。
# kubelet 的 liveness/readiness 探针打的是 127.0.0.1:probe-port，
# 如果只绑节点 IP，localhost 就不通了，静态 Pod 会无限重启。
for f in kube-scheduler.yaml kube-controller-manager.yaml; do
  if grep -q -- "--bind-address=127.0.0.1" "${MANIFEST_DIR}/${f}"; then
    sed -i "s|--bind-address=127.0.0.1|--bind-address=0.0.0.0|" "${MANIFEST_DIR}/${f}"
    echo "    已修改 ${f}"
  else
    echo "    ${f} 无需修改"
  fi
done

echo "==> 3/4 etcd: metrics 追加节点 IP 监听（保留 127.0.0.1）"
if grep -q -- "--listen-metrics-urls=http://127.0.0.1:2381\$" "${MANIFEST_DIR}/etcd.yaml"; then
  sed -i "s|--listen-metrics-urls=http://127.0.0.1:2381|--listen-metrics-urls=http://127.0.0.1:2381,http://${NODE_IP}:2381|" \
    "${MANIFEST_DIR}/etcd.yaml"
  echo "    已修改 etcd.yaml"
else
  echo "    etcd.yaml 无需修改"
fi

echo "==> 4/4 kube-proxy: metricsBindAddress -> 0.0.0.0:10249"
# 空字符串 "" 表示走默认值，也就是只监听 127.0.0.1:10249，外部抓不到
if kubectl -n kube-system get cm kube-proxy -o jsonpath='{.data.config\.conf}' \
     | grep -qE '^[[:space:]]*metricsBindAddress:[[:space:]]*(0\.0\.0\.0:10249)?[[:space:]]*$'; then
  echo "    kube-proxy 已经是 0.0.0.0:10249，无需修改"
else
  kubectl -n kube-system get cm kube-proxy -o jsonpath='{.data.config\.conf}' \
    | sed 's|^\([[:space:]]*\)metricsBindAddress:.*$|\1metricsBindAddress: 0.0.0.0:10249|' > /tmp/kube-proxy.conf
  echo "    改动后：$(grep -n 'metricsBindAddress' /tmp/kube-proxy.conf)"
  python3 -c 'import json; cfg=open("/tmp/kube-proxy.conf", encoding="utf-8").read(); open("/tmp/kp-patch.json","w").write(json.dumps({"data":{"config.conf":cfg}}))'
  kubectl -n kube-system patch cm kube-proxy --type merge --patch-file /tmp/kp-patch.json
  kubectl -n kube-system rollout restart daemonset kube-proxy
  kubectl -n kube-system rollout status daemonset kube-proxy --timeout=180s
  echo "    已修改并重启 kube-proxy"
fi

echo
echo "==> 完成。等静态 Pod 重建（约 30-60 秒）后验证端口："
echo "    sudo ss -lntp | grep -E ':2381|:10257|:10259|:10249'"
echo
echo "期望看到："
echo "    etcd                    127.0.0.1:2381 + ${NODE_IP}:2381"
echo "    kube-scheduler          *:10259"
echo "    kube-controller-manager *:10257"
echo "    kube-proxy              *:10249"
