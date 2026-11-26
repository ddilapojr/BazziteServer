# 🎮 Bazzite Integration Server

A FastAPI server that integrates your Bazzite gaming PC with Home Assistant!

It provides:

✅ Game detection  
✅ CPU temperature monitoring  
✅ Remote shutdown & sleep commands  
✅ WebSocket-based push updates

---

## ⚡️ Quickstart (recommended)

On your Bazzite PC:

```bash
git clone https://github.com/Tijgerd/BazziteServer.git
cd BazziteServer
chmod +x install.sh
./install.sh
```

The installer creates a Python virtual environment, installs the dependencies, and sets up a user systemd service that starts the server.

After starting, the server will listen on:

```
ws://YOUR-PC-IP:5000/ws
```

Check that it's running:

```bash
systemctl --user status bazzite-server
journalctl --user -u bazzite-server -f
```

> **Note:** The server listens on all network interfaces (port 5000). Only use it on a trusted local network and don't expose it to the internet.
