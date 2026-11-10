# PolymarketBot - Cloud Deployment Guide

A complete, production-tested guide for running **PolymarketBot** natively on Linux Cloud Virtual Machines.

This guide provides targeted instructions for:
1. **Primary Deployment**: **Oracle Cloud Infrastructure (OCI) Always Free Tier** (Region: **`eu-amsterdam-1`**, Amsterdam)
2. **Secondary / Fallback**: **AWS 12-Month Free Tier** (Region: `eu-central-1`, Frankfurt)
3. **Alternative Budget VPS**: **Hetzner Cloud** (CX22 / CPX11) or **DigitalOcean**

> [!IMPORTANT]
> **Why Oracle Cloud Amsterdam (`eu-amsterdam-1`) is Our Designated Target:**
> 1. **UK IP Geoblock**: Polymarket restricts and geoblocks UK IP addresses. Deploying in London (`uk-london-1` or AWS `eu-west-2`) risks account restriction or API 403 geoblock errors. Amsterdam is an EU hub with zero geoblock issues.
> 2. **Network Latency & Direct Peering**: Amsterdam is home to AMS-IX (one of the largest internet exchanges globally) and direct low-latency Cloudflare/Polymarket transit paths. Benchmarks show `eu-amsterdam-1` (OCI) provides faster and more predictable round-trip latency than AWS Dublin (`eu-west-1`) and AWS London (`eu-west-2`).
> 3. **Cost & Hardware Superiority**: OCI Always Free provides up to **4 ARM Ampere cores and 24 GB RAM for $0/month forever**. Because OCI `eu-amsterdam-1` is both faster and provides 24x the RAM of AWS free tier (1 GB), it is definitely not worth paying for AWS.

---

## 1. Architecture: Native `uv` Deployment (Zero Docker Overhead)

### Why NO Docker?
Free tier and low-cost cloud instances operate with strictly constrained RAM:
- **OCI E2.1.Micro**: 1 GB RAM
- **AWS t2.micro / t3.micro**: 1 GB RAM
- **Hetzner CX22 / CPX11**: 2 GB – 4 GB RAM

The Docker daemon (`dockerd`, `containerd`, shim processes, bridge networking, and layer overhead) routinely consumes 350 MB–800 MB of idle memory. On a 1 GB RAM VM, Docker alone will trigger the Linux kernel Out-Of-Memory (OOM) killer before the bot even initializes!

### The Native Solution: Astral `uv` + `systemd`
- **Zero Daemon Overhead**: Standalone binary with 0 MB idle footprint.
- **Fast Builds & Upgrades**: `uv sync --upgrade --no-dev` installs and updates dependencies directly.
- **Universal Architecture Support**: Native precompiled binaries for both **ARM64** (`aarch64` Ampere A1) and **x86_64** (Intel/AMD).
- **Resource Hardening**: Process isolation managed directly by Linux `systemd`:
  - `MemoryHigh=1.2G`, `MemoryMax=1.5G`, `MemorySwapMax=0`
  - `CPUQuota=95%`
  - Low-memory DuckDB guard: `DUCKDB_MEMORY_LIMIT=1GB`, `DUCKDB_THREADS=2`
  - C memory allocator tuning: `MALLOC_ARENA_MAX=2`

---

## 2. Platform Comparison

| Cloud Provider | Instance Shape | Architecture | vCPU / RAM | Monthly Cost | Primary Region |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Oracle Cloud (OCI)** | `VM.Standard.A1.Flex` | **ARM64** (Ampere) | Up to 4 OCPUs / 24 GB RAM | **$0.00 (Always Free)** | **`eu-amsterdam-1` (Primary)** |
| **Oracle Cloud (OCI)** | `VM.Standard.E2.1.Micro` | **x86_64** (AMD) | 1 OCPU (shared) / 1 GB RAM | **$0.00 (Always Free)** | **`eu-amsterdam-1`** |
| **AWS Free Tier** | `t2.micro` or `t3.micro` | **x86_64** (Intel/AMD) | 1–2 vCPU / 1 GB RAM | **$0.00 (12 Months)** | `eu-central-1` (Frankfurt) |
| **Hetzner Cloud** | `CX22` / `CPX11` | **x86_64** (Intel/AMD) | 2 vCPU / 2–4 GB RAM | **~€3.79 – €4.50/mo** | Falkenstein / Helsinki |

---

## 3. Deployment Walkthrough: Oracle Cloud (OCI) in `eu-amsterdam-1`

### Step 3.1: Create Compute Instance
1. Log in to [Oracle Cloud Console](https://cloud.oracle.com).
2. Set region selector (top-right) to **Netherlands Northwest (Amsterdam)** (`eu-amsterdam-1`).
3. Open navigation menu (top-left) -> **Compute** -> **Instances** -> **Create Instance**.
4. **Name**: `polymarketbot-node1`
5. **Placement & Availability Domain**: Select any available AD (e.g. `AD-1` or `AD-2`).
6. **Image & Shape**:
   - **Image**: Click *Change Image* -> Choose **Ubuntu 24.04 LTS** (or Oracle Linux 9).
   - **Shape**: Click *Change Shape*:
     - **Recommended (Top Choice)**: Select **Ampere (Arm-based Processor)** -> `VM.Standard.A1.Flex`. Allocate **2 to 4 OCPUs** and **12 to 24 GB RAM** (Always Free Eligible).
     - *Fallback if Ampere is out of capacity*: Select **Specialty and Previous Generation** -> `VM.Standard.E2.1.Micro` (1 OCPU, 1 GB RAM, Always Free).
7. **Networking**: Choose or create a Virtual Cloud Network (VCN) with a Public Subnet. Ensure **Assign a public IPv4 address** is checked.
8. **Add SSH Keys**: Generate a key pair or upload your public key (`~/.ssh/id_ed25519.pub`).
9. Click **Create** and wait 60–90 seconds for status to show **Running**.

### Step 3.2: OCI VCN Security List (Firewall)
PolymarketBot only initiates **outbound** client connections (HTTPS and WSS to Polymarket CLOB, Polygon RPCs, Kalshi). It does not need public listening server ports.
1. In OCI Console: **Networking** -> **Virtual Cloud Networks** -> Click your VCN -> **Security Lists** -> Click **Default Security List for [VCN]**.
2. **Ingress Rules**:
   - Verify Port `22` (SSH) is permitted (restrict Source CIDR to your home IP for best security, or `0.0.0.0/0` with key authentication).
3. **Egress Rules**:
   - Destination `0.0.0.0/0`, All Protocols: Allowed (Default).

### Step 3.3: Host-Level Firewall Adjustment (OCI Ubuntu/Oracle Linux)
Oracle Cloud images include host firewall filters (`iptables` / `firewalld`) that may block traffic. Connect via SSH and run:

```bash
ssh -i ~/.ssh/id_ed25519 ubuntu@YOUR_OCI_PUBLIC_IP

# If on Ubuntu:
sudo iptables -P INPUT ACCEPT
sudo iptables -P FORWARD ACCEPT
sudo iptables -P OUTPUT ACCEPT
sudo iptables -F
sudo netfilter-persistent save 2>/dev/null || true

# If on Oracle Linux (opc user):
# sudo firewall-cmd --permanent --zone=public --add-service=ssh
# sudo firewall-cmd --reload
```

---

## 4. Deployment Walkthrough: AWS Free Tier in `eu-west-2` (London)

### Step 4.1: Launch EC2 Instance
1. Log in to [AWS Management Console](https://console.aws.amazon.com/ec2).
2. Set region selector (top-right) to **Europe (London) `eu-west-2`**.
3. Navigate to **EC2 Dashboard** -> **Launch Instance**.
4. **Name**: `polymarketbot-aws`
5. **Application and OS Images (AMI)**: Choose **Ubuntu Server 24.04 LTS (HVM), SSD Volume Type** (64-bit x86).
6. **Instance Type**:
   - Choose **`t2.micro`** or **`t3.micro`** (Free tier eligible, 1 vCPU, 1 GiB RAM).
7. **Key Pair**:
   - Select an existing key pair or click **Create new key pair** (Type: `ED25519` or `RSA`, Name: `polymarket-aws-key`). Save the downloaded `.pem` file to `~/.ssh/polymarket-aws-key.pem`.
8. **Network Settings**:
   - Create Security Group: Check **Allow SSH traffic from Anywhere** (or **My IP**).
   - Ensure **Auto-assign public IP** is **Enable**.
9. **Configure Storage**: Set Root volume to **20 GiB or 30 GiB gp3** (AWS Free Tier includes up to 30 GB EBS).
10. Click **Launch Instance**.

### Step 4.2: Connect to AWS Instance
Ensure your key permissions are locked down before connecting:
```bash
chmod 400 ~/.ssh/polymarket-aws-key.pem
ssh -i ~/.ssh/polymarket-aws-key.pem ubuntu@YOUR_AWS_PUBLIC_IP
```

---

## 5. Universal Automated Setup: `deploy/deploy.sh`

Whether on Oracle Cloud (ARM64 or x86) or AWS (x86_64), deployment is executed using the identical automated, idempotent script.

### Step 5.1: Clone and Run Deployment Script
```bash
# 1. Clone repository
git clone https://github.com/ultra2207/polymarketbot.git
cd polymarketbot

# 2. Run the deployment automation
sudo bash deploy/deploy.sh
```

### What `deploy.sh` performs automatically:
1. **Detects CPU Architecture**: Supports both `aarch64` (OCI Ampere ARM) and `x86_64` (AWS `t2.micro` / OCI `E2.1.Micro`).
2. **Detects Low RAM (< 2GB)**: On 1GB instances (AWS `t2/t3.micro` or OCI `E2.1.Micro`), automatically provisions a **2GB swapfile** (`/swapfile`) with `vm.swappiness=10`. This prevents OOM errors during builds.
3. **Installs System Tools**: `curl`, `git`, `build-essential` / `gcc`, `rsync`.
4. **Installs Standalone Astral `uv`**: Fetches the official release directly into `/usr/local/bin/uv`.
5. **Configures Sandboxed User**: Creates unprivileged service account `polymarketbot`.
6. **Synchronizes Codebase**: Copies files cleanly to `/opt/polymarketbot`.
7. **Builds Virtual Environment**: Runs `uv sync --no-dev` inside `/opt/polymarketbot`.
8. **Hardens File Permissions**: Restricts access (`chmod 750 /opt/polymarketbot`, `chmod 600 /etc/polymarketbot/polymarketbot.env`).
9. **Installs Systemd Service**: Copies `/opt/polymarketbot/deploy/polymarketbot.service` to `/etc/systemd/system/` and enables it at system boot.

---

## 6. Configuring Credentials & Secrets

Edit the production environment file:
```bash
sudo nano /etc/polymarketbot/polymarketbot.env
```

Populate the required credentials:
```ini
# ==============================================================================
# PolymarketBot Production Credentials (/etc/polymarketbot/polymarketbot.env)
# ==============================================================================

# 1. Polymarket CLOB L2/L3 Credentials
POLY_API_KEY=your_api_key_here
POLY_SECRET=your_api_secret_here
POLY_PASSPHRASE=your_passphrase_here

# 2. Polygon Signer Key & Maker Collateral Wallet
PRIVATE_KEY=your_64_char_hex_private_key
PROXY_WALLET_ADDRESS=your_maker_or_deposit_wallet_address

# Signature Type: 0 = EOA, 1 = Magic Proxy, 2 = Gnosis Safe, 3 = Deposit Wallet ERC-1271
POLY_SIGNATURE_TYPE=0

# 3. Polygon PoS RPC (Dedicated RPC recommended, e.g. Alchemy or QuickNode)
POLYGON_RPC_URL=https://polygon-mainnet.g.alchemy.com/v2/YOUR_ALCHEMY_KEY

# 4. Low-RAM VPS Guardrails (Preconfigured defaults)
DUCKDB_MEMORY_LIMIT=1GB
DUCKDB_THREADS=2
PYTHONUNBUFFERED=1
PYTHONOPTIMIZE=1
MALLOC_ARENA_MAX=2
```

Save and ensure strict permissions:
```bash
sudo chmod 600 /etc/polymarketbot/polymarketbot.env
sudo chown polymarketbot:polymarketbot /etc/polymarketbot/polymarketbot.env
```

---

## 7. Starting the Bot & Selecting Execution Modes

### Start the Service:
```bash
sudo systemctl start polymarketbot
```

### Check Service Status:
```bash
sudo systemctl status polymarketbot
```

### View Live Real-Time Logs:
```bash
sudo journalctl -u polymarketbot -f -o cat
```

---

### Selecting Execution Modes

By default (leading to December 1st live launch), `polymarketbot.service` executes the **24/7 Live Paper Soak Daemon** across the 3 active production pillars ($1,000 each = $3,000 total):
```ini
ExecStart=/opt/polymarketbot/.venv/bin/python -m polymarketbot.paper.main --capital 3000
```

### Preflight Health Audit

The standalone `polymarketbot.tools.preflight` CLI was removed with the dead
tooling; the engine's built-in `preflight_check()` runs inside `paper.main` and
the live runner before they trade.

To switch to a different operational mode, run:
```bash
sudo systemctl edit polymarketbot
```
Paste your override in the opened editor:

```ini
[Service]
ExecStart=
# --- Option 1: 3-Strategy Live Paper Soak Daemon ($3,000 virtual capital) ---
ExecStart=/opt/polymarketbot/.venv/bin/python -m polymarketbot.paper.main --capital 3000

# --- Option 2: Live Real Money Quoter (BLOCKED until Dec 1st - requires real capital) ---
# ExecStart=/opt/polymarketbot/.venv/bin/python -m polymarketbot.trading.runner --live --yes --budget-pct 0.20
```

Reload and restart:
```bash
sudo systemctl daemon-reload
sudo systemctl restart polymarketbot
```

---

## 8. Ongoing Monitoring & Operations

### Resource Monitoring:
```bash
# Process CPU and Memory breakdown
htop

# Systemd cgroup slice metrics (shows exact MemoryMax usage)
systemd-cgtop
```

### Updating to the Latest Code:
```bash
cd /opt/polymarketbot
sudo git pull origin master
sudo -u polymarketbot uv sync --no-dev
sudo systemctl restart polymarketbot
```

### Restarting & Stopping:
```bash
sudo systemctl restart polymarketbot
sudo systemctl stop polymarketbot
```
