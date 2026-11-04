# Setup: Proxmox + Tailscale

A small journal for you and your friends: habits, a daily dial, notes and photos, an SOS button and a shared radio.
It runs in one Proxmox container. **Only you and the friends you share it with can reach it**, through Tailscale. Nothing is on the public internet.

How people get in, two locks:
1. **Tailscale:** only your devices, plus devices of friends you shared the container with, can reach the address.
2. **The app's own login:** an account made with your setup code or an invite link.

---

## 1. Make a container (Proxmox web UI)

1. **local → CT Templates → Templates**: download `debian-12-standard`.
2. **Create CT**:
   - Unprivileged: **on** (default)
   - Template: the Debian 12 one
   - Disk: **8 GB** on your Storage
   - CPU: **1 core**
   - Memory: **512 MB** (swap 256)
   - Network: DHCP
3. Don't start it yet. Note its ID, e.g. `105`.

## 2. Let Tailscale run inside the container

An unprivileged container can't use the TUN device by default.

On the **Proxmox host** shell:

```sh
nano /etc/pve/lxc/105.conf        # use your CT ID
```

Add these two lines at the end:

```
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
```

Then start the container: `pct start 105`.

## 3. Install Tailscale in the container

In the **container console**:

```sh
apt update && apt install -y curl python3
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up --hostname=habituality
```

- Open the login link it prints. Sign in with your preffered method of choice.
  - No new Tailscale account: the container joins your existing tailnet as one more device.
- `habituality` now shows up in the Tailscale app on your phone, next to your other devices.
- Admin console → **Machines** → `habituality` → **⋯** → **Disable key expiry**.
  - Otherwise its login runs out after about 180 days, and the site goes offline for everyone until you log it in again.

## 4. Copy the app in and install it

**Easiest: straight from GitHub.** Run this in the **container console**:

```sh
apt install -y git
git clone https://github.com/slarem1711/habituality.git /root/habituality
cd /root/habituality
sh install.sh
```

In the **container console**:

```sh
cd /root
python3 -m zipfile -e habituality-server.zip .
cd habituality-server
sh install.sh
```

- It prints a **setup code**, e.g. `5CFNN-229ER`. Keep it; you need it once.
- Quick check: `curl -s localhost:8080/api/me` should print `{"code":"unauthenticated","setup":true}`.

## 5. Give it an https address in your tailnet (Serve)

1. Tailscale admin console → **DNS**:
   - MagicDNS: on
   - HTTPS Certificates: **Enable**
2. In the container:

   ```sh
   tailscale serve --bg 8080
   tailscale serve status
   ```

   - `--bg` keeps it on after reboots.
   - `status` shows the address, like `https://habituality.tail1a2b3c.ts.net`.
     - The middle part is your tailnet's DNS name, shown on the **DNS** page of the admin console.
   - Serve is **private**: it only answers devices in your tailnet and devices you share it with.

## 6. Your account

1. On your phone, keep Tailscale **on** and open the address from step 5.
2. Create your account with the setup code. You become the owner.
3. Tip: Safari → Share → **Add to Home Screen**, so it opens like an app.

## 7. Add a friend (repeat per friend)

You send each friend **two links**:

1. **Tailscale share link** (gets them through lock 1):
   - Admin console → **Machines** → `habituality` → **⋯** → **Share…**
   - Share by email, or copy the link.
   - Sharing **doesn't** make them a user of your tailnet. They only get this one machine.
2. **App invite link** (gets them through lock 2):
   - In the app, tap your round letter (top right) → **Invite a friend** → **Copy link**.
   - Each invite link works once, for 7 days.

Then send them the message below.

### Message to send a friend

> 1. Install **Tailscale** (App Store / Play Store, or tailscale.com/download on a PC).
> 2. Sign in with Google, Apple, Microsoft or GitHub. Any account works.
> 3. Open this share link and tap **Accept**: `<Tailscale share link>`
> 4. Turn Tailscale **on**, then open: `<app invite link>`
> 5. Pick a name, username and a password (12+ characters, e.g. 3–4 random words).
> 6. Next time, just open `https://habituality.<…>.ts.net` with Tailscale on. Add it to your home screen.
>
> Tailscale only sends traffic for my server through it. Your normal internet stays the same. If you use another VPN, you may need to switch between them.


## Day-to-day commands (in the container)

| What | Command |
|---|---|
| Is it running? | `systemctl status habituality` |
| Live log | `journalctl -u habituality -f` |
| New invite code | `habituality invite` |
| List accounts | `habituality users` |
| Reset a password (also signs that person out everywhere) | `habituality passwd <username>` |
| Show setup code again | `habituality setup-code` |
| See blocked addresses | `habituality bans` |
| Unblock one | `habituality unban <address>` |
| Unblock everyone | `habituality unban all` |
| Restart | `systemctl restart habituality` |

## Update to a new version

- **From GitHub:** `cd /root/habituality && git pull && sh install.sh`
- **With a zip:** copy the new folder in (step 4) and run `sh install.sh` again.
Your data in `/var/lib/habituality` stays.

## Backups

- **Easiest:** Proxmox → **Datacenter → Backup → Add**
  - Pick this CT.
  - Storage: your SATA backup disk.
  - Schedule: daily.
  - Mode: snapshot.
- **Just the data:** `habituality backup` writes a copy to `/var/lib/habituality/backups`.

## Remove someone or turn it off

| What | How |
|---|---|
| A friend's access to the server | Admin console → Machines → `habituality` → Share… → remove them |
| A friend's app account | `habituality passwd <username>` with a random password signs them out and locks them out |
| Turn the address off | `tailscale serve reset` |
| Stop the app | `systemctl stop habituality` |

## Plan B: a friend who really can't install Tailscale

`tailscale funnel --bg --https=443 localhost:8080` makes the same address **public on the internet**.

- Everyone can then reach the sign-in page, including bots. The app's login, password rules and IP blocking still protect it.
- Funnel needs approval the first time (the command prints a link).
- Back to private: `tailscale funnel reset`, then `tailscale serve --bg 8080`.

## Good to know

- **Two locks:** Tailscale decides who can reach the site at all, then the app login decides who gets in.
- **Blocking:** 10 wrong tries in a row from one address (wrong password or wrong invite code) blocks that address from signing in.
  - Addresses look like `100.x.y.z` (a Tailscale address).
  - A correct login resets the count. Blocks stay until `habituality unban`.
  - Each block shows up in the log: `journalctl -u habituality | grep BLOCKED`.
- **Passwords:** at least 12 characters, not on the common list, not a keyboard or number run, and no name or username inside.
- The app only listens on `127.0.0.1:8080` inside the container. Tailscale Serve is the only way in.
- Data lives in `/var/lib/habituality`:
  - `habituality.db`: entries, accounts
  - `files/`: photos, songs
- Limits:
  - Uploads: 25 MB each.
  - Storage: 1 GB per person.
  - Accepted: photos (JPG, PNG, WebP, GIF) and songs (MP3, M4A, OGG, WAV, FLAC). No SVG.
- "Write today, see all" is a rule in the page, not a lock on the server. Fine among friends; just know it.

## If something breaks

| What you see | What to do |
|---|---|
| `tailscale up` says `/dev/net/tun` is missing | Step 2 lines are missing, or the CT wasn't restarted after adding them. |
| `install.sh` says the service didn't start | Run `journalctl -u habituality -n 50`. |
| `tailscale serve` asks about HTTPS | Turn on HTTPS Certificates (step 5.1), then run it again. |
| A friend's page doesn't load | Is Tailscale **on**? Did they accept the share? Are they using the full `…ts.net` address (short names don't work for shared machines)? Is MagicDNS on in **their** admin console? |
| Friend's invite "doesn't work" | It was used or is older than 7 days. Make a new one. |
| Friend sees "Too many wrong tries from your network (100.x.y.z)" | `habituality unban 100.x.y.z` |
