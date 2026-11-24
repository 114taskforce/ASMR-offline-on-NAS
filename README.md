# asmr-ipv6 — asmr.one 严格 IPv6-only 下载器

参考 [fireinrain/asmr-downloader v2](https://github.com/fireinrain/asmr-downloader/tree/v2) 的
API 流程与功能设计，用 Python 标准库重写，**硬性只走 IPv6，绝不使用 IPv4**。

```
ipv6_net.py      严格 IPv6 的 HTTPS 客户端（SNI 变体、AAAA、网卡绑定、代理、Range、重试）
asmr_client.py   API 封装（登录 / 作品 / 音轨树 / 搜索 / 列表）
downloader.py    命令行主程序
config.example.json  配置示例（默认已锁定「以太网 4」）
```

---

## 1. 环境诊断结论（为什么需要专门处理）

在本机实测（2026-11-19 WLAN、2026-11-20 有线网各一轮）：

| 项目 | 结果 |
| --- | --- |
| `asmr.one` / `api.asmr.one` 的 AAAA | Cloudflare `2606:4700:...`，纯 IPv6 TCP 可达 |
| 带 `api.asmr.one` 明文 SNI 的 TLS 握手 | **被 RST**（基于明文 SNI 的阻断；IPv4 直连同样被阻断） |
| 不带 SNI / 无关 SNI（如 `cloudflare.com`） | TLS 握手成功，但 Cloudflare 回 403（域名前置已禁） |
| SNI 末尾加一个点，如 `API.ASMR.ONE.` | **绕过阻断，HTTP 200 返回真实 API JSON**（连续 6/6 成功） |
| 媒体域名 `raw.kiko-play-niptan.one` | 有 Cloudflare AAAA，正常 SNI 即可，`Range` 返回 `206` |
| `api.asmr-300.com`（参考项目默认地址） | 本机 DNS 被污染：AAAA 是 `2001::/32` 合成地址、IPv4 解析到无关 IP，不可用 |
| 本地代理 `127.0.0.1:61963` | 只监听 IPv4 回环，且出口同样被 SNI 阻断，**不能**作为纯 IPv6 方案 |

因此本工具：

- API 基址默认改为 **`https://api.asmr.one`**；
- 对 asmr 系域名的 TLS **SNI 自动使用变体**（`API.ASMR.ONE.`），HTTP `Host` 头仍是真实域名，
  Cloudflare 会规范化 SNI 并正确路由；
- 证书链与主机名校验照常保留（因 SNI 是变体，握手后手动做 `match_hostname`）。

> 换网络环境后如果不需要变体，程序会按 SNI 候选顺序自动回退；也可 `--sni-policy never`。

### 当前有线网卡情况（2026-11-20）

- 有 IPv6 默认路由的网卡只有 **「以太网 4」（ifIndex 27, 1 Gbps）**，WLAN 4 已断开；
  唯一 `::/0` 路由的下一跳是 `fe80::16d8:64ff:fe10:5101%27`。
- 该网卡的 IPv6 是 **ULA 私有段** `fd00::...`（不是 `2001:da8:...` 那样的全局地址）：
  `fd00::8117:b5b:e18d:efb4`（/64）与临时地址 `fd00::f907:cd08:72fc:b99b`（/128）。
  实测由路由器做 NAT66 转换后**可以正常访问公网 IPv6**，源地址就是 `fd00::f907:cd08:72fc:b99b`。
- 有线链路上的 SNI 阻断策略与 WLAN **完全一致**（普通 SNI 被 RST，尾随点变体 200 OK），
  所以绕过逻辑无需改动。
- 系统同时存在 IPv4 默认路由（`192.168.1.1`），但**本工具在进程内把它彻底封死**，
  不会有任何 IPv4 流量（见下一节）。

---

## 2. 「只能使用 IPv6」是怎么保证的

程序入口第一件事调用 `enable_strict_ipv6()`：

1. `socket.getaddrinfo` 被替换 —— 只返回 `AF_INET6` 结果，显式 `AF_INET` 查询直接抛 `IPv4Forbidden`；
2. `socket.socket` 被替换 —— 创建 `AF_INET` socket 直接抛错，未指定 family 时默认 `AF_INET6`；
3. 所有连接显式 `AF_INET6` + 只连 AAAA 地址；`ipv6_net.py` 只实现 `https`；
4. 绑定网卡时 `--bind-ipv6` / `bind_interface` 自动发现的地址都经过
   `socket.inet_pton(AF_INET6, ...)` 校验，**传入 IPv4 字面量会直接抛 `IPv4Forbidden`**；
5. 证书链 + 主机名校验保留，不因 SNI 变体降低安全性。

也就是说：**进程内任何 IPv4 出站都会被拦截并报错**，不是「优先 IPv6、失败回退 IPv4」。
`check` 命令会实测创建一次 `AF_INET` socket 并确认它被拒绝。

> 注意：`--proxy` 的地址也必须是 IPv6 字面量或只解析出 AAAA 的主机名。
> 写 `127.0.0.1:8080` 会被拦截，请改用 `[::1]:8080` 或 IPv6 地址。

### 关于「IPv6 免流」——不能打包票

**工具能保证的是「下载流量不承载在 IPv4 上」，不能保证「不计费」。** 免不免流由学校 / 运营商的
计费系统按**出口路径、目的地址、账号套餐**判定，跟客户端用哪个协议族不是一回事。

本机 2026-11-20 实测（`check --egress`）：

- 公网出口 IPv6 是 `2001:da8:a800:811::3:ffb`，属于 **CERNET2 教育网 `2001:da8::/32`**，
  经 Cloudflare 香港节点（HKG）出去。这与之前 WLAN 的 `2001:da8:...` 是同一体系。
- 有利信号：多数高校对 CERNET2 的 IPv6 流量不计费 / 不计流量，只对 IPv4 收费。
  **但目的地址是 Cloudflare `2606:4700::`（国际方向）**，有的学校只对教育网内网地址免流、
  对国际 IPv6 仍计费 —— 这必须由你向校方 / 查看计费规则确认。

需要你自己核实的三点：

1. 贵校 IPv6 是否全免？还是只免 `2001:da8::/32` 等教育网网内地址？
2. 有线（经路由器 NAT66）与 WLAN 是否同一计费账号 / 同一出口策略？
3. **实测**：下载前后对比计费系统或路由器里的 IPv4 / IPv6 流量计数，这是唯一可靠的判定方式。

> 注意：`--bind-interface` 只锁定 LAN 侧网卡的源地址（`fd00::...` ULA）；经 NAT66 后，
> 公网看到的源地址由路由器 WAN 口决定，工具无法控制，也无法替你确认它是否免费。

**唯一残留的 IPv4 报文**：本机 DNS 服务器全是 IPv4（`192.168.1.1`、`223.5.5.5`、
`114.114.114.114`），`socket.getaddrinfo` 走系统解析器时会发出少量 IPv4 DNS 查询。
这不是下载流量、量极小，但如果要求「绝对零 IPv4 报文」，可用 `--pin` 把涉及域名
（`api.asmr.one`、`asmr.one`、媒体域名等）全部固定为 IPv6 字面量以跳过 DNS，或配置 IPv6 DNS。

自查命令：

```bash
python downloader.py check --egress --bind-interface "以太网 4"
```

---

## 3. 快速开始

```bash
cd C:/Users/Administrator/WorkBuddy/2026-11-19-19-56-53/asmr-ipv6

# 1) 先自检（网卡 IPv6、IPv4 护栏实测、AAAA、真实出栈源地址、登录、媒体域名）
python downloader.py check --bind-interface "以太网 4"

# 2) 只列出本机网卡的 IPv6 地址与 ifIndex
python downloader.py check --interfaces

# 3) 下载指定作品（显式锁定有线网卡，纯 IPv6）
python downloader.py download RJ01426915 -o ./downloads --bind-interface "以太网 4"

# 4) 只看会下载什么，不落盘
python downloader.py download RJ01426915 --dry-run -o ./downloads --bind-interface 27

# 使用配置文件（--config 必须放在子命令之前）
python downloader.py --config config.example.json download RJ01426915
```

**除 `--config` 外，所有选项写在子命令之后**（例如 `download RJxxx --workers 8`）。
`config.example.json` 里已把 `bind_interface` 设为 `"以太网 4"`，直接用配置文件即可锁定有线网卡。

---

## 4. 锁定出栈网卡（有线网）

三个等价写法，任选其一：

```bash
# 按接口名（中文名要用引号）
python downloader.py check --bind-interface "以太网 4"

# 按 ifIndex（更稳，不依赖接口名）
python downloader.py check --bind-interface 27

# 直接给源 IPv6 字面量（优先级最高）
python downloader.py check --bind-ipv6 fd00::f907:cd08:72fc:b99b
```

工作过程：

1. `--bind-interface` 通过解析 `ipconfig /all` 找到该网卡当前的非 link-local IPv6 地址
   （优先 RFC4941 临时地址，与 Windows 默认出栈选择一致），因此**临时地址续租变化也不会失效**；
2. 每次建立连接前对该地址做 `bind((addr, 0))`（2 元组；Windows 上给 ULA 附加 `scope_id` 会报
   `WinError 10049`），绑定到该网卡独有的源地址即锁定出接口；
3. `check` 会建立一次**真实 TCP 连接**并读取 `getsockname()`，打印
   `真实 TCP 出栈源地址`，与绑定值比对，输出 `绑定生效: 实际源地址 == 绑定地址`。

`check` 输出示例（有线网）：

```
本机网卡 IPv6 概览:
  以太网 4 (ifIndex=27)    fd00::f907:cd08:72fc:b99b, fd00::8117:b5b:e18d:efb4
  Radmin VPN (ifIndex=5)   fdfd::1a7b:53f9
IPv4 护栏: 已启用（实测 AF_INET socket 被拒绝，进程内不可能有 IPv4 出站）
系统默认 IPv6 出栈地址: fd00::f907:cd08:72fc:b99b
锁定网卡: 以太网 4 (ifIndex=27)
绑定源 IPv6: fd00::f907:cd08:72fc:b99b
真实 TCP 出栈源地址: fd00::f907:cd08:72fc:b99b:59875  (目标 2606:4700:3034::6815:2686)
绑定生效: 实际源地址 == 绑定地址，流量确实走所选网卡
```

> 不指定绑定参数时，因为当前唯一的 IPv6 默认路由就在「以太网 4」，系统本来也会走有线网卡；
> 显式绑定是为了在 WLAN 重新连接等情况下**仍然强制走有线**。

---

## 5. 命令一览

| 命令 | 说明 |
| --- | --- |
| `check` | 诊断 IPv6 环境、IPv4 护栏、真实出栈源地址、AAAA、SNI 策略、API 登录、媒体域名 |
| `check --interfaces` | 只列出本机网卡的 IPv6 地址与 ifIndex 后退出 |
| `check --egress` | 探测公网出口 IPv6、判断地址段、提示 DNS/IPv4 侧信道（免流自查） |
| `search "表达式" -c 10` | 搜索作品，支持参考项目的高级语法 |
| `works -c 10 --order dl_count` | 列出作品列表 |
| `list RJ01426915 -d` | 查看音轨文件类型统计 / 详细列表 |
| `download RJ01426915,RJ01037721` | 批量下载 |
| `download hot -n 10` | 下载热门作品前 N 个 |
| `export RJ01426915 -o ./export` | 导出 `links.txt` + `aria2_download.sh` |

搜索语法（与参考项目一致）：`关键词,排除词@过滤条件?分页参数`

```bash
python downloader.py search "护士,-中出@duration:1h?order=dl_count&sort=desc" -c 20
python downloader.py search "@va:陽向葵ゅか?subtitle=1" -c 20
```

---

## 6. 常用参数

| 参数 | 说明 |
| --- | --- |
| `--bind-interface IFACE` | 锁定出栈网卡：接口名或 ifIndex，如 `"以太网 4"` / `27` |
| `--bind-ipv6 IPv6` | 锁定出栈源 IPv6 地址（IPv4 一律拒绝）；优先于 `--bind-interface` |
| `-o, --dir` | 下载目录（相对路径基于当前工作目录） |
| `--workers N` | 并发数，默认 4 |
| `--qps N` | 每秒请求上限，默认 1.0，`0` 不限 |
| `--retries N` / `--timeout S` | 重试次数 / 超时秒数 |
| `--prefer-media mp3>wav>flac` | 音频格式优先级，`all` 表示不限 |
| `--include-ext .mp3,.png` / `--exclude-ext .mp4` | 扩展名白 / 黑名单 |
| `--include-keyword` / `--exclude-keyword` | 路径关键词过滤 |
| `-f, --force` | 覆盖已存在文件（默认跳过已完成文件） |
| `--dry-run` / `--limit N` | 只列计划 / 每个作品最多下载 N 个文件 |
| `--pin HOST=IPv6` | 手动固定 IPv6 地址，绕开被污染的 DNS（可重复） |
| `--proxy http://[::1]:8080` | IPv6 HTTP CONNECT 代理 |
| `--proxy socks5://[2001:db8::1]:1080` | IPv6 SOCKS5 代理（支持账号密码） |
| `--sni-policy auto\|always\|never` | SNI 变体策略，默认 `auto` |

示例（DNS 被污染时固定地址 + 锁定有线网卡）：

```bash
python downloader.py download RJ01426915 \
    --bind-interface 27 \
    --pin api.asmr.one=2606:4700:3034::6815:2686
```

配置文件见 `config.example.json`，命令行参数会覆盖配置文件。

---

## 7. 与参考项目的功能对照

| 参考项目 | 本工具 |
| --- | --- |
| `/api/auth/me` 取 JWT | ✅ |
| `/api/work/<id>`、`/api/tracks/<id>` 递归音轨树 | ✅ |
| `/api/search`、`/api/works`、`/api/recommender/popular` | ✅（hot 用 `works?order=dl_count`） |
| 站点测速选最快 API | 简化为固定 `api.asmr.one` + SNI 变体 |
| 格式优先级 / 扩展名 / 关键词过滤 | ✅ |
| 跳过已完成 / 断点续传（Range 206）/ 重试退避 / 并发 / 限流 | ✅ |
| 导出 `links.txt` + aria2 脚本 | ✅ |
| `sync` 全量元数据同步、SQLite、Web 播放界面 | ❌（未实现，非下载必需） |
| **强制仅 IPv6** | ✅ 本工具核心 |
| **锁定指定网卡（有线/WLAN）出栈** | ✅ `--bind-interface` / `--bind-ipv6` |

> `export` 生成的 `aria2_download.sh` 只是备用：aria2 无法硬性禁止 IPv4。
> 严格要求纯 IPv6 时请直接用本工具的 `download` 命令。

---

## 8. 排错

| 现象 | 处理 |
| --- | --- |
| `IPv4Forbidden` | 有代码/参数试图走 IPv4；检查 `--proxy` 是否写了 `127.0.0.1`，改成 `[::1]`；`--bind-ipv6` 不能填 IPv4 |
| `ConnectionResetError` / 连接被重置 | 先跑 `check`；试 `--sni-policy always`；必要时 `--pin` 固定 Cloudflare IPv6 |
| `no AAAA record` | 该域名无 IPv6 或被 DNS 污染，用 `--pin HOST=IPv6` 指定 |
| 接口名找不到 / 无可用 IPv6 | 跑 `check --interfaces` 看实际名字；改用 ifIndex（如 `27`）或 `--bind-ipv6` 直接指定 |
| `WinError 10049` bind 失败 | 程序用 2 元组 bind 已规避；若自己改代码，不要给 ULA 源地址附加 `scope_id` |
| 网卡 IPv6 地址变了 | 用 `--bind-interface`（每次启动自动发现），不要硬编码临时地址 |
| 登录 422 / token 为空 | 换 `--account/--password`（默认 `guest/guest`）；请求已带 `Content-Type: application/json` |
| 下载到一半断开 | 自动重试 + 从已下载字节 `Range` 续传；也可降低 `--workers` / `--qps` |
| 文件名/路径过长 | 已做非法字符净化与 Windows 长路径 (`\\?\`) 处理 |

---

## 9. 验证记录

**2026-11-19（WLAN，全局 IPv6 `2001:da8:...`）**

- `check`：IPv6 出栈地址、AAAA、登录（拿到 JWT）、`/api/works`（站点 62978 个作品）、媒体域名 AAAA 全部 OK；
- `works` / 高级 `search` / `list` / `export` 均正常；
- 真实下载一个 11,791,434 字节文件成功；
- 人为截断到一半后重跑，返回 `206 Partial Content` 并续传回完整大小；
- 再次运行自动 `skip`；`AF_INET` socket 创建被硬阻断。

**2026-11-20（有线网「以太网 4」，ULA `fd00::...` + NAT66）**

- 有线 IPv6 TCP 可达 Cloudflare；SNI 阻断策略与 WLAN 一致（普通 SNI 被 RST，`API.ASMR.ONE.` 返回 200）；
- 新增网卡绑定：`--bind-interface "以太网 4"` / `27` / `--bind-ipv6` 三种写法均验证；
- `check --interfaces` 正确列出网卡与 ifIndex；`check` 实测 `AF_INET` socket 被拒绝；
- 真实 TCP `getsockname()` 确认出栈源地址 == 绑定的有线网卡 IPv6；
- 传入 `--bind-ipv6 192.168.1.100` 被 `IPv4Forbidden` 明确拒绝；
- 登录、作品信息、音轨树、真实下载与 `206` 断点续传在有线链路上复验通过；
- 新增 `check --egress`：实测公网出口 `2001:da8:a800:811::3:ffb`（CERNET2，CF 节点 HKG），
  并列出本机 3 个 IPv4 DNS 服务器以提示侧信道；普通 `check` 回归正常。

验证过程产生的 `downloads/` 与 `export/` 内容已清理，可自行重新运行下载。

---

仅用于下载你有权访问的内容，请遵守站点条款与当地法规，并避免高频请求。
