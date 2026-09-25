# 部署准备与数据库备份

当前准备方案是一台 Linux 服务器运行一个 Uvicorn worker，由 Caddy 提供 HTTPS/WSS。域名和服务器尚未购买；`deploy/` 中的路径和域名都是将来上机时使用的模板，不会修改本机服务。

## 文件与配置

| 用途 | 服务器路径 | 仓库模板 |
| --- | --- | --- |
| 程序及虚拟环境 | `/opt/texas-holdem` | 当前仓库 |
| SQLite 数据库 | `/var/lib/texas-holdem/texas_holdem.sqlite3` | `deploy/texas-holdem.env.example` |
| 数据库备份 | `/var/backups/texas-holdem/` | `deploy/texas-holdem-backup.service` |
| 管理员密码等配置 | `/etc/texas-holdem/texas-holdem.env` | `deploy/texas-holdem.env.example` |
| 应用服务与定时备份 | `/etc/systemd/system/` | `deploy/*.service`、`deploy/*.timer` |
| HTTPS 入口 | `/etc/caddy/Caddyfile` | `deploy/Caddyfile.example` |

数据库和备份都在代码目录外，重新拉取代码或更新虚拟环境不会覆盖它们。二者均包含未公开底牌、玩家身份和历史记录，目录仅允许服务账号及受信任的管理员访问；备份也不能进入 Git 仓库或前端静态目录。

应用服务只监听 `127.0.0.1:8000`，明确使用一个 worker。当前 WebSocket 连接和管理员会话位于进程内；多 worker 会导致连接状态不一致。Caddy 统一代理 HTTP 和 WebSocket，并在有真实域名、DNS 指向服务器且 80/443 可达时提供 HTTPS。Uvicorn 只信任来自本机代理的转发头，以便正确识别 `https` 并设置 `Secure` Cookie。请勿将 Uvicorn 的 8000 端口直接向公网开放。

## 现在就能做的本地备份和恢复演练

在项目根目录运行，文件会留在 Git 忽略的 `backups/` 中：

```powershell
D:\miniconda\envs\Texas_Holdem\python.exe -B -m deploy.sqlite_data backup --source database/texas_holdem.sqlite3 --backup-dir backups
```

命令会输出带 UTC 时间戳的备份路径。将输出路径填到下面的 `--backup`，目标必须是**不存在的新路径**：

```powershell
D:\miniconda\envs\Texas_Holdem\python.exe -B -m deploy.sqlite_data restore --backup backups/texas_holdem-时间戳.sqlite3 --target backups/restore-check.sqlite3
```

工具先检查来源是完整的本项目数据库，再调用 SQLite 在线备份接口生成快照；写入临时文件、校验成功后才公布为 `.sqlite3`。备份时应用可以继续运行。恢复同样先校验备份，只允许写入空路径；已有目标库或残留 WAL/SHM/日志文件时会报错，不会覆盖。演练后可检查恢复库的 `PRAGMA quick_check`、房间数和手牌数，再清理临时演练库。备份文件应继续保留。

## 拿到 Linux 服务器以后

1. 安装 Python、Git 和 Caddy；创建不能登录的 `texas-holdem` 服务账号。把仓库放到 `/opt/texas-holdem`，在该目录建立 `.venv` 并安装 `backend/requirements.txt`。应用需要能读取代码和迁移脚本，不需要写入代码目录。
2. 创建 `/var/lib/texas-holdem` 和 `/var/backups/texas-holdem`，所有者为 `texas-holdem`，目录权限为 `0700`。复制环境变量模板到 `/etc/texas-holdem/texas-holdem.env`，权限为 `0600`，将空白的 `TEXAS_ADMIN_PASSWORD` 换成至少 12 位的独有强密码。这个真实文件不进 Git。
3. 如需迁移本地牌局，先用上面的工具制作备份，再通过安全方式传到服务器；在应用首次启动前，将它恢复到 `/var/lib/texas-holdem/texas_holdem.sqlite3`，并确保服务账号拥有该文件。若准备从空库开始，可让应用首次启动时自动建库。
4. 复制 `deploy/texas-holdem.service`、`deploy/texas-holdem-backup.service` 和 `deploy/texas-holdem-backup.timer` 到 `/etc/systemd/system/`；执行 `systemctl daemon-reload`，启用应用服务与备份 timer。timer 使用服务器本地时间每天 03:00 执行，错过时补跑。可以手动执行一次备份 service，检查输出文件和服务日志。
5. 域名解析到服务器后，把 `deploy/Caddyfile.example` 中的 `poker.example.com` 改为实际域名，再作为 Caddyfile 安装；开放公网 80/443，仅让 Caddy 对外。验证 `https://域名/admin`、邀请链接、WebSocket 重连以及登录后 Cookie 的 `Secure` 属性。

在常见的 systemd Linux 发行版上，完成系统包安装、并将本轮部署文件提交到 GitHub 后，从**空数据库开始**的命令骨架如下。执行前检查当前路径、账号和发行版差异；`texas-holdem.env` 中的管理员密码必须先填写，才能启动应用。若要迁移本地历史，必须在 `systemctl enable --now` 前先完成第 3 步的数据库恢复：

```bash
sudo useradd --system --home /nonexistent --shell /usr/sbin/nologin texas-holdem
sudo git clone https://github.com/1148802323/Texas_Holdem.git /opt/texas-holdem
sudo python3 -m venv /opt/texas-holdem/.venv
sudo /opt/texas-holdem/.venv/bin/python -m pip install -r /opt/texas-holdem/backend/requirements.txt
sudo install -d -o texas-holdem -g texas-holdem -m 0700 /var/lib/texas-holdem /var/backups/texas-holdem
sudo install -d -m 0700 /etc/texas-holdem
sudo install -m 0600 /opt/texas-holdem/deploy/texas-holdem.env.example /etc/texas-holdem/texas-holdem.env
sudoedit /etc/texas-holdem/texas-holdem.env
sudo install -m 0644 /opt/texas-holdem/deploy/texas-holdem.service /etc/systemd/system/
sudo install -m 0644 /opt/texas-holdem/deploy/texas-holdem-backup.service /etc/systemd/system/
sudo install -m 0644 /opt/texas-holdem/deploy/texas-holdem-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now texas-holdem.service texas-holdem-backup.timer
sudo systemctl start texas-holdem-backup.service
```

检查 `systemctl status texas-holdem.service texas-holdem-backup.timer`，以及 `journalctl -u texas-holdem-backup.service -n 30` 和备份目录中的新文件。Caddyfile 的真实域名填写后，先运行 `caddy validate --config /etc/caddy/Caddyfile`，再重新加载 Caddy。

模板假设 Caddy 与 Uvicorn 在同一台机器。若后来选择容器平台或其他反向代理，应按实际网络路径重新配置可信代理地址。备份目录目前不自动清理旧文件，应监控磁盘空间，并至少保存一份服务器外的加密备份；仅有服务器本机备份无法应对整机丢失。

## 从备份恢复正式数据库

先停止 `texas-holdem.service`，并确认没有其他进程连接数据库。将当前数据库文件**移到受保护的备份目录**，保留供回退，然后运行：

```bash
cd /opt/texas-holdem
/opt/texas-holdem/.venv/bin/python -B -m deploy.sqlite_data restore \
  --backup /var/backups/texas-holdem/选定的备份.sqlite3 \
  --target /var/lib/texas-holdem/texas_holdem.sqlite3
```

恢复目标必须不存在；若原数据库旁有 `-wal`、`-shm` 或 `-journal` 文件，先检查其来历并处理，不要直接删掉。确认新库的完整性、权限和房间数据后，再启动服务。恢复会回到该备份的时间点，之后发生的买入和手牌不会自动重现；因此应定期演练并根据可接受的数据损失窗口调整备份频率。
