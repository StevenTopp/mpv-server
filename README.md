# SyncTV Mine

轻量版同步观影工具，保留房间同步、播放控制、弹幕，并加入 Bilibili / Alist 资源播放。

## 运行

```bash
cd /mnt/d/Code/synctv_mine
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 9997
```

如果 9997 被占用，可以换端口：

```bash
uvicorn main:app --host 127.0.0.1 --port 10097
```

## 功能

- 创建/加入房间
- 同步播放、暂停、跳转
- 本地视频和远程直链播放
- Bilibili 扫码登录，解析 BV 号或视频链接后共享到房间
- Alist 登录、目录浏览、文件播放
- 弹幕广播

第三方账号凭据保存在 `data/vendor_state.json`。这是轻量本地版本，没有做多用户权限隔离，适合个人或可信环境使用。
