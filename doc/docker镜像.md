# 运行下面的2条命令，在linux系统上, Mac或者windows，最好手动编辑daemon.json
自定义成国内的镜像

```
sudo tee /etc/docker/daemon.json > /dev/null <<EOF
{
  "registry-mirrors": [
    "https://docker.1panel.live",
    "https://hub.rat.dev",
    "https://dockerpull.org",
    "https://docker.m.daocloud.io"
  ]
}
EOF
```

```
sudo systemctl daemon-reload && sudo systemctl restart docker
```
