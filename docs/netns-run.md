# 本机已有 ppp0 时怎么测（netns 隔离）

pppd 2.4.9 不支持 `ifname`，SSTP 一定抢 `ppp0`。如果跑测试的机器上已经有 ppp0
（L2TP、其它 SSTP、某些 4G 拨号等），直接在宿主跑会撞名/污染路由。

做法：起一个网络命名空间，veth + NAT 给它出网，在里头跑 checker —— 它的 `ppp0`
是独立 netns 里的，宿主隧道完全不受影响。

```bash
#!/bin/bash
set -u
NS=sstprun
cleanup() {
  ip netns exec $NS pkill -f sstpc 2>/dev/null; sleep 1
  ip netns del $NS 2>/dev/null; ip link del veth-r 2>/dev/null
  iptables -t nat -D POSTROUTING -s 10.200.97.0/24 -j MASQUERADE 2>/dev/null
  iptables -D FORWARD -i veth-r -o eth0 -j ACCEPT 2>/dev/null
  iptables -D FORWARD -i eth0 -o veth-r -j ACCEPT 2>/dev/null
  rm -rf /etc/netns/$NS 2>/dev/null
}
trap cleanup EXIT

ip netns add $NS
mkdir -p /etc/netns/$NS && printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n' > /etc/netns/$NS/resolv.conf
ip link add veth-r type veth peer name veth-r2
ip link set veth-r2 netns $NS
ip addr add 10.200.97.1/24 dev veth-r; ip link set veth-r up
ip netns exec $NS ip addr add 10.200.97.2/24 dev veth-r2
ip netns exec $NS ip link set veth-r2 up
ip netns exec $NS ip link set lo up
ip netns exec $NS ip route add default via 10.200.97.1
sysctl -qw net.ipv4.ip_forward=1
iptables -t nat -I POSTROUTING 1 -s 10.200.97.0/24 -j MASQUERADE
iptables -I FORWARD 1 -i veth-r -o eth0 -j ACCEPT
iptables -I FORWARD 1 -i eth0 -o veth-r -j ACCEPT

ip netns exec $NS env LIMIT=5 python3 checker.py
```

## 坑

- **FORWARD 默认 DROP**：只加 NAT 不放开 FORWARD，netns 里解析得了域名但连不出去，
  表现为 `sstpc: Could not lookup host`。必须加上面两条 FORWARD ACCEPT。
- **DNS**：netns 默认不继承 `/etc/resolv.conf`，要写 `/etc/netns/<ns>/resolv.conf`。
- **出口校验**：VPN Gate 的出口 IP 就是中继机自己的 IP，别拿它当"隧道没生效"的依据；
  只跟测试机自身出口 IP 比。
