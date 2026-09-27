# 私人德州牌桌：从零部署到维护（Ubuntu 单机版）

本指南针对现有的腾讯云 Ubuntu 26.04 服务器：公网 IP `119.28.204.247`，牌桌地址 `https://poker.8u7hybby.com`。首次上线按用户选择从**空数据库**开始，不上传本机旧牌局。照着操作时，先判断自己是在“首次安装”还是“更新已经运行的服务”，不要把首次安装命令直接用于现有服务器。

> **更新已有服务器：**先完成本地测试并把目标提交上传 GitHub，再检查服务器当前提交与工作区状态，只允许快进更新。不要为了追上 GitHub 强制重置服务器分支；正式数据库和私密配置仍留在代码目录外。

## 先看懂程序如何工作

浏览器 → DNS 把 `poker.8u7hybby.com` 解析为服务器 IP → Caddy 接收 HTTPS 和 WebSocket → 转发到本机 `127.0.0.1:8000` 的 Uvicorn/FastAPI → SQLite 保存房间、玩家、买入和手牌。应用只运行**一个 worker**，因为当前 WebSocket 连接和管理员会话存放在进程内。

| 内容 | 服务器路径 | 用途 |
| --- | --- | --- |
| 代码和 Python 虚拟环境 | `/opt/texas-holdem/` | 可随版本更新 |
| 正式数据库 | `/var/lib/texas-holdem/texas_holdem.sqlite3` | 不随代码更新覆盖 |
| 数据库备份 | `/var/backups/texas-holdem/` | 每日在线快照 |
| 管理员密码和路径配置 | `/etc/texas-holdem/texas-holdem.env` | 只留在服务器，不进 Git |
| 程序及备份服务 | `/etc/systemd/system/` | 开机启动、失败重启、定时备份 |
| 公网入口 | `/etc/caddy/Caddyfile` | HTTPS 和反向代理 |

数据库与备份可能包含未公开底牌及玩家身份。不要把它们、真实密码或 SSH 私钥放进 Git、网页静态目录或聊天记录。

## 首次安装，第 1 步：服务器和网络

在腾讯云确认实例运行、记下**公网** IPv4，并在轻量应用服务器防火墙放行 TCP `22`（SSH）、`80`（HTTP 和证书验证）、`443`（HTTPS）。不要向公网开放 `8000`。若 Ubuntu 自己启用了 UFW，也需要允许 SSH、80、443；更改 UFW 前先确保当前 SSH 能继续登录。

在 **Windows PowerShell** 登录。密码输入时不会显示字符，这是正常现象：

```powershell
ssh ubuntu@119.28.204.247
```

默认登录用户名是 `ubuntu`。本次 SSH 登录密码曾在对话中提供；若至今未更换，在 SSH 终端运行 `passwd` 修改当前用户密码，之后可改用 SSH 密钥。SSH 密码与管理网页密码分别设置，不要复用。

## 第 2 步：让域名指向服务器

腾讯云 DNSPod → `8u7hybby.com` →“记录管理”→“添加记录”，填入：

| 字段 | 值 |
| --- | --- |
| 主机记录 | `poker` |
| 记录类型 | `A` |
| 线路类型 | `默认` |
| 记录值 | `119.28.204.247` |

`@` 是根域名记录，不等于 `poker`。到“解析设置 / 域名信息”检查状态，以及域名注册商正在使用的 NS 与 DNSPod 分配的 NS 是否一致。本次界面显示 `louse.dnspod.net`、`macro.dnspod.net`；以后以控制台当时显示的值为准。

在 **Windows PowerShell** 验证公共解析：

```powershell
Resolve-DnsName poker.8u7hybby.com -Type A -Server 1.1.1.1
```

结果应含 `119.28.204.247`。如果控制台已有记录但公共查询失败，先核对完整子域名、记录是否启用、NS 是否匹配，然后等待解析生效并重试。官方说明：[设置 A 记录](https://docs.dnspod.cn/dns/help-a/)和[解析生效排查](https://docs.dnspod.cn/dns/help-dns-effect/)。

## 第 3 步：安装基础软件

以下命令开始都在 **Ubuntu SSH 终端**执行，不在 Windows PowerShell 执行。`sudo` 可能要求再次输入 Ubuntu 密码。每行成功后再执行下一行。

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip curl
```

按 [Caddy 官方 Ubuntu 安装说明](https://caddyserver.com/docs/install)配置稳定版软件源：

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg
sudo chmod o+r /etc/apt/sources.list.d/caddy-stable.list
sudo apt update
sudo apt install -y caddy
```

用 `python3 --version`、`git --version`、`caddy version` 确认都能输出版本。

## 第 4 步：取代码并安装 Python 依赖

创建只用于运行服务的账号；它不能用于 SSH 登录：

```bash
sudo useradd --system --home /nonexistent --shell /usr/sbin/nologin texas-holdem
```

这条命令只用于首次安装。若提示账号已存在，先运行 `id texas-holdem` 核实。确认 GitHub 已包含所需提交后，安装项目：

```bash
sudo git clone https://github.com/1148802323/Texas_Holdem.git /opt/texas-holdem
sudo python3 -m venv /opt/texas-holdem/.venv
sudo /opt/texas-holdem/.venv/bin/python -m pip install -r /opt/texas-holdem/backend/requirements.txt
```

`/opt/texas-holdem/.venv` 是这个项目自己的 Python 环境，不会改动你电脑上的 Miniconda。

## 第 5 步：建立空库所需的目录和私密配置

```bash
sudo install -d -o texas-holdem -g texas-holdem -m 0700 /var/lib/texas-holdem /var/backups/texas-holdem
sudo install -d -m 0700 /etc/texas-holdem
sudo install -m 0600 /opt/texas-holdem/deploy/texas-holdem.env.example /etc/texas-holdem/texas-holdem.env
sudo nano /etc/texas-holdem/texas-holdem.env
```

在 Nano 中仅把空白的 `TEXAS_ADMIN_PASSWORD=` 改为自己新生成、至少 12 位的强密码。其他两行应指向：

```text
TEXAS_DB_PATH=/var/lib/texas-holdem/texas_holdem.sqlite3
TEXAS_BACKUP_DIR=/var/backups/texas-holdem
```

按 `Ctrl+O`、回车保存，再按 `Ctrl+X` 退出。不要用会把密码打印到终端的命令检查配置。首次启动会自动创建空数据库并运行迁移；本次没有把本机数据复制过来。

## 第 6 步：启动程序和每日备份

把仓库里的三个 systemd 模板安装到系统目录：

```bash
sudo install -m 0644 /opt/texas-holdem/deploy/texas-holdem.service /etc/systemd/system/
sudo install -m 0644 /opt/texas-holdem/deploy/texas-holdem-backup.service /etc/systemd/system/
sudo install -m 0644 /opt/texas-holdem/deploy/texas-holdem-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now texas-holdem.service texas-holdem-backup.timer
```

先在服务器本机验证，不依赖 DNS 或 HTTPS：

```bash
systemctl is-active texas-holdem.service
curl -fS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/admin
sudo ss -lntp
```

预期分别为 `active`、`200`，并能看到 Uvicorn 只监听 `127.0.0.1:8000`。若失败，用 `sudo journalctl -u texas-holdem.service -n 50 --no-pager` 查看原因，优先检查密码是否为空、数据库目录权限及依赖是否装完。

手动触发一份备份，再检查文件：

```bash
sudo systemctl start texas-holdem-backup.service
sudo systemctl status texas-holdem-backup.timer --no-pager
sudo ls -lh /var/backups/texas-holdem/
```

timer 按服务器本地时间每天 03:00 执行，错过时补跑；备份脚本用 SQLite 在线备份接口制作并校验快照。若失败，查看 `sudo journalctl -u texas-holdem-backup.service -n 50 --no-pager`。备份目前**不会自动清理旧文件**，要检查磁盘空间，并保留一份受保护的异机备份。

## 第 7 步：配置 Caddy 和 HTTPS

仅在首次安装、服务器没有其他网站配置时复制模板：

```bash
sudo install -m 0644 /opt/texas-holdem/deploy/Caddyfile.example /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile
```

把示例域名改成实际域名，最终站点块为：

```caddyfile
poker.8u7hybby.com {
    reverse_proxy 127.0.0.1:8000
}
```

如果机器已有其他网站，不要覆盖原 Caddyfile，而是加入这个站点块。保存后校验并重载：

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
systemctl is-active caddy
```

DNS 已指向服务器、80/443 已开放时，浏览器打开 `https://poker.8u7hybby.com/admin` 应显示管理登录页。打不开时，按 **DNS → 云防火墙 → Caddy → 本机 8000** 的顺序定位；Caddy 日志命令是 `sudo journalctl -u caddy -n 50 --no-pager`。不要直接对公网开放 Uvicorn 的 8000 端口。

## 第 8 步：验收网页功能

1. 管理员登录 `/admin`，创建一张测试桌，检查盲注、人数、买入上限、各阶段时限和邀请链接。
2. 用两个浏览器或隐私窗口通过邀请链接加入不同昵称，分别入座、买入、准备并确认开局。
3. 打一手牌，确认自己只能看到自己的未公开底牌；刷新页面后身份、筹码和进行中的手牌仍存在。
4. 结算后检查盈亏、买入记录及各自的私人手牌历史。另开浏览器不应读到原玩家的私人记录。
5. 开发者工具里确认页面使用 `https://`、实时连接使用 `wss://`。重启程序后玩家连接会重建；管理员会话在进程内，需重新登录。
6. 用同一手机浏览器关闭后重开邀请链接，检查原昵称、座位和手牌继续显示；玩家 Cookie 应有 30 天 `Max-Age`。隐私模式、其他浏览器及已丢失 Cookie 的玩家仍需恢复码。
7. 在测试桌检查行动者高亮、操作按钮及发两次时的两排公共牌和分池结果，尤其查看窄屏是否遮挡公共牌。

## 以后更新代码，不重建数据库

正式数据库不在代码目录里，但更新前仍应备份，并尽量等当前手牌结束后再重启。先在本地完成相关测试，核对目标提交**确实已上传到 GitHub**。

```bash
sudo systemctl start texas-holdem-backup.service
sudo git -C /opt/texas-holdem status --short --branch
sudo git -C /opt/texas-holdem pull --ff-only
sudo /opt/texas-holdem/.venv/bin/python -m pip install -r /opt/texas-holdem/backend/requirements.txt
sudo systemctl restart texas-holdem.service
systemctl is-active texas-holdem.service
```

`git status` 若出现未提交改动、`pull --ff-only` 若拒绝更新，先查明版本关系，不要强制覆盖。新代码启动时会执行数据库迁移，因此升级前的备份很重要。更新后检查 `/admin`、邀请链接、一次玩家操作和 `sudo journalctl -u texas-holdem.service -n 50 --no-pager`。

2026-09-26 的缓存修复当时尚未上传 GitHub，所以曾用 **Git bundle** 将提交送到服务器。以下是那次特殊更新的记录，后续目标提交已在 GitHub 时使用上面的快进更新流程。在本机项目根目录用 Git 创建包，Windows PowerShell 上传：

```powershell
git bundle create backups/admin-cache-fix.bundle main
scp backups/admin-cache-fix.bundle ubuntu@119.28.204.247:/tmp/admin-cache-fix.bundle
```

服务器先查看工作区是否干净，再取出包中提交并仅允许快进合并：

```bash
sudo git -C /opt/texas-holdem status --short --branch
sudo git -C /opt/texas-holdem fetch /tmp/admin-cache-fix.bundle main
sudo git -C /opt/texas-holdem merge --ff-only FETCH_HEAD
sudo systemctl restart texas-holdem.service
systemctl is-active texas-holdem.service
curl -fsS https://poker.8u7hybby.com/admin
```

这只是说明**当时已经执行的特殊更新路径**。当时重启前检查过数据库，没有进行中的手牌；合并后线上 HTML 含 `/assets/main.js?v=0.8.3`，对应资源返回 `Cache-Control: no-cache`。

## 本次部署遇到的问题及真正原因

| 现象 | 怎么排查、怎么解决 | 结论 |
| --- | --- | --- |
| DNSPod 已有 A 记录，第三方检测却提示域名 IP 获取失败 | 核对完整子域名、A 记录、NS；控制台显示 NS 与 DNSPod 所属服务器一致。待公共解析生效后重试 | 当时公共解析尚未返回目标 IP；没有证据把它归因于某一个具体配置错误 |
| 只开放 443 后仍需确认入口 | 逐层检查 DNS、80/443、Caddy 和本机服务 | 放行 443 本身不能证明域名、证书和代理都正常 |
| 管理页“我的牌局”每 3 秒闪一下 | 初版 JS 在轮询时重建列表，先改成数据相同就保留 DOM；线上接口连续 5 次、每次间隔 3 秒取样，房间数据完全相同。用户按 `Ctrl+F5` 后闪烁消失 | **最终确定浏览器还在执行旧版 JS 缓存**；不是牌桌逻辑不断更新数据 |
| 其他浏览器仍可能缓存旧脚本 | 给 JS/CSS 地址加 `?v=0.8.3`，让 `/assets/` 重新验证缓存；部署后验证 HTTPS 页面和响应头 | 新资源地址强制获取修复后的脚本，避免再要求每位用户手动强刷 |
| 管理员密码曾在聊天或命令输出中暴露 | 更换服务器配置中的管理员密码，不把真实值写进仓库和文档 | 已暴露的密码不再使用；查看配置时也不应打印密码 |
| 当时线上修复提交比 GitHub 新 | 以本地 Git 提交和 bundle 部署到服务器，核对运行版本；之后在内测版上传时同步 GitHub | 更新时须核对版本，不能假定 GitHub `main` 是当前线上最新版 |

## 真要恢复数据库时

普通更新**不需要恢复**。恢复会回到备份时点；之后的买入和手牌不会自动出现。先在临时路径演练：

```bash
cd /opt/texas-holdem
sudo -u texas-holdem /opt/texas-holdem/.venv/bin/python -B -m deploy.sqlite_data restore --backup /var/backups/texas-holdem/选定的备份.sqlite3 --target /var/lib/texas-holdem/演练恢复.sqlite3
```

把示例文件名换成真实备份文件名，目标必须**不存在**。检查演练库完整性与数据。正式恢复时：

1. `sudo systemctl stop texas-holdem.service`，再用 `sudo ls -la /var/lib/texas-holdem/` 检查文件。若有 `-wal`、`-shm` 或 `-journal`，先确认它们的来历和是否仍有进程连接，不要直接删除。
2. 为当前正式库另建受保护目录，例如 `sudo install -d -o texas-holdem -g texas-holdem -m 0700 /var/backups/texas-holdem/pre-restore`。确认该目录没有同名文件后，把旧库和确认需要保留的旁路文件移入该目录；例如没有旁路文件时，执行 `sudo mv -n /var/lib/texas-holdem/texas_holdem.sqlite3 /var/backups/texas-holdem/pre-restore/`。
3. 在 `/opt/texas-holdem` 下运行 `sudo -u texas-holdem /opt/texas-holdem/.venv/bin/python -B -m deploy.sqlite_data restore --backup /var/backups/texas-holdem/选定的备份.sqlite3 --target /var/lib/texas-holdem/texas_holdem.sqlite3`。工具会先验证备份，并拒绝覆盖已有目标。
4. 检查新库属主为 `texas-holdem`，再运行 `sudo systemctl start texas-holdem.service`、`systemctl is-active texas-holdem.service`，登录网页验收房间、买入和手牌记录。旧库至少保留到验收结束。
