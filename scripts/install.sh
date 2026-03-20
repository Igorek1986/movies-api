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
# Set VERBOSE=1 to see full output of long-running commands
VERBOSE="${VERBOSE:-0}"
# Set DEBUG_INSTALL=1 to skip git branch/update checks
DEBUG_INSTALL="${DEBUG_INSTALL:-0}"
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

PROJECT_DIR="${PROJECT_DIR:-$USER_HOME/movies-api}"

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

# Run a command with spinner (quiet mode) or full output (verbose mode).
# Usage: run_cmd "Description" cmd arg1 arg2 ...
function run_cmd {
    local desc="$1"; shift
    if [ "$VERBOSE" = "1" ]; then
        echo -e "${YELLOW}▶ ${desc}${NC}"
        "$@" || return $?
    else
        local spin=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏')
        local i=0
        local logfile
        logfile=$(mktemp /tmp/movies-api-install.XXXXXX)
        "$@" >"$logfile" 2>&1 &
        local pid=$!
        while kill -0 "$pid" 2>/dev/null; do
            printf "\r  ${spin[$i]} ${desc}..."
            i=$(( (i+1) % 10 ))
            sleep 0.3
        done
        wait "$pid"
        local rc=$?
        printf "\r"
        if [ $rc -ne 0 ]; then
            echo -e "  ${RED}✗ ${desc} — FAILED${NC}"
            echo -e "${RED}--- Output ---${NC}"
            cat "$logfile"
            rm -f "$logfile"
            return $rc
        else
            echo -e "  ${GREEN}✓ ${desc}${NC}"
        fi
        rm -f "$logfile"
    fi
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
    # Check systemd service
    if [ -f "/etc/systemd/system/${SERVICE_NAME}.service" ] || \
       systemctl list-unit-files 2>/dev/null | grep -q "${SERVICE_NAME}.service"; then
        echo "service"
        return
    fi
    # Check Docker — any containers (running or stopped) named movies-api
    if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q "movies-api"; then
        echo "docker"
        return
    fi
    echo "none"
}

# ---------------------------------------------------------------------------
# GitHub version check
# ---------------------------------------------------------------------------
function check_updates {
    if [ "$DEBUG_INSTALL" = "1" ]; then
        warn "[DEBUG] Skipping git update check."
        return 1
    fi
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

function _is_placeholder {
    local val="$1"
    case "$val" in
        ""|"TOKEN"|"Bearer TOKEN"|"PASSWORD"|"password"|"your_password"|"CHANGE_ME") return 0 ;;
        "The path to the directory relative to the home directory") return 0 ;;
        *"example"*) return 0 ;;
    esac
    return 1
}

function _get_env_val {
    local key="$1"
    local envfile="${2:-$PROJECT_DIR/.env}"
    grep "^${key}=" "$envfile" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '\r' | sed "s/^['\"]//;s/['\"]$//" || true
}

function setup_env_file {
    local envfile="$PROJECT_DIR/.env"
    local template="$PROJECT_DIR/.env.template"

    if [ ! -f "$template" ]; then
        warn ".env.template not found — skipping env setup."
        return
    fi

    local is_fresh=false
    if [ ! -f "$envfile" ]; then
        is_fresh=true
        info "Creating new .env file..."
    else
        info "Updating existing .env file..."
    fi

    # Load ALL existing values from .env into associative array
    declare -A existing_vals
    if [ -f "$envfile" ]; then
        while IFS= read -r eline; do
            eline="${eline%$'\r'}"
            [[ "$eline" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
            local ekey="${BASH_REMATCH[1]}"
            local evalue="${BASH_REMATCH[2]}"
            # Strip surrounding quotes
            evalue="${evalue#\'}" ; evalue="${evalue%\'}"
            evalue="${evalue#\"}" ; evalue="${evalue%\"}"
            existing_vals["$ekey"]="$evalue"
        done < "$envfile"
    fi

    local tmpfile="${envfile}.tmp"
    declare -A written_keys
    > "$tmpfile"

    header "Configuring environment variables"
    if $is_fresh; then
        warn "Fresh install — заполните все переменные."
    else
        warn "Проверка пропущенных и незаполненных переменных..."
    fi
    echo -e "  \033[2mПустой ввод (Enter) = используется значение по умолчанию\033[0m"
    echo ""

    local last_comment=""
    local prompted=0

    # Open template on fd 3 so that interactive `read -rp` inside the loop
    # still reads from the terminal (fd 0), not from the template file.
    while IFS= read -r line <&3; do
        # Strip carriage return (CRLF support)
        line="${line%$'\r'}"

        # Pass comments and blank lines through as-is
        if [[ "$line" =~ ^[[:space:]]*#(.*)$ ]]; then
            echo "$line" >> "$tmpfile"
            last_comment="${BASH_REMATCH[1]# }"
            continue
        fi
        if [[ -z "${line// }" ]]; then
            echo "$line" >> "$tmpfile"
            last_comment=""
            continue
        fi
        # Only handle KEY=value lines
        if ! [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)= ]]; then
            echo "$line" >> "$tmpfile"
            continue
        fi

        local key="${BASH_REMATCH[1]}"
        local template_raw="${line#*=}"
        # Strip quotes from template default for display
        local template_val="${template_raw#\'}" ; template_val="${template_val%\'}"
        template_val="${template_val#\"}" ; template_val="${template_val%\"}"

        local final_val=""
        local need_prompt=false

        if [ "${existing_vals[$key]+isset}" = "isset" ]; then
            local cur="${existing_vals[$key]}"
            if $is_fresh || _is_placeholder "$cur"; then
                need_prompt=true
                # Show current value as suggestion if not a placeholder
                _is_placeholder "$cur" && final_val="$template_val" || final_val="$cur"
            else
                # Existing valid value — keep it silently
                final_val="$cur"
            fi
        else
            # Missing key
            need_prompt=true
            final_val="$template_val"
        fi

        if $need_prompt; then
            [ -n "$last_comment" ] && echo -e "  ${BLUE}${last_comment}${NC}"
            local hint=""
            [ "$key" = "TMDB_TOKEN" ] && hint="  \033[2m(Bearer добавится автоматически)\033[0m"
            if [ -n "$final_val" ]; then
                printf "  \033[1;33m%s\033[0m (по умолчанию: \033[2m%s\033[0m)%b: " "$key" "$final_val" "$hint"
            else
                printf "  \033[1;33m%s\033[0m%b: " "$key" "$hint"
            fi
            read -r user_val </dev/tty
            [ -n "$user_val" ] && final_val="$user_val"
            # Auto-add Bearer prefix for TMDB_TOKEN
            if [ "$key" = "TMDB_TOKEN" ] && [ -n "$final_val" ]; then
                [[ "$final_val" != Bearer* ]] && final_val="Bearer ${final_val}"
            fi
            (( prompted++ )) || true
        fi

        echo "${key}=${final_val}" >> "$tmpfile"
        written_keys["$key"]=1
        last_comment=""
    done 3< "$template"

    # Дописываем ключи из существующего .env которых нет в шаблоне
    # (например, раскомментированные пользователем опциональные переменные)
    local extras=()
    for key in "${!existing_vals[@]}"; do
        [ "${written_keys[$key]+isset}" = "isset" ] && continue
        extras+=("$key")
    done
    if [ "${#extras[@]}" -gt 0 ]; then
        echo "" >> "$tmpfile"
        echo "# Extra variables (not in template)" >> "$tmpfile"
        for key in "${extras[@]}"; do
            echo "${key}=${existing_vals[$key]}" >> "$tmpfile"
        done
    fi

    mv "$tmpfile" "$envfile"

    if [ "$prompted" -gt 0 ]; then
        info "✓ .env saved ($prompted value(s) configured)."
    else
        info "✓ .env is up to date."
    fi
}

function validate_env {
    local envfile="$PROJECT_DIR/.env"
    [ -f "$envfile" ] || { warn ".env not found."; return 1; }

    local ok=true
    local required_keys=(TMDB_TOKEN DB_USER DB_PASSWORD DB_NAME ADMIN_PASSWORD)

    for key in "${required_keys[@]}"; do
        local val
        val=$(_get_env_val "$key" "$envfile")
        if _is_placeholder "$val"; then
            warn "  ⚠  ${key} still has placeholder/empty value"
            ok=false
        fi
    done
    $ok
}

# ---------------------------------------------------------------------------
# System dependencies
# ---------------------------------------------------------------------------
function install_system_deps {
    header "Installing system dependencies"
    # Ignore errors from 3rd-party repos (e.g. syncthing, broken PPAs)
    _sudo apt-get update -o Acquire::ForceIPv4=true 2>/dev/null || true
    run_cmd "Installing packages" \
        _sudo apt-get install -y --no-install-recommends \
            git curl make build-essential \
            libssl-dev zlib1g-dev libbz2-dev libreadline-dev \
            libsqlite3-dev wget llvm libncurses5-dev libncursesw5-dev \
            xz-utils tk-dev libffi-dev liblzma-dev python3-openssl \
        || error_exit "Failed to install system dependencies"
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
    # In debug mode, always use the existing directory as-is
    if [ "$DEBUG_INSTALL" = "1" ]; then
        if [ -d "$PROJECT_DIR" ] && [ -n "$(ls -A "$PROJECT_DIR" 2>/dev/null)" ]; then
            warn "[DEBUG] Skipping clone/pull — using existing ${PROJECT_DIR}"
            return
        fi
    fi

    # Clone only if directory doesn't exist or is empty
    if [ ! -d "$PROJECT_DIR" ] || [ -z "$(ls -A "$PROJECT_DIR" 2>/dev/null)" ]; then
        info "Cloning repository into ${PROJECT_DIR}..."
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
# PostgreSQL setup
# ---------------------------------------------------------------------------
function setup_postgres {
    header "PostgreSQL Setup"

    local db_user db_pass db_name db_host db_port
    db_user=$(_get_env_val "DB_USER")
    db_pass=$(_get_env_val "DB_PASSWORD")
    db_name=$(_get_env_val "DB_NAME")
    db_host=$(_get_env_val "DB_HOST")
    db_port=$(_get_env_val "DB_PORT")
    db_host="${db_host:-localhost}"
    db_port="${db_port:-5432}"

    if [ -z "$db_user" ] || [ -z "$db_name" ]; then
        warn "DB_USER or DB_NAME not set in .env — skipping PostgreSQL setup."
        return
    fi

    # Install PostgreSQL if missing
    if ! command -v psql &>/dev/null; then
        warn "PostgreSQL client not found."
        if confirm "Install PostgreSQL now? (Y/n) " "y"; then
            run_cmd "Installing PostgreSQL" \
                _sudo apt-get install -y postgresql postgresql-contrib \
                || { warn "Failed to install PostgreSQL — skipping DB setup."; return; }
        else
            warn "Skipping PostgreSQL setup — make sure it's running before starting the app."
            return
        fi
    fi

    # Start if not running
    if ! pg_isready -h "$db_host" -p "$db_port" -q 2>/dev/null; then
        warn "PostgreSQL is not running. Starting..."
        _sudo systemctl start postgresql 2>/dev/null \
            || { warn "Failed to start PostgreSQL — skipping DB setup."; return; }
        sleep 2
        if ! pg_isready -h "$db_host" -p "$db_port" -q 2>/dev/null; then
            warn "PostgreSQL still not ready — skipping DB setup."
            return
        fi
    fi

    # Enable on boot
    _sudo systemctl enable postgresql 2>/dev/null || true

    # Create role if not exists
    local role_exists
    role_exists=$(_sudo -u postgres psql -tAc \
        "SELECT 1 FROM pg_roles WHERE rolname='${db_user}'" 2>/dev/null || echo "")
    if [ "$role_exists" != "1" ]; then
        info "Creating PostgreSQL role: ${db_user}"
        _sudo -u postgres psql -c \
            "CREATE USER \"${db_user}\" WITH PASSWORD '${db_pass}';" 2>/dev/null \
            || warn "Could not create role ${db_user} — it may already exist."
    else
        info "Role '${db_user}' already exists — updating password."
        _sudo -u postgres psql -c \
            "ALTER USER \"${db_user}\" WITH PASSWORD '${db_pass}';" 2>/dev/null || true
    fi

    # Create database if not exists
    local db_exists
    db_exists=$(_sudo -u postgres psql -tAc \
        "SELECT 1 FROM pg_database WHERE datname='${db_name}'" 2>/dev/null || echo "")
    if [ "$db_exists" != "1" ]; then
        info "Creating database: ${db_name}"
        _sudo -u postgres psql -c \
            "CREATE DATABASE \"${db_name}\" OWNER \"${db_user}\";" 2>/dev/null \
            || { warn "Failed to create database ${db_name}."; return; }
        _sudo -u postgres psql -c \
            "GRANT ALL PRIVILEGES ON DATABASE \"${db_name}\" TO \"${db_user}\";" 2>/dev/null || true
        info "Database '${db_name}' created."
    else
        info "Database '${db_name}' already exists."
    fi

    info "PostgreSQL ready."
}

# ---------------------------------------------------------------------------
# Python deps
# ---------------------------------------------------------------------------
function install_python_deps {
    header "Installing Python dependencies"
    export PATH="$USER_HOME/.local/bin:$PATH"
    cd "$PROJECT_DIR"

    run_cmd "Installing Python packages (poetry)" \
        poetry install --no-root --no-interaction \
        || error_exit "Failed to install Python dependencies"
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
# Wait for app HTTP endpoint to respond
# Usage: wait_for_app PORT "logs hint"
# ---------------------------------------------------------------------------
function wait_for_app {
    local port="${1:-$DEFAULT_PORT}"
    local logs_hint="${2:-}"
    printf "  Waiting for app to start"
    local i=0
    while [ $i -lt 30 ]; do
        if curl -sf "http://localhost:${port}/" >/dev/null 2>&1; then
            echo " OK"
            return 0
        fi
        printf "."
        sleep 1
        i=$(( i + 1 ))
    done
    echo ""
    warn "App did not respond within 30s."
    [ -n "$logs_hint" ] && warn "Check logs: ${logs_hint}"
    return 1
}

# ---------------------------------------------------------------------------
function check_docker {
    if ! command -v docker &>/dev/null; then
        warn "Docker not found."
        if confirm "Install Docker now? (Y/n) " "y"; then
            header "Installing Docker"
            run_cmd "Downloading Docker install script" \
                curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
            run_cmd "Installing Docker" \
                _sudo sh /tmp/get-docker.sh
            rm -f /tmp/get-docker.sh
            # Add current user to docker group so sudo isn't needed
            if ! $IS_ROOT; then
                _sudo usermod -aG docker "$USER_NAME" || true
                warn "Added ${USER_NAME} to docker group — re-login required for group to take effect."
                warn "For now, continuing with sudo..."
                # Use sudo docker for the rest of this session
                DOCKER_CMD="sudo docker"
            fi
        else
            error_exit "Docker is required for Docker install mode."
        fi
    fi
    if ! docker compose version &>/dev/null && ! sudo docker compose version &>/dev/null 2>/dev/null; then
        error_exit "Docker Compose plugin not found. It should be included with Docker — try reinstalling."
    fi
    info "Docker and Docker Compose are available."
}

function ensure_releases_dir_absolute {
    # Docker volumes require an absolute path — expand RELEASES_DIR if relative
    local envfile="$PROJECT_DIR/.env"
    local val
    val=$(_get_env_val "RELEASES_DIR")
    if [ -z "$val" ]; then
        val="NUMParser/public"
    fi
    local abs_val
    if [[ "$val" = /* ]]; then
        abs_val="$val"
    else
        abs_val="$USER_HOME/$val"
    fi
    # Update .env with absolute path
    sed -i "s|^RELEASES_DIR=.*|RELEASES_DIR=${abs_val}|" "$envfile"
    mkdir -p "$abs_val" || true
    info "RELEASES_DIR=${abs_val}"
}

function install_docker_mode {
    check_docker
    clone_or_update_repo
    cd "$PROJECT_DIR"
    setup_env_file
    ensure_releases_dir_absolute

    header "Building and starting Docker containers"
    warn "Building Docker image — это может занять несколько минут..."
    if [ "$VERBOSE" = "1" ]; then
        docker compose -f docker-compose.yml build \
            || error_exit "docker compose build failed"
        docker compose -f docker-compose.yml up -d \
            || error_exit "docker compose up failed"
    else
        run_cmd "Building Docker image" \
            docker compose -f docker-compose.yml build
        run_cmd "Starting containers" \
            docker compose -f docker-compose.yml up -d
    fi
    local docker_port
    docker_port=$(_get_env_val "PORT" || true)
    docker_port="${docker_port:-$DEFAULT_PORT}"
    wait_for_app "$docker_port" "docker compose -f ${PROJECT_DIR}/docker-compose.yml logs"
    echo ""
    docker compose -f docker-compose.yml ps
    info "Docker stack started."
}

# ---------------------------------------------------------------------------
# Main install flow
# ---------------------------------------------------------------------------
function do_install {
    local existing
    existing=$(detect_install_type)
    if [ "$existing" != "none" ]; then
        local already_items=("Update existing installation" "Switch install mode" "Uninstall, then reinstall" "Cancel")
        arrow_menu "Already installed (${existing} mode)" "${already_items[@]}"
        case $MENU_RESULT in
            0) do_update;   return ;;
            1) do_switch;   return ;;
            2) do_uninstall
               warn "Re-running installation..." ;;
            3) info "Cancelled."; return ;;
        esac
    fi

    local mode_items=("Systemd service  (Python + Poetry on the host)" "Docker  (requires Docker + Docker Compose)")
    arrow_menu "Select install mode" "${mode_items[@]}"
    local mode_choice=$(( MENU_RESULT + 1 ))

    case "$mode_choice" in
        1)
            install_system_deps
            check_or_install_python
            install_poetry
            clone_or_update_repo
            cd "$PROJECT_DIR"
            setup_env_file
            setup_postgres

            local svc_port
            svc_port=$(_get_env_val "PORT" || true)
            svc_port="${svc_port:-$DEFAULT_PORT}"

            install_python_deps
            setup_systemd_service "$svc_port"

            wait_for_app "$svc_port" "sudo journalctl -u ${SERVICE_NAME} -f"

            header "Installation complete"
            info "Access URL: http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo localhost):${svc_port}"
            info "Manage:     sudo systemctl {status|restart|stop} ${SERVICE_NAME}"
            info "Logs:       sudo journalctl -u ${SERVICE_NAME} -f"
            info "Nginx HTTPS: ${PROJECT_DIR}/nginx/numparser.conf"
            echo ""
            warn "Парсер данных NUMParser: https://github.com/Igorek1986/NUMParser"
            ;;
        2)
            install_docker_mode

            local host_ip
            host_ip=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
            local port
            port=$(grep "^PORT=" "$PROJECT_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2 | tr -d "'\"" || true)
            port="${port:-$DEFAULT_PORT}"

            header "Installation complete"
            info "Access URL: http://${host_ip}:${port}"
            info "Manage:     docker compose -f ${PROJECT_DIR}/docker-compose.yml {ps|logs|down}"
            info "Nginx HTTPS: ${PROJECT_DIR}/nginx/numparser.conf"
            echo ""
            warn "Парсер данных NUMParser: https://github.com/Igorek1986/NUMParser"
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
    setup_env_file

    case "$install_type" in
        service)
            install_python_deps
            _sudo systemctl restart "$SERVICE_NAME"
            local svc_port
            svc_port=$(_get_env_val "PORT" || true)
            svc_port="${svc_port:-$DEFAULT_PORT}"
            wait_for_app "$svc_port" "sudo journalctl -u ${SERVICE_NAME} -f"
            ;;
        docker)
            cd "$PROJECT_DIR"
            if [ "$VERBOSE" = "1" ]; then
                docker compose -f docker-compose.yml build \
                    || error_exit "docker compose build failed"
                docker compose -f docker-compose.yml up -d \
                    || error_exit "docker compose up failed"
            else
                run_cmd "Rebuilding Docker image" \
                    docker compose -f docker-compose.yml build
                run_cmd "Restarting containers" \
                    docker compose -f docker-compose.yml up -d
            fi
            info "Docker stack rebuilt."
            local docker_port
            docker_port=$(_get_env_val "PORT" || true)
            docker_port="${docker_port:-$DEFAULT_PORT}"
            wait_for_app "$docker_port" "docker compose -f ${PROJECT_DIR}/docker-compose.yml logs"
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
            confirm "Stop systemd service and start Docker stack? (y/N) " "n" \
                || { info "Cancelled."; return; }

            local db_user db_name dump_file
            db_user=$(_get_env_val "DB_USER")
            db_name=$(_get_env_val "DB_NAME")
            dump_file="/tmp/movies_api_dump_$$.sql"

            # Dump host database before stopping service
            local do_migrate=false
            if command -v pg_dump &>/dev/null && [ -n "$db_name" ]; then
                if confirm "Перенести базу данных в Docker? (Y/n) " "y"; then
                    do_migrate=true
                    local dump_log; dump_log=$(mktemp)
                    printf "  ⠋ Создаём дамп базы данных..."
                    if ( cd /tmp && _sudo -u postgres pg_dump "$db_name" ) > "$dump_file" 2>"$dump_log"; then
                        printf "\r  ✓ Дамп создан\n"
                    else
                        printf "\r  ✗ Не удалось создать дамп\n"
                        [ "$VERBOSE" = "1" ] && cat "$dump_log"
                        do_migrate=false
                    fi
                    rm -f "$dump_log"
                fi
            fi

            # Stop and remove service
            _sudo systemctl stop "$SERVICE_NAME" 2>/dev/null || true
            _sudo systemctl disable "$SERVICE_NAME" 2>/dev/null || true
            _sudo rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
            _sudo systemctl daemon-reload 2>/dev/null || true
            info "Systemd service removed."

            # Offer to remove host PostgreSQL
            if command -v psql &>/dev/null; then
                if confirm "Удалить PostgreSQL с хоста? (y/N) " "n"; then
                    [ -n "$db_name" ] && _sudo -u postgres psql -c "DROP DATABASE IF EXISTS \"${db_name}\";" 2>/dev/null || true
                    [ -n "$db_user" ] && _sudo -u postgres psql -c "DROP ROLE IF EXISTS \"${db_user}\";" 2>/dev/null || true
                    run_cmd "Removing PostgreSQL" \
                        _sudo apt-get remove -y --purge postgresql postgresql-contrib
                    _sudo apt-get autoremove -y 2>/dev/null || true
                    info "PostgreSQL удалён."
                fi
            fi

            check_docker
            cd "$PROJECT_DIR"
            ensure_releases_dir_absolute

            if [ "$VERBOSE" = "1" ]; then
                docker compose -f docker-compose.yml build \
                    || error_exit "docker compose build failed"
            else
                run_cmd "Building Docker image" \
                    docker compose -f docker-compose.yml build
            fi

            # Start only postgres first so we can restore dump
            if $do_migrate; then
                run_cmd "Starting postgres container" \
                    docker compose -f docker-compose.yml up -d postgres
                # Wait for postgres container to be ready
                local i=0
                printf "  Waiting for postgres"
                while [ $i -lt 20 ]; do
                    if docker compose -f docker-compose.yml exec -T postgres \
                            pg_isready -U "$db_user" >/dev/null 2>&1; then
                        echo " OK"
                        break
                    fi
                    printf "."; sleep 1; i=$(( i + 1 ))
                done
                local restore_log; restore_log=$(mktemp)
                printf "  ⠋ Restoring database..."
                if docker compose -f docker-compose.yml exec -T postgres \
                        psql -U "$db_user" "$db_name" < "$dump_file" >"$restore_log" 2>&1; then
                    printf "\r  ✓ База данных перенесена\n"
                else
                    printf "\r  ✗ Ошибка восстановления\n"
                    [ "$VERBOSE" = "1" ] && cat "$restore_log"
                    warn "Проверьте данные вручную."
                fi
                rm -f "$dump_file" "$restore_log"
            fi

            run_cmd "Starting containers" \
                docker compose -f docker-compose.yml up -d
            local sw_port
            sw_port=$(_get_env_val "PORT" || true)
            sw_port="${sw_port:-$DEFAULT_PORT}"
            wait_for_app "$sw_port" "docker compose -f ${PROJECT_DIR}/docker-compose.yml logs"
            local host_ip; host_ip=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
            header "Switch complete"
            info "Access URL: http://${host_ip}:${sw_port}"
            info "Manage:     docker compose -f ${PROJECT_DIR}/docker-compose.yml {ps|logs|down}"
            ;;

        docker)
            header "Switching Docker → systemd service"
            confirm "Stop Docker stack and install as systemd service? (y/N) " "n" \
                || { info "Cancelled."; return; }

            cd "$PROJECT_DIR"

            local db_user db_name dump_file
            db_user=$(_get_env_val "DB_USER")
            db_name=$(_get_env_val "DB_NAME")
            dump_file="/tmp/movies_api_dump_$$.sql"

            # Dump from Docker postgres before stopping
            local do_migrate=false
            if [ -n "$db_name" ] && docker compose -f docker-compose.yml ps postgres 2>/dev/null | grep -q "running\|Up"; then
                if confirm "Перенести базу данных из Docker на хост? (Y/n) " "y"; then
                    do_migrate=true
                    local dump_log; dump_log=$(mktemp)
                    printf "  ⠋ Создаём дамп из Docker postgres..."
                    if docker compose -f docker-compose.yml exec -T postgres \
                            pg_dump -U "$db_user" "$db_name" > "$dump_file" 2>"$dump_log"; then
                        printf "\r  ✓ Дамп создан\n"
                    else
                        printf "\r  ✗ Не удалось создать дамп\n"
                        [ "$VERBOSE" = "1" ] && cat "$dump_log"
                        do_migrate=false
                    fi
                    rm -f "$dump_log"
                fi
            fi

            # Stop containers (keep volumes)
            docker compose -f docker-compose.yml down 2>/dev/null || true
            info "Контейнеры остановлены. Docker volumes сохранены."

            # Offer to remove containers and images
            if confirm "Удалить Docker образы? (y/N) " "n"; then
                docker rmi "movies-api-app" "${SERVICE_NAME}-app" 2>/dev/null || true
                local pg_image
                pg_image=$(grep "image:" "$PROJECT_DIR/docker-compose.yml" 2>/dev/null | grep postgres | awk '{print $2}' | tr -d '\r' || true)
                [ -n "$pg_image" ] && docker rmi "$pg_image" 2>/dev/null || true
                info "Docker образы удалены."
            fi

            # Setup host PostgreSQL and restore dump
            setup_postgres
            if $do_migrate && [ -f "$dump_file" ]; then
                local restore_log; restore_log=$(mktemp)
                printf "  ⠋ Restoring database..."
                if ( cd /tmp && _sudo -u postgres psql "$db_name" ) < "$dump_file" >"$restore_log" 2>&1; then
                    printf "\r  ✓ База данных перенесена\n"
                else
                    printf "\r  ✗ Ошибка восстановления\n"
                    [ "$VERBOSE" = "1" ] && cat "$restore_log"
                    warn "Проверьте данные вручную."
                fi
                rm -f "$dump_file" "$restore_log"
            fi

            check_or_install_python
            install_poetry
            install_python_deps

            printf "  Port to listen on [${DEFAULT_PORT}]: "
            read -r svc_port </dev/tty
            svc_port="${svc_port:-$DEFAULT_PORT}"
            setup_systemd_service "$svc_port"
            wait_for_app "$svc_port" "sudo journalctl -u ${SERVICE_NAME} -f"
            local host_ip; host_ip=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
            header "Switch complete"
            info "Access URL: http://${host_ip}:${svc_port}"
            info "Manage:     sudo systemctl {status|restart|stop} ${SERVICE_NAME}"
            info "Logs:       sudo journalctl -u ${SERVICE_NAME} -f"
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

    # Move out of project dir before any deletion to avoid getcwd errors
    cd "$USER_HOME" 2>/dev/null || true

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
                docker compose -f docker-compose.yml down -v 2>/dev/null || true
                info "Docker stack and volumes removed."
            else
                docker compose -f docker-compose.yml down 2>/dev/null || true
                info "Docker stack removed (volumes kept)."
            fi
            if confirm "Remove Docker images for this project? (y/N) " "n"; then
                # Remove app image (built locally)
                docker rmi "movies-api-app" "${SERVICE_NAME}-app" 2>/dev/null || true
                # Remove postgres image used by compose
                local pg_image
                pg_image=$(grep "image:" "$PROJECT_DIR/docker-compose.yml" 2>/dev/null | grep postgres | awk '{print $2}' | tr -d '\r' || true)
                [ -n "$pg_image" ] && docker rmi "$pg_image" 2>/dev/null || true
                info "Docker images removed."
            fi
            ;;
        none)
            warn "No active installation found."
            ;;
    esac

    # PostgreSQL database and user
    if command -v psql &>/dev/null; then
        local db_user db_name
        db_user=$(_get_env_val "DB_USER")
        db_name=$(_get_env_val "DB_NAME")
        if [ -n "$db_name" ]; then
            if confirm "Drop PostgreSQL database '${db_name}'? (y/N) " "n"; then
                _sudo -u postgres psql -c "DROP DATABASE IF EXISTS \"${db_name}\";" 2>/dev/null \
                    && info "Database '${db_name}' dropped." \
                    || warn "Could not drop database '${db_name}'."
            fi
        fi
        if [ -n "$db_user" ]; then
            if confirm "Drop PostgreSQL role '${db_user}'? (y/N) " "n"; then
                _sudo -u postgres psql -c "DROP ROLE IF EXISTS \"${db_user}\";" 2>/dev/null \
                    && info "Role '${db_user}' dropped." \
                    || warn "Could not drop role '${db_user}'."
            fi
        fi
        if confirm "Uninstall PostgreSQL from the system? (y/N) " "n"; then
            run_cmd "Removing PostgreSQL" \
                _sudo apt-get remove -y --purge postgresql postgresql-contrib
            _sudo apt-get autoremove -y 2>/dev/null || true
            info "PostgreSQL uninstalled."
        fi
    fi

    # Project directory
    if [ -d "$PROJECT_DIR" ]; then
        if confirm "Remove project directory ${PROJECT_DIR}? (y/N) " "n"; then
            rm -rf "$PROJECT_DIR"
            info "Project directory removed."
        fi
    fi

    # Poetry and pyenv only relevant for systemd (service) installs
    if [ "$install_type" = "service" ]; then
        if command -v poetry &>/dev/null || [ -f "$USER_HOME/.local/bin/poetry" ]; then
            if confirm "Remove Poetry? (y/N) " "n"; then
                rm -f "$USER_HOME/.local/bin/poetry"
                rm -rf "$USER_HOME/.local/share/pypoetry"
                info "Poetry removed."

                local shell_files=("$USER_HOME/.bashrc" "$USER_HOME/.bash_profile"
                                   "$USER_HOME/.profile" "$USER_HOME/.zshrc" "$USER_HOME/.zprofile")
                for f in "${shell_files[@]}"; do
                    [ -f "$f" ] && sed -i '/poetry/d' "$f" 2>/dev/null || true
                done
                NEED_RELOAD=true
            fi
        fi

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
            docker compose -f "$PROJECT_DIR/docker-compose.yml" ps 2>/dev/null || true
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
# Arrow-key menu
# ---------------------------------------------------------------------------

# arrow_menu TITLE item1 item2 ... — returns selected index in MENU_RESULT
function arrow_menu {
    local title="$1"; shift
    local items=("$@")
    local count=${#items[@]}
    local selected=0

    # Hide cursor
    tput civis 2>/dev/null || true
    trap 'tput cnorm 2>/dev/null || true' EXIT INT TERM

    while true; do
        clear
        echo ""
        echo -e "${BLUE}================================================${NC}"
        printf "${BLUE}  %-44s${NC}\n" "$title"
        echo -e "${BLUE}================================================${NC}"
        echo ""

        for i in "${!items[@]}"; do
            if [ "$i" -eq "$selected" ]; then
                echo -e "  ${GREEN}▶ ${items[$i]}${NC}"
            else
                echo -e "    ${items[$i]}"
            fi
        done
        echo ""
        echo -e "  ${YELLOW}↑↓ — navigate   Enter — select${NC}"

        # Read key
        local key
        IFS= read -rsn1 key
        if [[ "$key" == $'\x1b' ]]; then
            IFS= read -rsn2 key
            case "$key" in
                '[A') selected=$(( (selected - 1 + count) % count )) ;;  # Up
                '[B') selected=$(( (selected + 1) % count ))          ;;  # Down
            esac
        elif [[ "$key" == "" ]]; then
            # Enter
            tput cnorm 2>/dev/null || true
            MENU_RESULT=$selected
            return 0
        fi
    done
}

# ---------------------------------------------------------------------------
# Interactive menu
# ---------------------------------------------------------------------------
function show_menu {
    local install_type
    install_type=$(detect_install_type)

    clear
    if [ "$install_type" = "none" ]; then
        local items=("Install" "Exit")
        arrow_menu "Movies API — Not Installed" "${items[@]}"
        case $MENU_RESULT in
            0) do_install ;;
            1) exit 0 ;;
        esac
    else
        local status_line
        case "$install_type" in
            service) status_line="Installed: systemd service" ;;
            docker)  status_line="Installed: Docker" ;;
        esac

        local switch_label
        [ "$install_type" = "service" ] && switch_label="Switch to Docker" || switch_label="Switch to systemd service"

        local items=("Update" "$switch_label" "Status" "Uninstall" "Exit")
        arrow_menu "Movies API — ${status_line}" "${items[@]}"
        case $MENU_RESULT in
            0) do_update ;;
            1) do_switch ;;
            2) do_status ;;
            3) do_uninstall ;;
            4) exit 0 ;;
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
