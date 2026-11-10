# Running the bot 24/7 for free

The bot needs about 550 MB of RAM, very little CPU, a few MB of disk per day and **no open ports**
(it only makes outgoing connections). Any always-on Linux machine works. Two free options:

| | Oracle Cloud Always Free (recommended) | Google Cloud e2-micro |
|---|---|---|
| Machine | ARM "Ampere A1" VM, up to 2 cores / 12 GB RAM free | 2 shared vCPU, 1 GB RAM (swap added automatically) |
| Where | Any region (pick one close to you) | Only us-west1, us-central1, us-east1 |
| Card | Needed for identity check, not charged | Needed for identity check, not charged |
| Watch out | Idle free VMs can be reclaimed — upgrading the account to Pay-As-You-Go avoids this and stays free within the limits | Only one free e2-micro per account; use a standard persistent disk |

> Only **one copy** of the bot may run at a time (Telegram allows one connection per token).
> Stop the copy on your PC before starting the server.

## 1. Create the server

**Oracle Cloud**
1. Sign up at <https://www.oracle.com/cloud/free/> and pick a home region near you.
2. *Compute → Instances → Create instance*.
3. *Image*: **Ubuntu 24.04**. *Shape*: **Ampere → VM.Standard.A1.Flex**, 1 OCPU, 6 GB RAM.
   If it says "out of capacity", try another availability domain or try again later.
4. *Add SSH keys*: **Generate a key pair for me** → download the private key.
5. *Create*. Copy the **public IP address** shown on the instance page.

**Google Cloud**
1. Sign up at <https://cloud.google.com/free>.
2. *Compute Engine → VM instances → Create instance*: region **us-central1** (or us-west1 / us-east1),
   machine type **e2-micro**, boot disk **Ubuntu 24.04, standard persistent disk, 30 GB**.
3. *Create*, then use the **SSH** button in the console (no key needed).

## 2. Connect

From Windows PowerShell (Oracle; the user is `ubuntu`):

```powershell
ssh -i C:\path\to\ssh-key.key ubuntu@<PUBLIC_IP>
```

If Windows complains that the key is "too open": right-click the key file → *Properties → Security →
Advanced → Disable inheritance → remove all users except yourself*.

## 3. Install the bot (one command)

```bash
curl -fsSL https://raw.githubusercontent.com/MeneryOLD/Crypto-assistant/main/deploy/install.sh | bash
```

It installs Docker, downloads the bot, asks for your bot token, adds swap on small machines and starts the
bot. It restarts automatically after crashes and reboots. Run the **same command again to update**.

Optional: put your Telegram id in `ADMIN_IDS` (`nano ~/vexora/.env`, then re-run the command) to unlock `/admin`.

## 4. Keep your history (optional)

To move the database from your PC (news, price memory, your settings), stop the bot on the server, copy
the file, start it again:

```powershell
# on your PC, after stopping the local bot
scp -i C:\path\to\ssh-key.key data\bot.db ubuntu@<PUBLIC_IP>:~/vexora/data/bot.db
```

```bash
# on the server
sudo chown 1000:1000 ~/vexora/data/bot.db && sudo docker compose -f ~/vexora/compose.yaml restart
```

## Everyday commands (on the server)

| What | Command |
|---|---|
| Live logs | `sudo docker compose -f ~/vexora/compose.yaml logs -f --tail 50` |
| Is it running? | `sudo docker compose -f ~/vexora/compose.yaml ps` |
| Restart | `sudo docker compose -f ~/vexora/compose.yaml restart` |
| Stop | `sudo docker compose -f ~/vexora/compose.yaml down` |
| Update to the latest version | re-run the install command |
| Back up the database | `cp ~/vexora/data/bot.db ~/bot-backup-$(date +%F).db` |
