# ASMR WebUI 部署指南（保姆版）

适用对象：拿到一份项目代码，要在一台\*\*Linux NAS（群晖、飞牛等）\*\*上从零跑起来的人。全程只需要 SSH 和一个浏览器。预计 30 分钟。

***

## 0. 这个项目是什么

跑在 NAS 上的 ASMR 作品库管理站，功能包括：

- **搜索发现**：从 asmr.one 搜索作品，勾选后批量下载到 NAS；
- **本地资源**：扫描已下载的作品，提供筛选、排序、标签搜索、详情、目录浏览；
- **播放器**：网页直接播放 NAS 上的音频，支持字幕、记忆进度；
- **生成字幕**：用本地 AI 模型把日语音频转成日文字幕，再调用翻译 API 生成中文字幕（可选功能）；
- **系统**：存储空间管理、完整性校验、中文版检查、密码设置。

架构：Python Flask 单进程 + SQLite 数据库，前端是纯 HTML/JS（无需构建），字幕识别用 onnxruntime 纯 CPU 推理。**不需要 Docker**。

项目特点：

- **IPv6 直连免流**：asmr.one 走 Cloudflare IPv6 直连（配置里固定 `pins` 解析），不消耗代理/VPN 流量，国内移动网络下 IPv6 流量免计费；
- **低配硬件友好**：Intel N100 迷你主机即可流畅运行全部功能（下载、播放、字幕识别），无需独立显卡，推理纯 CPU；
- **翻译成本极低**：字幕翻译调用 OpenAI 兼容 API，实测约 **0.1 元/小时**音频（按批发送 + 服务商前缀缓存优化），并支持分时禁用（如工作时间不调用 API）进一步省钱；
- **本地识别零上传**：ASR 识别完全在本机完成，翻译环节只上传纯文本，音频不出 NAS。

已知限制（生成字幕）：

- **多主角对话识别效果不佳**：模型针对单人旁白式语音优化，多声优对话、多人同时说话的场景容易漏字、错分段；
- **声道会合并识别**：推理时立体声会被下混为单声道，双声道分左右耳的音源可能出现单侧耳语相消导致漏识别。

硬件要求：

| 用途         | 最低要求                                      |
| ---------- | ----------------------------------------- |
| 只用下载/浏览/播放 | 总内存 ≥ 512MB，无特殊要求                         |
| 启用生成字幕     | **总内存 ≥ 4GB**（硬性要求，低于会自动禁用该功能），建议 4 核 CPU |

***

## 1. 需要准备的文件

把你手上的项目文件夹拷到 NAS。**只需要下面这些**，其余（`.screenshots`、`.trae`、`__pycache__`、`spec.md`、`tasks.md`、`release` 等）都不用拷：

```
asmr-ipv6/               ← 项目根，整体上传即可（排除上面列的开发文件）
├─ web/                  # 全部后端+前端，必传
├─ data/models/          # AI 模型，想用生成字幕就必须有（见第 5 步）
├─ deploy/               # systemd 服务文件和安装脚本
├─ downloader.py  asmr_client.py  ipv6_net.py   # 下载内核，必传
└─ config.example.json   # 配置模板
```

模型权重因超出 GitHub 普通文件限制，**放在仓库的 Release 附件里**，需要单独下载一次：

1. 打开本仓库页面 → 右侧 **Releases** → 最新版本；
2. 下载附件 `asmr-models.zip`（约 424 MB）；
3. 解压后把里面的 `models/` 整个目录放到项目根的 `data/models/`（最终结构见下表）。

| 文件 | 放哪 | 大小 |
|---|---|---|
| `vad_asmr_encdec.onnx` | `data/models/` | 约 114 MB |
| `Haas-ja-v1.0/`（encoder/decoder/joiner + 词表） | `data/models/Haas-ja-v1.0/` | 约 460 MB |

纯浏览/下载/播放（不用生成字幕）可以跳过这一步。

***

## 2. 上传文件到 NAS

方法任选其一：

**方法 A：群晖 SMB 共享（最简单）**

1. 在 File Station 新建共享文件夹 `asmr-webui-src`；
2. 把整个项目文件夹拖进去；
3. 后面用 SSH 把它挪到 `/opt/asmr-webui`。

**方法 B：scp 命令行**

在电脑上（Windows PowerShell）执行，按实际 IP/端口改：

```powershell
scp -P 22 -r .\asmr-ipv6 user@<NAS的IP>:/home/user/asmr-ipv6
```

***

## 3. 安装（SSH 到 NAS 后逐行执行）

SSH 登录：

```bash
ssh user@<NAS的IP>      # 端口默认 22，换成你的实际 IP 和账号
```

### 3.1 挪到标准位置

```bash
sudo mkdir -p /opt/asmr-webui
sudo cp -r /home/user/asmr-ipv6/. /opt/asmr-webui/
sudo chown -R user:Users /opt/asmr-webui     # 群晖用户组常为 Users，Ubuntu 用 user:user
```

### 3.2 确认 Python

```bash
python3 --version    # 需要 3.10 及以上
```

低于 3.10 先安装新版本，并记住它的可执行路径（下文统一用 `python3`）。

### 3.3 装 Python 依赖

```bash
python3 -m pip install --user flask onnxruntime numpy soundfile faster-whisper pypdf python-docx
```

- 前四个（flask/onnxruntime/numpy/soundfile）是**必需**的；
- 后三个（faster-whisper/pypdf/python-docx）是生成字幕解析台本/官方字幕用的，建议一起装。

装完验证：

```bash
python3 -c "import flask, onnxruntime, numpy, soundfile; print('ok')"
```

### 3.4 装系统工具

```bash
ffmpeg -version || sudo apt install -y ffmpeg      # 必须：转码/时长计算
ffprobe -version || sudo apt install -y ffmpeg
# 可选（一般不需要）：
sudo apt install -y libchromaprint-tools poppler-utils
```

群晖没有 apt 的话，去套件中心装 Video Station 相关依赖或用 SynoCommunity 的 ffmpeg 套件，确认 `ffmpeg` 在 PATH 里即可。

### 3.5 一键自检（可选但推荐）

```bash
bash /opt/asmr-webui/deploy/subtitle_setup.sh
```

全部显示 `[ok]` 就没问题，`[缺失]` 的按提示补装。

***

## 4. 写配置文件

```bash
cd /opt/asmr-webui
cp config.example.json config.json
nano config.json     # 没有 nano 用 vi config.json
```

改这几项（其余保持默认即可）：

```jsonc
{
  "api_base": "https://api.asmr.one",
  "account": "guest",          // asmr.one 的账号，guest 可匿名浏览
  "password": "guest",

  "web_port": 8787,            // 网页端口，被占用就换一个

  "storage_roots": [           // 下载保存位置，按优先级从上到下
    {"path": "/vol1/asmr-downloads", "min_free_gb": 50}
  ],
  "db_path": "/vol1/asmr-data/library.db"   // 数据库放哪，目录会自动建
}
```

注意事项：

1. **路径必须写 NAS 上的 Linux 绝对路径**，目录可以不存在，程序会创建；
2. 多块盘就写多个 `storage_roots` 条目，下载时从上往下找第一个空间够的；
3. `min_free_gb` 是该盘要预留的空间，小于它就跳到下一块盘；
4. `pins` 里的 IPv6 地址是 asmr.one 的 Cloudflare 解析固定值，**国内 IPv6 直连异常时保留**；如果你的网络访问 asmr.one 正常且报证书/连接错误，可以先整段删掉 `pins` 试试；
5. 走代理的话填 `"proxy": "http://IP:端口"`。

**验证配置**：

```bash
python3 /opt/asmr-webui/web/app.py
```

看到 Flask 启动日志后，在浏览器打开 `http://<NAS的IP>:8787` ——能看到页面就算成功。按 `Ctrl+C` 停止，进入下一步设成开机自启。

***

## 5. 配置开机自启（systemd）

```bash
sudo nano /etc/systemd/system/asmr-webui.service
```

粘贴以下内容，**三处按你实际情况改**（用户名、用户组、内存上限）：

```ini
[Unit]
Description=ASMR WebUI Server
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=user                       # ← 改成你的 NAS 登录用户
Group=Users                     # ← 群晖是 Users，Ubuntu 一般是同用户名
WorkingDirectory=/opt/asmr-webui
ExecStart=/usr/bin/python3 /opt/asmr-webui/web/app.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
StandardOutput=append:/var/log/asmr-webui.log
StandardError=append:/var/log/asmr-webui.err.log
MemoryMax=4G                    # ← 只下载浏览：512M；启用字幕生成：至少 4G，建议 6G

[Install]
WantedBy=multi-user.target
```

> `MemoryMax` 是给服务划的内存天花板：超过就重启该服务，防止拖死整台 NAS。字幕识别峰值约 2GB，所以要用字幕就别低于 4G。

启动并设为自启：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now asmr-webui
sudo systemctl status asmr-webui        # 显示 active (running) 即成功
```

浏览器打开 `http://<NAS的IP>:8787` 开始使用。

日常管理命令：

```bash
sudo systemctl restart asmr-webui    # 改了 config.json 或 Python 代码后重启
sudo systemctl stop asmr-webui
tail -n 100 /var/log/asmr-webui.log          # 看运行日志
tail -n 100 /var/log/asmr-webui.err.log      # 看报错日志
```

***

## 6. 启用「生成字幕」（可选功能）

前置条件：总内存 ≥4GB、第 3 步依赖装全、第 1 步模型文件放好。不满足的话此功能会显示「内存不足已禁用」，其他功能不受影响。

1. **翻译 API**：网页 → 设置 → 密钥令牌，填 OpenAI 兼容接口的 `API 地址`（如 `https://api.xxx.com/v1`）、`模型名`、`API Key`，点「保存设置」再点「测试配置」，显示「连接正常 xxx ms」即可。
2. **用一下**：在搜索发现或本地资源勾选作品 → 加入生成队列。流程是自动的：下载本地音频 → VAD 切分 → ASR 识别出日文 → 调 API 翻译成中文 → 字幕文件落到作品目录的 `生成字幕/中文版/`。
3. **调节**：生成字幕页右上角齿轮，可设识别线程数（默认 2）、空闲卸载分钟数、API 分时禁用时段（如工作时间不调用 API）等。
4. 观察：生成中/翻译中/待翻译等分组卡片实时显示进度，进度卡住先看 `/var/log/asmr-webui.err.log`。

***

## 7. 常见问题排查

| 现象              | 原因与处理                                                                                                 |
| --------------- | ----------------------------------------------------------------------------------------------------- |
| 浏览器打不开 8787     | `sudo systemctl status asmr-webui` 看是否 active；看 `.err.log` 是否报端口占用（改 `web_port`）或 Python 依赖缺失（重跑 3.3） |
| 搜索不出结果          | NAS 无法直连 asmr.one：配置代理，或保留/修正 `pins`；也可能是账号问题，试 `guest/guest`                                         |
| 下载报错 Errno 36   | 文件名过长，程序会自动截断，仍报错说明路径层级太深，换浅一点目录                                                                      |
| 下载任务卡在下载中       | 重启服务会中断任务并把 running 标记为失败，在「我的下载」点重新下载即可断点续传                                                          |
| 字幕功能显示「内存不足已禁用」 | 机器总内存 <4GB，属硬性限制；或把 `MemoryMax` 调大后重启                                                                 |
| 字幕整篇乱码          | 换 Haas 模型为非量化 FP32 版本（第 1 步的注意框）                                                                      |
| 翻译全部保留日文        | API 未配置/配错，或正处于分时禁用时段；设置 → 密钥令牌里「测试配置」验证                                                              |
| 想改了代码立即生效       | 改 `web/static/` 下的前端文件刷新页面即可；改 Python 文件必须 `sudo systemctl restart asmr-webui`                        |
| 磁盘满             | 存储空间按 `storage_roots` 顺序自动顺延；都满了会弹窗警告，清理后重试                                                           |

***

## 8. 升级与备份

**升级**（拿到新版代码时）：

```bash
sudo systemctl stop asmr-webui
# 用新代码覆盖 /opt/asmr-webui，但保留这三个东西：
#   config.json、data/、以及 db_path 指向的数据库文件
sudo systemctl start asmr-webui
```

**备份**：只需要备份 `config.json` + 数据库文件（`db_path` 指向的 `.db`）。已下载的音频和 `生成字幕/` 目录本身就在你的存储盘上。

**改文件前留个后悔药**是本项目惯例：任何较大改动前，把旧文件复制到 `/opt/asmr-webui/_backup_<日期>_<主题>/`，出问题直接拷回来。
