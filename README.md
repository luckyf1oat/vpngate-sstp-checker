# vpngate-sstp-checker

每 2 小时用 **MS-SSTP 真实建链**测一轮 VPN Gate 公共中继，输出可用节点 + 出口 IP 详情。

- 数据源：<https://www.vpngate.net/cn/>（列表页里标了 `SSTP 主机名` 的才测）
- 协议：MS-SSTP，账号固定 `vpn` / `vpn`（VPN Gate 官方约定）
- 出口信息：<https://ip.5671234.xyz/json/>（Cloudflare 边缘侧回显 + PROXYCHECK 归属）
- 调度：GitHub Actions，`0 */2 * * *`，也可手动 `workflow_dispatch` 调 `limit` / `max_ping`

## 产物

`result.txt`，一行一个可用节点，格式：

```
sstp://vpn:vpn@<host>:<port>  {ip详细信息}
```

例：

```
sstp://vpn:vpn@vpn475501975.opengw.net:1462  {"ip":"219.28.138.78","country":"JP","countryName":"Japan","region":"Tokyo","city":"Tokyo","asn":"AS17676","org":"Japan nation-wide Network of SOFTBANK Corp.","timezone":"Asia/Tokyo","colo":"NRT"}
```

`result.json` 是结构化版（含连接耗时 `connect_s`、网关自报 ping/带宽、测试台数）。

## 怎么做的

1. **先用官方 API** `https://www.vpngate.net/api/iphone/` 取服务器清单（CSV：HostName / IP / Ping / Speed / 国家 / 会话数）。
2. 再抓一次列表页，只补 API 里缺的那个字段 —— 每台的 **`SSTP 主机名:端口`**（SSTP 端口常不是 443，如 :995 / :1773 / :1822）。
3. 两边取交集：只测"API 里有 + 页面标了 SSTP"的主机。
4. 按 ping 升序、带宽降序取前 `LIMIT` 台。
5. 对每台：`sstpc --user vpn --password vpn --cert-warn host:port -- noauth nodetach noipdefault mtu 1400 ...`
   - 轮询 `ppp0` 是否拿到 inet，最多 `CONNECT_TIMEOUT` 秒
   - 起来后**只给 ip.5671234.xyz 的 A 记录加 /32 路由走 ppp0**，`curl --resolve` 拿 JSON
   - 立刻删路由、结束 sstpc，确保 `ppp0` 消失再测下一台
6. 结果边测边写（中途挂掉也留得住）。

## 安全设计（重要）

- **绝不改默认路由**：不加 `defaultroute`，只钉一个 /32 走隧道，查完即删。CI runner 或生产机上都不会把整机流量带走。
- **启动前检查已有 ppp0**：pppd 2.4 不能自定义 ifname，若本机已有 ppp 隧道（比如 L2TP/其它 VPN）会撞名，直接退出不碰（`ALLOW_EXISTING_PPP0=1` 可强制）。
- 每台测完确保 `ppp0` 被清理，残留则 `ip link del`。

## 本地跑

```bash
sudo apt-get install -y sstp-client ppp curl
LIMIT=5 sudo -E env "PATH=$PATH" python3 checker.py
```

本机已经有 `ppp0` 时，用隔离 netns 跑（见 `docs/netns-run.md`）。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `LIMIT` | 40 | 每轮测几台 |
| `MAX_PING` | 400 | 只测自报 ping ≤ 该值的 |
| `CONNECT_TIMEOUT` | 15 | 等 ppp0 起来的秒数 |
| `FETCH_TIMEOUT` | 12 | 取 IP 详情的超时 |
| `VPNGATE_URL` | `https://www.vpngate.net/cn/` | 列表页 |
| `IPAPI_URL` | `https://ip.5671234.xyz/json/` | 出口 IP 详情接口 |
| `ALLOW_EXISTING_PPP0` | 0 | 已存在 ppp0 时是否硬跑 |

## 为什么不能只用 API

`api/iphone/` 的 CSV 里**没有 SSTP 字段、也没有 SSTP 端口**，它只给 OpenVPN 配置。
试过从 OpenVPN 配置里的 `remote <ip> <port>` 推 SSTP 端口——**不可靠**：
`vpn805893912` 配置里是 995、页面 SSTP 也是 995，但同类服务器里有连不上的；
换个思路直接拿页面上的 `SSTP 主机名:端口` 才准。所以是「API 出清单 + 页面补端口」。

## 并发是坑（实测）

| 方式 | 样本 | 可用率 | 耗时 |
|---|---|---|---|
| 单路串行 | 20 台 | **18/20（90%）** | ~3 分钟 |
| 3 路 netns 并发 | 77 台 | **3/77（4%）** | 50 秒 |

VPN Gate 对**同一源 IP 的并发 SSTP 连接**有明显限流：并发一上来，绝大多数握手直接被拒（0.5–5s 内失败）。
所以默认 `jobs=1` 串行跑，79 台约 10–13 分钟；`scripts/parallel.sh` 留着，但在被限流的环境里没意义。

## 已知限制

- VPN Gate 的 SSTP 出口 IP 就是该中继服务器自己的公网 IP，所以"出口 IP == 列表里的 IP"是**正常**的；脚本只在出口等于测试机自身 IP 时才判失败。
- 公共中继不稳，通常 30%–70% 能连上；连不上多是服务器侧限流/下线。
- 列表页有中文/英文两版，解析按"行内出现 `SSTP` + `*.opengw.net`"匹配，改版可能失效。
