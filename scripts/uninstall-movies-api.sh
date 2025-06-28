#!/bin/bash

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check if running as root
if [ "$(id -u)" -eq 0 ]; then
    USER_HOME="/root"
    USER_NAME="root"
else
    USER_HOME="$HOME"
    USER_NAME="$(id -un)"
fi

PROJECT_DIR="$USER_HOME/movies-api"
SERVICE_NAME="movies-api.service"
NEED_RELOAD=false

function error_exit {
    echo -e "${RED}Error: $1${NC}" >&2
    exit 1
}

function info {
    echo -e "${GREEN}$1${NC}"
}

function warning {
    echo -e "${YELLOW}$1${NC}"
}

function confirm {
    read -p "$1 (y/N) " response
    case "$response" in
        [Yy]*) return 0 ;;
        *) return 1 ;;
    esac
}

function reload_shell {
    echo -e "${YELLOW}\nChanges to PATH and environment variables require a shell reload.${NC}"
    if confirm "Would you like to reload the shell now? (Y/n) " "y"; then
        echo -e "${GREEN}Reloading shell...${NC}"
        # Переходим в домашний каталог перед перезагрузкой
        cd ~ || cd /tmp
        exec $SHELL -l
    else
        echo -e "${YELLOW}Please manually reload your shell or run:${NC}"
        echo -e "  source ~/.bashrc (or your shell config file)"
    fi
}

function safe_remove {
    local target="$1"
    local description="$2"

    if [ -e "$target" ] || [ -L "$target" ]; then
        warning "Removing $description..."
        if rm -rf "$target"; then
            info "✓ Successfully removed $description"
            return 0
        else
            warning "⚠ Failed to completely remove $description"
            return 1
        fi
    else
        info "✓ $description not found (already removed)"
        return 0
    fi
}

function check_command {
    local cmd="$1"
    if ! command -v "$cmd" &>/dev/null; then
        info "✓ $cmd not found in PATH (no need to remove)"
        return 1
    fi
    return 0
}

function is_root {
    [ "$(id -u)" -eq 0 ]
}

function run_systemctl {
    if is_root; then
        systemctl "$@"
    else
        sudo systemctl "$@"
    fi
}

function remove_service {
    # Stop service if running
    if systemctl list-unit-files | grep -q "$SERVICE_NAME"; then
        warning "Stopping service..."
        run_systemctl stop "$SERVICE_NAME" 2>/dev/null || warning "Service was not running"

        warning "Disabling service..."
        run_systemctl disable "$SERVICE_NAME" 2>/dev/null || warning "Service was not enabled"
    fi

    # Remove service file
    local SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME"
    safe_remove "$SERVICE_FILE" "service file"

    # Reload systemd if service file existed
    [ -f "$SERVICE_FILE" ] && run_systemctl daemon-reload
}

function clean_shell_configs {
    warning "Cleaning shell configurations..."

    local shell_files=(
        "$USER_HOME/.bashrc"
        "$USER_HOME/.bash_profile"
        "$USER_HOME/.bash_login"
        "$USER_HOME/.profile"
        "$USER_HOME/.zshrc"
        "$USER_HOME/.zprofile"
        "$USER_HOME/.zlogin"
    )

    for rcfile in "${shell_files[@]}"; do
        if [ -f "$rcfile" ]; then
            cp "$rcfile" "${rcfile}.bak" 2>/dev/null

            if [ "$CLEAN_PYENV" -eq 1 ]; then
                if grep -q "pyenv" "$rcfile"; then
                    NEED_RELOAD=true
                fi
                sed -i '/# Pyenv configuration/d;/PYENV_ROOT/d;/pyenv init/d;/pyenv virtualenv-init/d;/\/\.pyenv/d' "$rcfile"
            fi
            if [ "$CLEAN_POETRY" -eq 1 ]; then
                if grep -q "poetry" "$rcfile"; then
                    NEED_RELOAD=true
                fi
                sed -i '/poetry/d' "$rcfile"
            fi
            sed -i '/movies-api/d' "$rcfile"

            # Удалить пустые строки в конце и подряд идущие пустые строки
            sed -i ':a; /^\n*$/{$d;N;ba;}' "$rcfile" 2>/dev/null

            info "✓ Cleaned $rcfile"

            if ! diff -q "$rcfile" "${rcfile}.bak" &>/dev/null; then
                warning "Changes made to $rcfile:"
                diff --color=always "$rcfile" "${rcfile}.bak" | grep -E '^>|^<' || true
                rm -f "${rcfile}.bak"
            else
                rm -f "${rcfile}.bak"
            fi
        fi
    done
}

function show_summary {
    warning "\n=== Final Status ==="

    # Service status
    if systemctl list-unit-files | grep -q "$SERVICE_NAME"; then
        warning "⚠ Service still registered (try rebooting)"
    else
        info "✓ Service completely removed"
    fi

    # Project files
    if [ -d "$PROJECT_DIR" ]; then
        warning "⚠ Project directory still exists at $PROJECT_DIR"
    else
        info "✓ Project directory removed"
    fi

    # Python environments
    if check_command "pyenv"; then
        warning "pyenv still available at: $(command -v pyenv)"
    elif [ -d "$USER_HOME/.pyenv" ]; then
        warning "⚠ pyenv directory remains at $USER_HOME/.pyenv"
    else
        info "✓ pyenv completely removed"
    fi

    if check_command "poetry"; then
        warning "poetry still available at: $(command -v poetry)"
    elif [ -f "$USER_HOME/.local/bin/poetry" ]; then
        warning "⚠ poetry binary remains at $USER_HOME/.local/bin/poetry"
    else
        info "✓ poetry completely removed"
    fi
}

# Main execution
warning "Starting Movies API uninstallation..."

# Phase 1: Remove service and project files
remove_service
safe_remove "$PROJECT_DIR" "project directory"

# Phase 2: Optional cleanup
info "\n=== Optional Components Removal ==="

CLEAN_PYENV=0
CLEAN_POETRY=0

if confirm "Remove Poetry package manager?"; then
    safe_remove "$USER_HOME/.local/bin/poetry" "Poetry binary"
    safe_remove "$USER_HOME/.local/share/pypoetry" "Poetry data"
    CLEAN_POETRY=1
fi

if confirm "Remove pyenv and Python versions?"; then
    safe_remove "$USER_HOME/.pyenv" "pyenv directory"
    CLEAN_PYENV=1
fi

# Clean shell configurations
clean_shell_configs

# Final report
show_summary

if $NEED_RELOAD; then
    reload_shell
else
    warning "\nNote: Some changes may require:"
    warning "1. Opening a new terminal session"
    warning "2. System reboot (for complete service removal)"
fi

info "\n✔ Uninstallation process completed!"