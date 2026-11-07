#!/usr/bin/env bash
# ==============================================================================
# PolymarketBot - Unified Production Cloud Deployment Script
# ==============================================================================
# Target OS: Ubuntu (22.04/24.04), Debian (11/12), Oracle Linux (8/9), RHEL/Rocky
# Design: Fully idempotent, lightweight (no Docker), native Python with astral 'uv'
#
# Usage:
#   sudo bash deploy/deploy.sh
# ==============================================================================

set -euo pipefail

# Colors for terminal output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1" >&2
}

log_step() {
    echo -e "\n${BLUE}==>${NC} ${GREEN}$1${NC}"
}

# ------------------------------------------------------------------------------
# 1. Preflight Checks & Privilege Verification
# ------------------------------------------------------------------------------
log_step "1/8 Checking permissions and execution environment..."

if [ "$EUID" -ne 0 ]; then
    log_error "This script must be run as root (or with sudo)."
    exit 1
fi

APP_DIR="/opt/polymarketbot"
APP_USER="polymarketbot"
APP_GROUP="polymarketbot"
ETC_DIR="/etc/polymarketbot"
VAR_LOG_DIR="/var/log/polymarketbot"
VAR_LIB_DIR="/var/lib/polymarketbot"
SCRIPT_SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Detect distribution family and CPU architecture
ARCH=$(uname -m)
log_info "Detected Architecture: $ARCH (ARM64: aarch64, x86_64: AMD/Intel)"

if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS_NAME=$ID
    OS_VERSION=$VERSION_ID
    log_info "Detected OS: $PRETTY_NAME ($OS_NAME $OS_VERSION)"
else
    log_warn "Unknown Linux distribution; assuming standard POSIX layout."
    OS_NAME="unknown"
fi

# ------------------------------------------------------------------------------
# 2. Low-RAM Protection: Configure Swap if RAM < 2GB
# ------------------------------------------------------------------------------
log_step "2/8 Checking system memory & swap allocation..."

TOTAL_RAM_MB=$(free -m | awk '/^Mem:/{print $2}')
TOTAL_SWAP_MB=$(free -m | awk '/^Swap:/{print $2}')

log_info "Physical RAM: ${TOTAL_RAM_MB}MB | Existing Swap: ${TOTAL_SWAP_MB}MB"

if [ "$TOTAL_RAM_MB" -lt 2000 ] && [ "$TOTAL_SWAP_MB" -lt 1000 ]; then
    log_warn "Detected low physical RAM (${TOTAL_RAM_MB}MB) without adequate swap."
    log_info "Configuring 2GB emergency swapfile to prevent OOM killer during setup..."
    
    SWAPFILE="/swapfile"
    if [ ! -f "$SWAPFILE" ]; then
        if command -v fallocate >/dev/null 2>&1; then
            fallocate -l 2G "$SWAPFILE" || dd if=/dev/zero of="$SWAPFILE" bs=1M count=2048 status=progress
        else
            dd if=/dev/zero of="$SWAPFILE" bs=1M count=2048 status=progress
        fi
        chmod 600 "$SWAPFILE"
        mkswap "$SWAPFILE"
        swapon "$SWAPFILE"
        if ! grep -q "$SWAPFILE" /etc/fstab; then
            echo "$SWAPFILE none swap sw 0 0" >> /etc/fstab
        fi
        log_info "Swap file of 2GB created and activated."
    fi
    # Set low swappiness for predictable performance
    sysctl vm.swappiness=10 >/dev/null 2>&1 || true
fi

# ------------------------------------------------------------------------------
# 3. Install Essential System Packages
# ------------------------------------------------------------------------------
log_step "3/8 Installing system dependencies..."

if [[ "$OS_NAME" =~ ^(ubuntu|debian)$ ]]; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y
    apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        git \
        build-essential \
        rsync \
        procps
elif [[ "$OS_NAME" =~ ^(ol|rhel|centos|rocky|almalinux|fedora|amzn)$ ]]; then
    if command -v dnf >/dev/null 2>&1; then
        dnf install -y curl ca-certificates git gcc gcc-c++ make rsync procps-ng
    else
        yum install -y curl ca-certificates git gcc gcc-c++ make rsync procps-ng
    fi
else
    log_warn "Unsupported package manager. Please ensure curl, git, build-essential/gcc, and rsync are installed."
fi

# ------------------------------------------------------------------------------
# 4. Install Astral 'uv' (Lightweight Standalone Package Manager)
# ------------------------------------------------------------------------------
log_step "4/8 Installing/updating standalone 'uv'..."

UV_BIN="/usr/local/bin/uv"
if ! command -v uv >/dev/null 2>&1; then
    log_info "Downloading standalone uv installer from https://astral.sh/uv/install.sh..."
    export CARGO_DIST_FORCE_INSTALLERS=1
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="/usr/local/bin" sh
    chmod +x /usr/local/bin/uv /usr/local/bin/uvx || true
fi

if command -v uv >/dev/null 2>&1; then
    UV_VERSION=$(uv --version)
    log_info "uv installed successfully: $UV_VERSION"
else
    log_error "Failed to locate uv in PATH."
    exit 1
fi

# ------------------------------------------------------------------------------
# 5. Provision System User & Target Directories
# ------------------------------------------------------------------------------
log_step "5/8 Setting up system user and deployment directories..."

if ! id "$APP_USER" >/dev/null 2>&1; then
    log_info "Creating service user: $APP_USER"
    useradd -r -s /usr/sbin/nologin -d "$APP_DIR" -M "$APP_USER"
fi

mkdir -p "$APP_DIR"
mkdir -p "$ETC_DIR"
mkdir -p "$VAR_LOG_DIR"
mkdir -p "$VAR_LIB_DIR"

# Synchronize project code to /opt/polymarketbot
if [ "$SCRIPT_SOURCE_DIR" != "$APP_DIR" ]; then
    log_info "Syncing repository files from $SCRIPT_SOURCE_DIR to $APP_DIR..."
    rsync -av --delete \
        --exclude='.venv' \
        --exclude='.git' \
        --exclude='.pytest_cache' \
        --exclude='.ruff_cache' \
        --exclude='.tmp' \
        --exclude='data/logs' \
        --exclude='*.pyc' \
        --exclude='__pycache__' \
        "$SCRIPT_SOURCE_DIR/" "$APP_DIR/"
fi

mkdir -p "$APP_DIR/data/logs"
mkdir -p "$APP_DIR/data/tmp"
mkdir -p "$APP_DIR/data/processed"

# ------------------------------------------------------------------------------
# 6. Build Virtual Environment using 'uv sync'
# ------------------------------------------------------------------------------
log_step "6/8 Synchronizing Python virtualenv (uv sync --frozen --no-dev)..."

cd "$APP_DIR"
# Run uv sync as polymarketbot user or root
uv sync --frozen --no-dev

log_info "Virtual environment successfully prepared at $APP_DIR/.venv"

# ------------------------------------------------------------------------------
# 7. Environment File Setup & Permissions Hardening
# ------------------------------------------------------------------------------
log_step "7/8 Configuring environment variables and security permissions..."

ENV_TARGET="$ETC_DIR/polymarketbot.env"
if [ ! -f "$ENV_TARGET" ]; then
    if [ -f "$APP_DIR/deploy/env.template" ]; then
        cp "$APP_DIR/deploy/env.template" "$ENV_TARGET"
        log_info "Created configuration template at $ENV_TARGET"
    else
        touch "$ENV_TARGET"
    fi
    chmod 600 "$ENV_TARGET"
    chown "$APP_USER:$APP_GROUP" "$ENV_TARGET"
    log_warn "Please edit $ENV_TARGET with your Polymarket credentials before starting the service!"
else
    log_info "Existing environment file found at $ENV_TARGET (preserving existing config)."
fi

# Ensure correct permissions
chown -R "$APP_USER:$APP_GROUP" "$APP_DIR"
chown -R "$APP_USER:$APP_GROUP" "$VAR_LOG_DIR"
chown -R "$APP_USER:$APP_GROUP" "$VAR_LIB_DIR"
chown -R "$APP_USER:$APP_GROUP" "$ETC_DIR"
chmod 750 "$APP_DIR"
chmod 750 "$VAR_LOG_DIR"
chmod 750 "$VAR_LIB_DIR"

# ------------------------------------------------------------------------------
# 8. Install & Register Systemd Service
# ------------------------------------------------------------------------------
log_step "8/8 Registering systemd service..."

SERVICE_SRC="$APP_DIR/deploy/polymarketbot.service"
SERVICE_DEST="/etc/systemd/system/polymarketbot.service"

if [ -f "$SERVICE_SRC" ]; then
    cp "$SERVICE_SRC" "$SERVICE_DEST"
    chmod 644 "$SERVICE_DEST"
    systemctl daemon-reload
    systemctl enable polymarketbot.service
    log_info "polymarketbot.service registered and enabled at boot."
else
    log_error "Could not find $SERVICE_SRC"
    exit 1
fi

echo -e "\n${GREEN}==============================================================================${NC}"
echo -e "${GREEN} PolymarketBot Deployment Completed Successfully!${NC}"
echo -e "${GREEN}==============================================================================${NC}"
echo -e "Next Steps:"
echo -e "  1. Edit your production credentials:"
echo -e "     ${YELLOW}sudo nano /etc/polymarketbot/polymarketbot.env${NC}"
echo -e "  2. Start the service:"
echo -e "     ${GREEN}sudo systemctl start polymarketbot${NC}"
echo -e "  3. Check live status and telemetry:"
echo -e "     ${BLUE}sudo systemctl status polymarketbot${NC}"
echo -e "     ${BLUE}sudo journalctl -u polymarketbot -f -o cat${NC}"
echo -e "==============================================================================\n"
