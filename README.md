# vpngate-sstp-checker

两件事，全在 GitHub Actions 里跑：

| 工作流 | 频率 | 干什么 |
|---|---|---|
| **collect.yml** | 每小时 :05 | 拉官方 API + 列表页，把当前可见的 SSTP 服务器并进 `sstpall.txt` 库（去重累积） |
| **check.yml** | 每 2 小时 :30 | 用 **MS-SSTP 真实建链**测「上次有效的 + 全库」，活着的写进 `result.txt` |

## 产物

**`sstpall.txt`** —— 累积库，一行一个：

```
sstp://vpn:vpn@public-vpn-114.opengw.net:443
sstp://vpn:vpn@vpn849985359.opengw.net:1773
```

**`result.txt`** —— 本轮实测可用的（链接 + 出口 IP 详情）：

```
sstp://vpn:vpn@public-vpn-114.opengw.net:443  {"ip":"219.100.37.236","country":"JP","countryName":"Japan","region":"Tokyo","city":"Tokyo","asn":"AS36599","org":"SoftEther Corporation","timezone":"Asia/Tokyo","colo":"NRT"}
```

`result.json` 带连接耗时 `connect_s`；`sstpall.json` 带每台的
`first_seen / last_seen / last_checked / last_ok / ok_count / fail_count`。

## 评分榜 `result-ranked.txt`

同样的节点，按综合评分从高到低排；行尾 JSON 里多两个字段：`score`（0–100）和 `score_parts`（各项得分）。

| 维度 | 权重 | 怎么算 |
|---|---|---|
| 家宽 | 30 | 出口 org/ASN 命中运营商关键词 = 家宽(30)；中性(16.5)；命中机房/云/VPN 厂商(4.5) |
| 带宽 | 20 | `log10(speed_mbps)/log10(800)` 封顶，800Mbps 满分（vpngate 自报） |
| 在线时长 | 15 | CSV 的 `Uptime`（毫秒）换算成天，30 天满分 |
| 延迟 | 15 | `1 - ping/250ms` |
| 稳定性 | 15 | 库里的 `(ok+1)/(ok+fail+2)`，多轮存活才高分 |
| 负载 | 3 | `1 - sessions/150`，当前会话越少越好 |
| 建链 | 2 | `1 - connect_s/12s` |

`score_parts.kind` 是家宽判定结果：`res` / `unknown` / `idc`。
判定逻辑在 `ranking.py`：先查机房/云厂关键词（amazon/aws/google/azure/oracle/digitalocean/hetzner/softether/it7/dmit…），命中即 `idc`；
再看运营商关键词（telecom/communications/broadband/kddi/softbank/kt/sk broadband/comcast/verizon…），命中即 `res`。

> 注意：VPN Gate 的 `public-vpn-*.opengw.net` 是项目自己放在机房的机器（org=SoftEther Corporation），
> 稳定但不算家宽，评分会明显低于 `vpn<数字>.opengw.net` 这种志愿者家里的机器。

## 数据来源

- **官方 CSV API** `https://www.vpngate.net/api/iphone/`
  → HostName / IP / Ping / Speed / 国家 / 会话数，**但没有 SSTP 字段，也没有 SSTP 端口**
- **列表页** `https://www.vpngate.net/cn/`
  → API 唯一缺的东西在这：每行 `MS-SSTP → SSTP 主机名:端口`（端口常不是 443，如 :995/:1773/:1822）

两边取交集：API 有、页面也标了 SSTP 的才进库。

## 检测流程

1. 目标 = 上次 `result.json` 里可用的（优先）+ 库里其余（从未检测 → 最久未检测）
2. `sstpc --user vpn --password vpn --cert-warn <host:port> -- noauth nodetach noipdefault mtu 1400 ...`
3. 轮询 `ppp0` 拿 inet（8s 超时）
4. **只给 `ip.5671234.xyz` 的 A 记录加 `/32` 路由走 ppp0**，`curl --resolve` 取 JSON，查完立刻删路由
5. 出口 IP == 测试机自身 IP ⇒ 隧道没生效，丢弃
6. 杀 sstpc、确认 ppp0 消失，再测下一台；结果边测边写

**绝不改默认路由**：`defaultroute` 会把 runner 整机流量带走，GitHub agent 直接失联。

## 实测数据

- 单轮 85 台 → **68 台可用（80%）**，约 12 分钟
- 单台建链 **5–7 秒**
- 历史几轮：40 台→32、40 台→34、79 台→68

## 已知坑

1. **并发被限流**：3 路 netns 并发 77 台只有 3 台成功（4%）；单路串行 20 台有 18 台成功（90%）。
   VPN Gate 对同一源 IP 的并发 SSTP 连接限制很严 → 必须串行。
2. **出口 IP 就是中继机自己的 IP**，不能拿「出口 == 列表 IP」当失败判据，只跟测试机自身 IP 比。
3. **列表按请求方地区切分**：美西视角 98 台、香港视角 97 台，两边重合只有 1 台 —— 单视角看不全。
4. **pppd 2.4.9 没有 `ifname`**，SSTP 必定抢 `ppp0`：本机已有 ppp 隧道时要在 netns 里跑（见 `docs/netns-run.md`）。
5. 官方 `/api/` 的 `[VPNGate Data File]` 里那个 SOAP 服务已 302 到 `ddns.softether.org/notice`，死了。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `MAX_TEST` | 0 | 每轮最多测几台，0 = 全库 |
| `CONNECT_TIMEOUT` | 8 | 等 ppp0 的秒数 |
| `FETCH_TIMEOUT` | 8 | 取出口 IP 的超时 |
| `IPAPI_URL` | `https://ip.5671234.xyz/json/` | 出口 IP 详情接口 |
| `PRIORITY_FIRST` | 1 | 上次有效的排最前 |

## 本地跑

```bash
sudo apt-get install -y sstp-client ppp curl
python3 collect.py                       # 只采集，不起隧道
sudo -E env "PATH=$PATH" python3 check.py  # 实测（本机有 ppp0 时用 netns）
```
