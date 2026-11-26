# 🎮 Bazzite Integration Server

A FastAPI server that integrates your Bazzite gaming PC with Home Assistant!

It provides:

✅ Game and app detection  
✅ CPU temperature monitoring  
✅ Sleep & shutdown announcements to Home Assistant  
✅ Remote shutdown & sleep commands  
✅ WebSocket-based push updates

---

## ⚡️ Quickstart (recommended)

On your Bazzite PC, copy and paste:

```bash
git clone https://github.com/ddilapojr/bazzite-server.git ~/BazziteServer && cd ~/BazziteServer && chmod +x install.sh && ./install.sh
```

----

After starting, the server will listen on:
ws://YOUR-PC-IP:5000/ws

To update:

```bash
cd ~/BazziteServer && git pull && systemctl --user restart bazzite-server
```

---

## 🙏 Credits

- [Tijgerd](https://github.com/Tijgerd) created the original [Bazzite Integration Server](https://github.com/Tijgerd/BazziteServer) and [Home Assistant integration](https://github.com/Tijgerd/BazziteHaIntegration).
- [bendamschke](https://github.com/bendamschke) contributed the added sensors.
