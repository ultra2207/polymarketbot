# PolymarketBot Cloud Deployment Runbook: OCI Always Free & AWS Free Tier

This guide provides end-to-end instructions for deploying the PolymarketBot high-frequency market making and settlement harvesting daemon to low-cost cloud virtual machines.

> [!IMPORTANT]
> **Zero-Docker Policy**: Docker daemon overhead (1.5–3.0 GB idle) causes severe out-of-memory (OOM) thrashing on free-tier cloud instances. PolymarketBot runs **bare-metal native Python managed via Astral `uv`**, consuming under 180 MB of RAM idle.

---

## Cloud Platform Comparison

| Cloud Provider | Tier | Recommended Region | Specs | Latency to CLOB | Monthly Cost | Deployment Priority |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Oracle Cloud (OCI)** | **Always Free** | **London (`uk-london-1`)** | Up to 4 OCPU ARM Ampere A1 (24 GB RAM) or 2 AMD Micro (1 GB RAM) | 1–4 ms | **$0 / month (Forever)** | **Primary (Deploy First)** |
| **Amazon Web Services (AWS)** | **12-Month Free** | **London (`eu-west-2`)** | 1 vCPU `t2.micro` or `t3.micro` (1 GB RAM, 30 GB EBS) | < 1 ms (Direct AWS AZ) | **$0 for 12 mos** | **Secondary / Fallback** |

---

## 1. Oracle Cloud Infrastructure (OCI) Always Free Setup (Primary)

OCI's Always Free tier is the gold standard for quantitative bots because it grants **up to 4 ARM Ampere cores and 24 GB of RAM** with zero monthly cost.

### Step 1.1: Launch OCI Compute Instance
1. Log into your Oracle Cloud Console.
2. In the top-right corner, ensure your region is set to **UK South (London)** (`uk-london-1`).
3. Navigate to **Compute** → **Instances** → **Create Instance**.
4. Configure the instance:
   - **Name**: `polymarket-bot-oci-london`
   - **Image**: **Ubuntu 24.04 LTS (Minimal or Standard)** or **Oracle Linux 9**
   - **Shape**:
     - *Preferred*: **Ampere (ARM)** → `VM.Standard.A1.Flex` (Allocate 2 OCPUs, 12 GB RAM, or up to 4 OCPUs, 24 GB RAM).
     - *Alternative*: **AMD (x86)** → `VM.Standard.E2.1.Micro` (1 OCPU, 1 GB RAM).
   - **Networking**: Select your default VCN and Public Subnet. Ensure **Assign a public IPv4 address** is checked.
   - **SSH Keys**: Download and save the generated private key (e.g. `ssh-key.key`) or upload your existing public key.
   - **Boot Volume**: 50 GB to 100 GB (Always Free tier includes up to 200 GB total storage across all instances).
5. Click **Create** and wait for the instance state to change to `RUNNING`.

### Step 1.2: Configure OCI Ingress / Security List
1. In the instance details, click on your **Virtual Cloud Network (VCN)** → **Security Lists** → **Default Security List**.
2. By default, SSH (Port 22) is open.
3. If you intend to run the real-time UI telemetry dashboard (`src/polymarketbot/ui/app.py`):
   - Add Ingress Rule:
     - **Source CIDR**: `0.0.0.0/0` (or your personal IP)
     - **IP Protocol**: TCP
     - **Destination Port Range**: `8501` (or your configured dashboard port)

### Step 1.3: SSH into your OCI Instance
```bash
# On your local machine (PowerShell or Bash)
ssh -i /path/to/ssh-key.key ubuntu@<YOUR_OCI_PUBLIC_IP>
```

---

## 2. AWS Free Tier Setup (`t2.micro` / `t3.micro`)

AWS `eu-west-2` (London) shares the exact AWS infrastructure where Polymarket's CLOB origin sits, delivering sub-millisecond network latency.

### Step 2.1: Launch AWS EC2 Instance
1. Log into AWS Management Console.
2. Set region in the header to **Europe (London) `eu-west-2`**.
3. Go to **EC2** → **Instances** → **Launch an instance**.
4. Configure:
   - **Name**: `polymarket-bot-aws-london`
   - **AMI**: **Ubuntu Server 24.04 LTS (HVM), SSD Volume Type** (64-bit x86).
   - **Instance type**: `t3.micro` (or `t2.micro` in regions where `t3` is not free-tier eligible).
   - **Key pair**: Select or create an RSA key pair (`.pem`).
   - **Network settings**:
     - Auto-assign public IP: **Enable**.
     - Security group: Allow SSH traffic from your IP (or `0.0.0.0/0`).
   - **Configure storage**: 30 GiB gp3 (maximum free tier root storage).
5. Click **Launch instance**.

### Step 2.2: SSH into your AWS Instance
```bash
ssh -i /path/to/key.pem ubuntu@<YOUR_AWS_PUBLIC_IP>
```

---

## 3. Automated One-Command Bot Deployment

Once inside your VM (either OCI or AWS), execute the automated deployment pipeline.

### Step 3.1: Clone Repository
```bash
# Install git if needed
sudo apt-get update && sudo apt-get install -y git

# Clone repository
git clone https://github.com/ultra2207/polymarketbot.git
cd polymarketbot
```

### Step 3.2: Run Idempotent Deployment Script
```bash
sudo bash deploy/deploy.sh
```

What `deploy.sh` automatically performs:
1. **Low-RAM Safeguard**: Detects if physical RAM is < 2 GB (such as AWS `t2.micro`). Automatically provisions and activates a 2 GB swap file with `swappiness=10` to eliminate OOM kills.
2. **Architecture Detection**: Supports both ARM64 (OCI Ampere A1) and x86_64 (AWS t2/t3 or OCI AMD).
3. **Astral `uv` Installation**: Installs the high-performance Rust-based Python manager into `/usr/local/bin/uv`.
4. **Isolated Service User**: Creates a dedicated system user `polymarketbot` with locked shell access.
5. **Syncs Dependencies**: Installs the production virtualenv at `/opt/polymarketbot/.venv` with zero dev dependencies.
6. **Systemd Service Registration**: Installs `/etc/systemd/system/polymarketbot.service` configured with resource quotas (`MemoryMax=1.5G`, `CPUQuota=95%`) and automatic restart on crash.

---

## 4. Live Configuration & Credentials

Edit the production environment file:
```bash
sudo nano /etc/polymarketbot/polymarketbot.env
```

Populate the required keys:
```ini
# ==============================================================================
# PolymarketBot Production Environment Configuration
# ==============================================================================

# Trading Mode: 'paper' (default) or 'live'
BOT_MODE=paper

# Polymarket Web3 / CLOB Credentials (Required for Live Trading)
POLYMARKET_PRIVATE_KEY=0xYOUR_POLYGON_PRIVATE_KEY
POLYMARKET_FUNDER_ADDRESS=0xYOUR_POLYGON_PUBLIC_ADDRESS
POLYMARKET_PROXY_WALLET=0xYOUR_POLYMARKET_PROXY_ADDRESS

# CLOB API Credentials (Generated via EIP-712 onboarding)
POLYMARKET_API_KEY=YOUR_CLOB_API_KEY
POLYMARKET_API_SECRET=YOUR_CLOB_API_SECRET
POLYMARKET_API_PASSPHRASE=YOUR_CLOB_API_PASSPHRASE

# Fast Polygon RPC (Private Alchemy / Infura recommended for production)
POLYGON_RPC_URL=https://polygon-mainnet.g.alchemy.com/v2/YOUR_ALCHEMY_KEY

# Low-Memory Optimization Limits
DUCKDB_MEMORY_LIMIT=1GB
DUCKDB_THREADS=2
PYTHONOPTIMIZE=1
```

Save and exit (`Ctrl+O`, `Enter`, `Ctrl+X`). Secure permissions:
```bash
sudo chmod 600 /etc/polymarketbot/polymarketbot.env
```

---

## 5. Controlling the Service via Systemd

### Start the Bot Daemon
```bash
sudo systemctl start polymarketbot
```

### Check Live Status
```bash
sudo systemctl status polymarketbot
```

### Stream Live Logs and Telemetry
```bash
# Real-time stdout/stderr stream from systemd journal
sudo journalctl -u polymarketbot -f -o cat

# Or view dedicated daily loguru log
tail -f /opt/polymarketbot/data/logs/*.log
```

### Restart / Stop
```bash
sudo systemctl restart polymarketbot
sudo systemctl stop polymarketbot
```

---

## 6. Verification Checklist

Before leaving the bot unattended:
- [ ] Check service is active: `systemctl is-active polymarketbot` returns `active`.
- [ ] Check memory usage: `systemctl status polymarketbot | grep Memory` shows < 250 MB.
- [ ] Verify NTP time sync: `timedatectl status` confirms `System clock synchronized: yes`.
- [ ] Verify network ping to Polymarket CLOB:
  ```bash
  curl -s -w "HTTP: %{http_code} | Time: %{time_total}s\n" -o /dev/null https://clob.polymarket.com/time
  ```
  Expected output: `HTTP: 200 | Time: 0.002s` (on London instances).
