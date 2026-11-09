============================================================
 TempMail v2.2.1  源码包（app.py 直接运行 / Docker 构建）
============================================================

 目录内容
 ----------------------------------------------------------
  app.py               主程序（Flask + aiosmtpd + SQLite，单文件）
  requirements.txt     Python 依赖（flask / aiosmtpd）
  Dockerfile           Docker 镜像定义
  docker-compose.yml   Docker Compose 编排
  mail.php             部署回传接收端（放到 ac.atusu.cn）
  LICENSE              非商业许可
  install.sh           一键部署 / 管理（venv + systemd）
  docker.sh            Docker 部署 / 管理

 方式一：直接部署（需要 python3 >= 3.8）
 ----------------------------------------------------------
  chmod +x install.sh docker.sh
  sudo bash install.sh            # 首次部署：检测 python3 -> 建 venv -> 装依赖 -> systemd 启动
  sudo bash install.sh            # 再次执行 = 管理菜单

  国内加速 / 换端口：
  sudo bash install.sh install --mirror https://pypi.tuna.tsinghua.edu.cn/simple
  sudo bash install.sh install --web-port 8080 --smtp-port 25

  安装位置 : /opt/temp-mail（依赖在 venv/，数据在 data/）
  服务名   : temp-mail（开机自启、崩溃自动重启）
  默认端口 : Web 6060 / SMTP 25
  端口占用 : install.sh 自动处理 —— 本程序旧实例占用则先停止复用；
             其它程序占用则自动顺延到空闲端口，并写回 .env，结束时打印实际端口

 方式二：Docker 部署（目标机不需要 Python）
 ----------------------------------------------------------
  chmod +x docker.sh
  sudo bash docker.sh             # 用 app.py + Dockerfile 构建镜像并启动
  sudo bash docker.sh rebuild     # 改了 app.py 后重建
  sudo bash docker.sh status      # 状态 / 访问地址
  sudo bash docker.sh down        # 停止并删除容器（数据保留）

 方式三：Docker Compose
 ----------------------------------------------------------
  docker compose up -d
  （与脚本二选一，不要同时管理同一个容器）

 部署完成后
 ----------------------------------------------------------
  1) 浏览器打开 http://服务器IP:6060 进入配置页
  2) 按页面提示添加 A 记录（指向服务器公网 IP）与 MX 记录
  3) 反代到本机 Web 端口，并透传 Host 头
  4) 在配置页填写主域名保存，解析生效后「下滑到页面底部」点「前往邮箱」按钮进入
  5) 反代端口以配置页显示的为准（端口被占用时程序会换成实际端口）

 常见问题
 ----------------------------------------------------------
  ./install.sh 报 ": No such file or directory"
      文件被转成了 CRLF 换行，修复：sed -i 's/\r$//' install.sh docker.sh
  依赖装不上
      国内服务器加 --mirror 指定 pip 镜像，或改用 docker.sh 部署
  提示系统缺少 pip / venv
      apt install -y python3 python3-venv   或   yum install -y python3
      （也可以直接用 docker.sh，容器里已带 Python）

 授权
 ----------------------------------------------------------
  开源版 · 禁止商用 · 开发者 atusu.cn/mail
  Copyright (C) 2026 atusu.cn
