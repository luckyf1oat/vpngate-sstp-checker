#!/bin/bash
# 多路并发跑 checker：每路一个独立 netns（各自的 ppp0 / 路由表，互不干扰）
# 用法：sudo bash scripts/parallel.sh [并发数]
set -u
JOBS="${1:-${JOBS:-4}}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

cleanup() {
  for i in $(seq 0 $((JOBS-1))); do
    ip netns exec "sstpw$i" pkill -f sstpc 2>/dev/null
  done
  sleep 1
  for i in $(seq 0 $((JOBS-1))); do
    ip netns del "sstpw$i" 2>/dev/null
    ip link del "vethw$i" 2>/dev/null
    iptables -t nat -D POSTROUTING -s "10.200.$((90+i)).0/24" -j MASQUERADE 2>/dev/null
    iptables -D FORWARD -i "vethw$i" -o eth0 -j ACCEPT 2>/dev/null
    iptables -D FORWARD -i eth0 -o "vethw$i" -j ACCEPT 2>/dev/null
    rm -rf "/etc/netns/sstpw$i" 2>/dev/null
  done
}
trap cleanup EXIT

sysctl -qw net.ipv4.ip_forward=1

for i in $(seq 0 $((JOBS-1))); do
  ns="sstpw$i"; v="vethw$i"; sub="10.200.$((90+i))"; host="$sub.1"; peer="$sub.2"
  ip netns del "$ns" 2>/dev/null; ip link del "$v" 2>/dev/null
  ip netns add "$ns"
  mkdir -p "/etc/netns/$ns" && printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n' > "/etc/netns/$ns/resolv.conf"
  ip link add "$v" type veth peer name "${v}p"
  ip link set "${v}p" netns "$ns"
  ip addr add "$host/24" dev "$v"; ip link set "$v" up
  ip netns exec "$ns" ip addr add "$peer/24" dev "${v}p"
  ip netns exec "$ns" ip link set "${v}p" up
  ip netns exec "$ns" ip link set lo up
  ip netns exec "$ns" ip route add default via "$host"
  iptables -t nat -I POSTROUTING 1 -s "$sub.0/24" -j MASQUERADE
  iptables -I FORWARD 1 -i "$v" -o eth0 -j ACCEPT
  iptables -I FORWARD 1 -i eth0 -o "$v" -j ACCEPT
done

cd "$REPO_DIR" || exit 1
rm -f result-*.json result-*.txt

for i in $(seq 0 $((JOBS-1))); do
  (
    ip netns exec "sstpw$i" env \
      PATH="$PATH" \
      SHARD="$i/$JOBS" \
      LIMIT="${LIMIT:-100}" MAX_PING="${MAX_PING:-400}" \
      CONNECT_TIMEOUT="${CONNECT_TIMEOUT:-10}" FETCH_TIMEOUT="${FETCH_TIMEOUT:-8}" \
      OUT_TXT="result-$i.txt" OUT_JSON="result-$i.json" \
      python3 checker.py > "worker-$i.log" 2>&1
  ) &
done
wait

echo "--- 各片结果 ---"
for i in $(seq 0 $((JOBS-1))); do
  echo "[worker $i] $(grep -E '待测|完成：' "worker-$i.log" | tr '\n' ' ')"
done

python3 scripts/merge.py
