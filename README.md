# 🌐 Tailscale VPN Node on GitHub Actions

Easily deploy a **temporary VPN exit node** using **Tailscale** + **GitHub Actions**.  
This setup allows you to route your device traffic through a GitHub runner (6 hours per workflow run).  

---

## 🚀 Features
- Deploys a **VPN exit node** in minutes  
- Works with **Tailscale app (Android, iOS, Windows, Linux, macOS)**  
- Optional **Telegram bot notifications**  
- Free to use with GitHub Actions  

---

## 🔑 Required Variables
Before running the workflow, go to your repository **Settings → Secrets → Actions** and add these:

| Variable Name       | Description |
|---------------------|-------------|
| `TAILSCALE_AUTHKEY` | Your reusable Tailscale auth key (from [Tailscale Admin Console](https://login.tailscale.com/admin/settings/keys)) |
| `TG_BOT_TOKEN`      | Your Telegram bot token (from [BotFather](https://t.me/BotFather)) |
| `TG_CHAT_ID`        | Your Telegram group/channel ID (starts with `-100...`) → Don’t forget to add the bot as **Admin** |

---

## ⚙️ Usage
1. Fork or import this repo into your GitHub account.  
2. Add the **secrets** listed above.  
3. Go to **Actions tab → Tailscale VPN Node → Run workflow**.  
4. In your **Tailscale Admin Console**, set the GitHub runner instance as **Exit Node**.  
5. On your **mobile/PC**, select this exit node in the Tailscale app.  

Now all your traffic will exit through the GitHub worker’s IP 🎉  

---

## 📡 Example
```bash
# On client with Tailscale CLI (Linux/macOS/Windows WSL)
tailscale up --exit-node=<worker_tailscale_ip>




```
🙏 Credits

Made with ❤️ by **Surya..!!!**  
For **learning & educational use only**```

