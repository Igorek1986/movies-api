#!/bin/bash
# =============================================================================
# Movies API — unified install / manage script
# Usage:
#   bash <(curl -fsSL https://raw.githubusercontent.com/Igorek1986/movies-api/main/scripts/install.sh)
#   ./scripts/install.sh [install|update|switch|uninstall|status]
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
REPO="Igorek1986/movies-api"
BRANCH="main"
SERVICE_NAME="movies-api"
DEFAULT_PORT=8888
PYTHON_MIN_VERSION="3.10"
PYTHON_INSTALL_VERSION="3.13.5"

# Paths — resolved after root/user detection below
USER_HOME=""
USER_NAME=""
IS_ROOT=false
PROJECT_DIR=""   # set after USER_HOME is known

USE_PYENV=false
NEED_RELOAD=false

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# ---------------------------------------------------------------------------
# Root / user detection  (must happen before any path is used)
# ---------------------------------------------------------------------------
if [ "$(id -u)" -eq 0 ]; then
    USER_HOME="/root"
    USER_NAME="root"
    IS_ROOT=true
else
    USER_HOME="$HOME"
    USER_NAME="$(id -un)"
    IS_ROOT=false
fi

PROJECT_DIR="$USER_HOME/movies-api"

# ---------------------------------------------------------------------------
# Helper: run sudo only when not already root
# ---------------------------------------------------------------------------
function _sudo {
    if $IS_ROOT; then
        "$@"
    else
        sudo "$@"
    fi
}

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
function info    { echo -e "${GREEN}$*${NC}"; }
function warn    { echo -e "${YELLOW}$*${NC}"; }
function error_exit { echo -e "${RED}Error: $*${NC}" >&2; exit 1; }

function header {
    echo -e "\n${BLUE}=== $* ===${NC}"
}

# Prompt y/N — default N unless second arg is "y"
function confirm {
    local prompt="$1"
    local default="${2:-n}"
    read -rp "$prompt" response
    case "${response:-$default}" in
        [Yy]*) return 0 ;;
        *) return 1 ;;
    esac
}

# ---------------------------------------------------------------------------
# Shell config file detection
# ---------------------------------------------------------------------------
function get_shell_config {
    if [ -n "${ZSH_VERSION:-}" ]; then
        [ -f "$USER_HOME/.zprofile" ] && echo "$USER_HOME/.zprofile" || echo "$USER_HOME/.zshrc"
    else
        [ -f "$USER_HOME/.profile" ] && echo "$USER_HOME/.profile" || echo "$USER_HOME/.bashrc"
    fi
}

# ---------------------------------------------------------------------------
# Detect current install type: "service", "docker", or "none"
# ---------------------------------------------------------------------------
function detect_install_type {
    # Check systemd service — check the unit file directly (no sudo needed)
    if [ -f "/etc/systemd/system/${SERVICE_NAME}.service" ] || \
       systemctl list-unit-files 2>/dev/null | grep -q "${SERVICE_NAME}.service"; then
        echo "service"
        return
    fi
    # Check docker compose stack
    if [ -f "$PROJECT_DIR/docker-compose.prod.yml" ]; then
        if docker compose -f "$PROJECT_DIR/docker-compose.prod.yml" ps 2>/dev/null | grep -q "movies-api-app"; then
            echo "docker"
            return
        fi
    fi
    echo "none"
}

# ---------------------------------------------------------------------------
# GitHub version check
# ---------------------------------------------------------------------------
function check_updates {
    if [ ! -d "$PROJECT_DIR/.git" ]; then
        warn "Project directory not found — cannot check for updates."
        return 1
    fi

    cd "$PROJECT_DIR"
    # Always track the main branch for production updates
    git fetch origin "$BRANCH" --quiet 2>/dev/null || { warn "Could not reach GitHub."; return 1; }

    local local_hash remote_hash current_branch
    current_branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
    local_hash=$(git rev-parse HEAD 2>/dev/null)
    remote_hash=$(git rev-parse "origin/$BRANCH" 2>/dev/null)

    if [ "$current_branch" != "$BRANCH" ]; then
        warn "  Current branch: ${current_branch} (production branch: ${BRANCH})"
    fi

    if [ "$local_hash" != "$remote_hash" ]; then
        info "Update available!"
        warn "  Local : $local_hash"
        info "  Remote: $remote_hash"
        return 0   # update available
    else
        info "Already up to date (${local_hash:0:8})."
        return 1   # no update
    fi
}

# ---------------------------------------------------------------------------
# ENV management
# ---------------------------------------------------------------------------

# Sync keys from .env.template into .env — prompts for any missing key.
# Prints count of added vars.
function sync_env_vars {
    local template="$PROJECT_DIR/.env.template"
    local envfile="$PROJECT_DIR/.env"
    local added=0

    if [ ! -f "$template" ]; then
        warn ".env.template not found — skipping sync."
        return 0
    fi

    header "Checking for new environment variables"

    while IFS= read -r line; do
        # Skip comments and blank lines
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        [[ -z "${line// }" ]] && continue
        # Only process KEY=value lines
        [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)= ]] || continue

        local key="${BASH_REMATCH[1]}"
        local template_val="${line#*=}"
        # Strip surrounding quotes from template value for display
        local display_val="${template_val//\'/}"
        display_val="${display_val//\"/}"

        # If key already exists in .env, skip
        if grep -q "^${key}=" "$envfile" 2>/dev/null; then
            continue
        fi

        warn "Missing variable: ${key}"
        warn "  Template default: ${display_val}"
        read -rp "  Enter value for ${key} [${display_val}]: " user_val
        local final_val="${user_val:-$display_val}"

        echo "${key}=${final_val}" >> "$envfile"
        info "  Added: ${key}"
        (( added++ )) || true
    done < "$template"

    if [ "$added" -eq 0 ]; then
        info "All environment variables are up to date."
    else
        info "Added ${added} new variable(s) to .env."
    fi

    return "$added"
}

# Validate required env keys are not placeholders.
function validate_env {
    local envfile="$PROJECT_DIR/.env"
    [ -f "$envfile" ] || { warn ".env not found."; return 1; }

    local ok=true
    local required_keys=(TMDB_TOKEN DB_USER DB_PASSWORD DB_NAME ADMIN_PASSWORD)
    local bad_values=("" "Bearer TOKEN" "PASSWORD" "your_password")

    for key in "${required_keys[@]}"; do
        local val
        val=$(grep "^${key}=" "$envfile" 2>/dev/null | head -1 | cut -d= -f2- | tr -d "'\"")
        local is_bad=false
        for bad in "${bad_values[@]}"; do
            [ "$val" = "$bad" ] && is_bad=true && break
        done
        if $is_bad; then
            warn "  WARNING: ${key} appears to be unset or using a placeholder value."
            ok=false
        fi
    done

    $ok && return 0 || return 1
}

# ---------------------------------------------------------------------------
# System dependencies
# ---------------------------------------------------------------------------
function install_system_deps {
    header "Installing system dependencies"
    warn "Updating package lists (may take a moment)..."
    # Ignore errors from 3rd-party repos (e.g. syncthing, broken PPAs)
    _sudo apt-get update -o Acquire::ForceIPv4=true 2>&1 \
        | grep -v "^Hit\|^Get\|^Ign\|^Fetched\|^Reading" || true
    warn "Installing packages..."
    _sudo apt-get install -y --no-install-recommends \
        git curl make build-essential \
        libssl-dev zlib1g-dev libbz2-dev libreadline-dev \
        libsqlite3-dev wget llvm libncurses5-dev libncursesw5-dev \
        xz-utils tk-dev libffi-dev liblzma-dev python3-openssl \
        || error_exit "Failed to install system dependencies"
    info "System dependencies installed."
}

# ---------------------------------------------------------------------------
# Python / pyenv
# ---------------------------------------------------------------------------
function check_system_python {
    if command -v python3 &>/dev/null; then
        local version_ok
        version_ok=$(python3 -c "import sys; print(1 if sys.version_info >= (3,10) else 0)" 2>/dev/null || echo 0)
        local ver
        ver=$(python3 -c "import sys; print('{}.{}'.format(*sys.version_info[:2]))" 2>/dev/null || echo "?")
        if [ "$version_ok" -eq 1 ]; then
            info "Found Python ${ver} — meets requirement ${PYTHON_MIN_VERSION}+."
            return 0
        else
            warn "Found Python ${ver} — below ${PYTHON_MIN_VERSION}+."
            return 1
        fi
    else
        warn "Python 3 not found."
        return 1
    fi
}

function check_pyenv_installed {
    if command -v pyenv &>/dev/null; then
        info "pyenv is installed: $(pyenv --version 2>/dev/null || echo 'unknown')"
        return 0
    fi
    if [ -d "$USER_HOME/.pyenv" ]; then
        if [ -d "$USER_HOME/.pyenv/versions" ] && [ -n "$(ls -A "$USER_HOME/.pyenv/versions" 2>/dev/null)" ]; then
            warn "Activating existing pyenv installation..."
            export PYENV_ROOT="$USER_HOME/.pyenv"
            export PATH="$PYENV_ROOT/bin:$PATH"
            eval "$(pyenv init --path)"
            eval "$(pyenv init -)"
            return 0
        else
            warn "Removing broken pyenv directory..."
            rm -rf "$USER_HOME/.pyenv"
        fi
    fi
    return 1
}

function install_pyenv {
    check_pyenv_installed && return

    header "Installing pyenv"
    curl -sSL https://pyenv.run | bash || error_exit "Failed to install pyenv"

    local shell_cfg
    shell_cfg=$(get_shell_config)
    if ! grep -q "pyenv init" "$shell_cfg" 2>/dev/null; then
        cat >> "$shell_cfg" <<EOF

# Pyenv configuration
export PYENV_ROOT="$USER_HOME/.pyenv"
export PATH="\$PYENV_ROOT/bin:\$PATH"
eval "\$(pyenv init --path)"
eval "\$(pyenv init -)"
eval "\$(pyenv virtualenv-init -)"
EOF
    fi

    export PYENV_ROOT="$USER_HOME/.pyenv"
    export PATH="$PYENV_ROOT/bin:$PATH"
    eval "$(pyenv init --path)"
    eval "$(pyenv init -)"
    NEED_RELOAD=true
}

function install_python_with_pyenv {
    header "Installing Python ${PYTHON_INSTALL_VERSION} via pyenv"
    warn "Downloading and compiling Python — this takes 5-15 minutes, please wait..."
    warn "You can watch progress in another terminal: tail -f /tmp/pyenv-install.log"
    pyenv install "$PYTHON_INSTALL_VERSION" --skip-existing -v \
        > /tmp/pyenv-install.log 2>&1 &
    local pid=$!
    # Show a progress indicator while compiling
    local spin=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏')
    local i=0
    while kill -0 "$pid" 2>/dev/null; do
        printf "\r  ${spin[$i]} Building Python..."
        i=$(( (i+1) % 10 ))
        sleep 0.5
    done
    printf "\r"
    wait "$pid" || error_exit "Failed to install Python ${PYTHON_INSTALL_VERSION}. Check /tmp/pyenv-install.log"
    pyenv global "$PYTHON_INSTALL_VERSION"
    info "Python ${PYTHON_INSTALL_VERSION} installed."
}

function check_or_install_python {
    if check_system_python; then
        if confirm "System Python meets requirements. Use pyenv anyway? (y/N) " "n"; then
            USE_PYENV=true
            install_pyenv
            install_python_with_pyenv
        fi
    else
        warn "System Python insufficient — installing via pyenv."
        USE_PYENV=true
        install_pyenv
        install_python_with_pyenv
    fi
}

# ---------------------------------------------------------------------------
# Poetry
# ---------------------------------------------------------------------------
function check_poetry_installed {
    if command -v poetry &>/dev/null; then
        info "Poetry is already installed."
        return 0
    elif [ -f "$USER_HOME/.local/bin/poetry" ]; then
        info "Poetry found at ~/.local/bin."
        export PATH="$USER_HOME/.local/bin:$PATH"
        return 0
    fi
    return 1
}

function install_poetry {
    check_poetry_installed && return

    header "Installing Poetry"
    if $USE_PYENV; then
        export PATH="$USER_HOME/.pyenv/shims:$USER_HOME/.pyenv/bin:$PATH"
    fi
    curl -sSL https://install.python-poetry.org | python3 - \
        || error_exit "Failed to install Poetry"

    export PATH="$USER_HOME/.local/bin:$PATH"
    local shell_cfg
    shell_cfg=$(get_shell_config)
    if ! grep -q ".local/bin" "$shell_cfg" 2>/dev/null; then
        echo "export PATH=\"$USER_HOME/.local/bin:\$PATH\"" >> "$shell_cfg"
        NEED_RELOAD=true
    fi
}

# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------
function clone_or_update_repo {
    if [ ! -d "$PROJECT_DIR" ]; then
        info "Cloning repository..."
        mkdir -p "$PROJECT_DIR"
        git clone "https://github.com/${REPO}.git" "$PROJECT_DIR" \
            || error_exit "Failed to clone repository"
    else
        header "Pulling latest changes"
        cd "$PROJECT_DIR"
        local current_branch
        current_branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
        if [ "$current_branch" != "$BRANCH" ]; then
            warn "Switching branch: ${current_branch} → ${BRANCH}"
            git checkout "$BRANCH" || error_exit "Failed to checkout branch ${BRANCH}"
        fi
        git pull origin "$BRANCH" || error_exit "git pull failed"
    fi
}

# ---------------------------------------------------------------------------
# Python deps
# ---------------------------------------------------------------------------
function install_python_deps {
    header "Installing Python dependencies"
    export PATH="$USER_HOME/.local/bin:$PATH"
    cd "$PROJECT_DIR"
    poetry install --no-root --no-interaction \
        || error_exit "Failed to install Python dependencies"
    info "Dependencies installed."
}

# ---------------------------------------------------------------------------
# .env setup
# ---------------------------------------------------------------------------
function setup_env_file {
    local envfile="$PROJECT_DIR/.env"
    local template="$PROJECT_DIR/.env.template"

    if [ ! -f "$envfile" ]; then
        header "Creating .env file"
        if [ -f "$template" ]; then
            cp "$template" "$envfile"
            info "Copied .env.template → .env"
        else
            warn ".env.template not found — creating empty .env"
            touch "$envfile"
        fi
    else
        info "Using existing .env file."
    fi

    # Auto-generate CACHE_CLEAR_PASSWORD if it's missing/placeholder
    local cache_pass
    cache_pass=$(grep "^CACHE_CLEAR_PASSWORD=" "$envfile" 2>/dev/null | head -1 | cut -d= -f2- | tr -d "'\"")
    if [ -z "$cache_pass" ] || [ "$cache_pass" = "PASSWORD" ]; then
        local new_pass
        new_pass=$(openssl rand -base64 16 | tr -dc 'a-zA-Z0-9' | head -c 12)
        if grep -q "^CACHE_CLEAR_PASSWORD=" "$envfile"; then
            sed -i "s|^CACHE_CLEAR_PASSWORD=.*|CACHE_CLEAR_PASSWORD=${new_pass}|" "$envfile"
        else
            echo "CACHE_CLEAR_PASSWORD=${new_pass}" >> "$envfile"
        fi
        info "Auto-generated CACHE_CLEAR_PASSWORD: ${new_pass}"
    fi

    # Sync any new keys from template
    sync_env_vars

    warn "Review your configuration:"
    warn "  ${envfile}"
}

# ---------------------------------------------------------------------------
# Systemd service
# ---------------------------------------------------------------------------
function setup_systemd_service {
    local port="${1:-$DEFAULT_PORT}"

    header "Configuring systemd service (port ${port})"

    local service_file="/etc/systemd/system/${SERVICE_NAME}.service"
    local pyenv_path="$USER_HOME/.pyenv/shims:$USER_HOME/.pyenv/bin:"

    _sudo tee "$service_file" > /dev/null <<EOF
[Unit]
Description=Movies API Service
After=network.target

[Service]
User=$USER_NAME
Group=$USER_NAME
WorkingDirectory=$PROJECT_DIR
Environment="PATH=${pyenv_path}$USER_HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
EnvironmentFile=$PROJECT_DIR/.env
ExecStart=$USER_HOME/.local/bin/poetry run uvicorn app.main:app --host 0.0.0.0 --port $port
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    _sudo systemctl daemon-reload
    _sudo systemctl enable "$SERVICE_NAME"
    _sudo systemctl restart "$SERVICE_NAME"
    info "Service ${SERVICE_NAME} enabled and started."
}

# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------
function check_docker {
    command -v docker &>/dev/null \
        || error_exit "Docker is not installed. Install Docker first: https://docs.docker.com/engine/install/"
    docker compose version &>/dev/null \
        || error_exit "Docker Compose plugin not found. Install it: https://docs.docker.com/compose/install/"
    info "Docker and Docker Compose are available."
}

function ensure_releases_dir_in_env {
    local envfile="$PROJECT_DIR/.env"
    local val
    val=$(grep "^RELEASES_HOST_DIR=" "$envfile" 2>/dev/null | head -1 | cut -d= -f2- | tr -d "'\"")
    if [ -z "$val" ]; then
        local default_dir="$USER_HOME/releases"
        read -rp "  Host path to releases directory [${default_dir}]: " user_dir
        local final_dir="${user_dir:-$default_dir}"
        echo "RELEASES_HOST_DIR=${final_dir}" >> "$envfile"
        info "Set RELEASES_HOST_DIR=${final_dir}"
        mkdir -p "$final_dir" || true
    fi
}

function install_docker_mode {
    check_docker
    clone_or_update_repo
    cd "$PROJECT_DIR"
    setup_env_file
    ensure_releases_dir_in_env

    header "Building and starting Docker containers"
    docker compose -f docker-compose.prod.yml up -d --build \
        || error_exit "docker compose up failed"
    info "Docker stack started."
}

# ---------------------------------------------------------------------------
# Main install flow
# ---------------------------------------------------------------------------
function do_install {
    header "Movies API Installation"

    local existing
    existing=$(detect_install_type)
    if [ "$existing" != "none" ]; then
        warn "Movies API is already installed (${existing} mode)."
        echo ""
        echo "  1) Update existing installation"
        echo "  2) Switch install mode"
        echo "  3) Uninstall first, then reinstall"
        echo "  0) Cancel"
        echo ""
        read -rp "Choice: " already_choice
        case "$already_choice" in
            1) do_update;   return ;;
            2) do_switch;   return ;;
            3) do_uninstall
               echo ""
               warn "Re-running installation..."
               ;;
            *) info "Cancelled."; return ;;
        esac
    fi

    echo "Select install mode:"
    echo "  1) Systemd service  (Python + Poetry on the host)"
    echo "  2) Docker           (requires Docker + Docker Compose)"
    read -rp "Choice [1]: " mode_choice
    mode_choice="${mode_choice:-1}"

    case "$mode_choice" in
        1)
            install_system_deps
            check_or_install_python
            install_poetry
            clone_or_update_repo
            cd "$PROJECT_DIR"
            setup_env_file

            read -rp "Port to listen on [${DEFAULT_PORT}]: " svc_port
            svc_port="${svc_port:-$DEFAULT_PORT}"

            install_python_deps
            setup_systemd_service "$svc_port"

            header "Installation complete"
            info "Access URL: http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo localhost):${svc_port}"
            info "Manage:     sudo systemctl {status|restart|stop} ${SERVICE_NAME}"
            info "Logs:       sudo journalctl -u ${SERVICE_NAME} -f"
            ;;
        2)
            install_docker_mode

            local host_ip
            host_ip=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
            local port
            port=$(grep "^PORT=" "$PROJECT_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2 | tr -d "'\"")
            port="${port:-$DEFAULT_PORT}"

            header "Installation complete"
            info "Access URL: http://${host_ip}:${port}"
            info "Manage:     docker compose -f ${PROJECT_DIR}/docker-compose.prod.yml {ps|logs|down}"
            ;;
        *)
            error_exit "Invalid choice."
            ;;
    esac

    if $NEED_RELOAD; then
        warn "Shell reload required — run: source $(get_shell_config)"
    fi
}

# ---------------------------------------------------------------------------
# Update flow
# ---------------------------------------------------------------------------
function do_update {
    header "Updating Movies API"

    local install_type
    install_type=$(detect_install_type)

    if ! check_updates; then
        if ! confirm "Already up to date. Force update anyway? (y/N) " "n"; then
            info "Nothing to do."
            return
        fi
    fi

    clone_or_update_repo
    sync_env_vars

    case "$install_type" in
        service)
            install_python_deps
            _sudo systemctl restart "$SERVICE_NAME"
            info "Service restarted."
            ;;
        docker)
            cd "$PROJECT_DIR"
            docker compose -f docker-compose.prod.yml up -d --build \
                || error_exit "docker compose up failed"
            info "Docker stack rebuilt."
            ;;
        none)
            warn "No running installation detected — run install first."
            ;;
    esac

    do_status
}

# ---------------------------------------------------------------------------
# Switch mode
# ---------------------------------------------------------------------------
function do_switch {
    local install_type
    install_type=$(detect_install_type)

    case "$install_type" in
        service)
            header "Switching service → Docker"
            confirm "Stop and disable the systemd service and start Docker stack? (y/N) " "n" \
                || { info "Cancelled."; return; }

            _sudo systemctl stop "$SERVICE_NAME" 2>/dev/null || true
            _sudo systemctl disable "$SERVICE_NAME" 2>/dev/null || true

            check_docker
            cd "$PROJECT_DIR"
            # .env already exists, just make sure RELEASES_HOST_DIR is set
            ensure_releases_dir_in_env
            docker compose -f docker-compose.prod.yml up -d --build \
                || error_exit "docker compose up failed"
            info "Switched to Docker mode."
            ;;
        docker)
            header "Switching Docker → systemd service"
            confirm "Stop Docker stack and install as systemd service? (y/N) " "n" \
                || { info "Cancelled."; return; }

            cd "$PROJECT_DIR"
            docker compose -f docker-compose.prod.yml down || true

            check_or_install_python
            install_poetry
            install_python_deps

            read -rp "Port to listen on [${DEFAULT_PORT}]: " svc_port
            svc_port="${svc_port:-$DEFAULT_PORT}"
            setup_systemd_service "$svc_port"
            info "Switched to systemd service mode."
            ;;
        none)
            warn "No installation detected. Run install first."
            ;;
    esac
}

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------
function do_uninstall {
    header "Uninstalling Movies API"

    confirm "This will remove the service/containers and optionally project files. Continue? (y/N) " "n" \
        || { info "Cancelled."; return; }

    local install_type
    install_type=$(detect_install_type)

    # Stop / remove service or Docker stack
    case "$install_type" in
        service)
            _sudo systemctl stop "$SERVICE_NAME" 2>/dev/null || true
            _sudo systemctl disable "$SERVICE_NAME" 2>/dev/null || true
            _sudo rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
            _sudo systemctl daemon-reload
            info "Systemd service removed."
            ;;
        docker)
            cd "$PROJECT_DIR" 2>/dev/null || true
            if confirm "Remove Docker volumes (database data)? (y/N) " "n"; then
                docker compose -f docker-compose.prod.yml down -v 2>/dev/null || true
                info "Docker stack and volumes removed."
            else
                docker compose -f docker-compose.prod.yml down 2>/dev/null || true
                info "Docker stack removed (volumes kept)."
            fi
            ;;
        none)
            warn "No active installation found."
            ;;
    esac

    # Project directory
    if [ -d "$PROJECT_DIR" ]; then
        if confirm "Remove project directory ${PROJECT_DIR}? (y/N) " "n"; then
            rm -rf "$PROJECT_DIR"
            info "Project directory removed."
        fi
    fi

    # Optional: remove Poetry
    if command -v poetry &>/dev/null || [ -f "$USER_HOME/.local/bin/poetry" ]; then
        if confirm "Remove Poetry? (y/N) " "n"; then
            rm -f "$USER_HOME/.local/bin/poetry"
            rm -rf "$USER_HOME/.local/share/pypoetry"
            info "Poetry removed."

            # Clean shell configs
            local shell_files=("$USER_HOME/.bashrc" "$USER_HOME/.bash_profile"
                               "$USER_HOME/.profile" "$USER_HOME/.zshrc" "$USER_HOME/.zprofile")
            for f in "${shell_files[@]}"; do
                [ -f "$f" ] && sed -i '/poetry/d' "$f" 2>/dev/null || true
            done
            NEED_RELOAD=true
        fi
    fi

    # Optional: remove pyenv
    if [ -d "$USER_HOME/.pyenv" ]; then
        if confirm "Remove pyenv (and all Python versions installed via it)? (y/N) " "n"; then
            rm -rf "$USER_HOME/.pyenv"
            info "pyenv removed."

            local shell_files=("$USER_HOME/.bashrc" "$USER_HOME/.bash_profile"
                               "$USER_HOME/.profile" "$USER_HOME/.zshrc" "$USER_HOME/.zprofile")
            for f in "${shell_files[@]}"; do
                [ -f "$f" ] && sed -i '/# Pyenv configuration/d;/PYENV_ROOT/d;/pyenv init/d;/pyenv virtualenv-init/d;/\/\.pyenv/d' "$f" 2>/dev/null || true
            done
            NEED_RELOAD=true
        fi
    fi

    if $NEED_RELOAD; then
        warn "Shell reload required — open a new terminal session."
    fi
    info "Uninstall complete."
}

# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
function do_status {
    header "Movies API Status"

    local install_type
    install_type=$(detect_install_type)
    info "Install type: ${install_type}"

    case "$install_type" in
        service)
            echo ""
            _sudo systemctl status "$SERVICE_NAME" --no-pager -l 2>/dev/null || true
            ;;
        docker)
            echo ""
            docker compose -f "$PROJECT_DIR/docker-compose.prod.yml" ps 2>/dev/null || true
            ;;
        none)
            warn "Movies API is not installed."
            ;;
    esac

    echo ""
    check_updates || true

    echo ""
    if [ -f "$PROJECT_DIR/.env" ]; then
        validate_env && info "ENV validation: OK" || warn "ENV validation: some variables need attention."
    else
        warn "No .env file found."
    fi
}

# ---------------------------------------------------------------------------
# Interactive menu
# ---------------------------------------------------------------------------
function show_menu {
    local install_type
    install_type=$(detect_install_type)

    echo ""
    echo -e "${BLUE}================================================${NC}"
    echo -e "${BLUE}         Movies API — Management Menu           ${NC}"
    echo -e "${BLUE}================================================${NC}"

    if [ "$install_type" = "none" ]; then
        warn "Movies API is not installed."
        echo ""
        echo "  1) Install"
        echo "  0) Exit"
        echo ""
        read -rp "Choice: " choice
        case "$choice" in
            1) do_install ;;
            0) exit 0 ;;
            *) warn "Invalid choice." ;;
        esac
    else
        local status_line
        case "$install_type" in
            service) status_line="systemd service" ;;
            docker)  status_line="Docker (docker-compose.prod.yml)" ;;
        esac
        info "Installed as: ${status_line}"
        echo ""

        local switch_label
        [ "$install_type" = "service" ] && switch_label="Switch to Docker" || switch_label="Switch to systemd service"

        echo "  1) Update"
        echo "  2) ${switch_label}"
        echo "  3) Status"
        echo "  4) Uninstall"
        echo "  0) Exit"
        echo ""
        read -rp "Choice: " choice
        case "$choice" in
            1) do_update ;;
            2) do_switch ;;
            3) do_status ;;
            4) do_uninstall ;;
            0) exit 0 ;;
            *) warn "Invalid choice." ;;
        esac
    fi
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
CMD="${1:-}"

case "$CMD" in
    install)   do_install   ;;
    update)    do_update    ;;
    switch)    do_switch    ;;
    uninstall) do_uninstall ;;
    status)    do_status    ;;
    "")        show_menu    ;;
    *)
        echo "Usage: $0 [install|update|switch|uninstall|status]"
        exit 1
        ;;
esac
