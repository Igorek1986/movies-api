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
    IS_ROOT=true
else
    USER_HOME="$HOME"
    USER_NAME="$(id -un)"
    IS_ROOT=false
fi

PROJECT_DIR="$USER_HOME/movies-api"
DEFAULT_PORT=8888
PYTHON_MIN_VERSION=3.10
USE_PYENV=false
NEED_RELOAD=false

function error_exit {
    echo -e "${RED}Error: $1${NC}" >&2
    exit 1
}

function get_shell_config {
    if [ -n "$ZSH_VERSION" ]; then
        if [ -f "$USER_HOME/.zprofile" ]; then
            echo "$USER_HOME/.zprofile"
        else
            echo "$USER_HOME/.zshrc"
        fi
    else
        if [ -f "$USER_HOME/.profile" ]; then
            echo "$USER_HOME/.profile"
        else
            echo "$USER_HOME/.bashrc"
        fi
    fi
}

function check_path {
    # Проверяем, добавлены ли нужные пути в PATH
    if [[ ":$PATH:" != *":$USER_HOME/.local/bin:"* ]] ||
       [[ "$USE_PYENV" == "true" && ":$PATH:" != *":$USER_HOME/.pyenv/bin:"* ]]; then
        NEED_RELOAD=true
    fi
}

function reload_shell {
    echo -e "${YELLOW}\nChanges to PATH and environment variables require a shell reload.${NC}"
    if confirm "Would you like to reload the shell now? (Y/n) " "y"; then
        echo -e "${GREEN}Reloading shell...${NC}"
        exec $SHELL -l
    else
        echo -e "${YELLOW}Please manually reload your shell or run:${NC}"
        echo -e "  source $(get_shell_config)"
    fi
}

function check_system_python {
    echo -e "${YELLOW}Checking system Python...${NC}"

    if command -v python3 &>/dev/null; then
        local python_version=$(python3 -c "import sys; print('{}.{}'.format(sys.version_info.major, sys.version_info.minor))")
        local version_ok=$(python3 -c "import sys; print(1 if sys.version_info >= (3, 10) else 0)")

        if [ "$version_ok" -eq 1 ]; then
            echo -e "${GREEN}Found Python ${python_version} (meets minimum requirement ${PYTHON_MIN_VERSION}+)${NC}"
            return 0
        else
            echo -e "${YELLOW}Found Python ${python_version} (below minimum requirement ${PYTHON_MIN_VERSION}+)${NC}"
            return 1
        fi
    else
        echo -e "${YELLOW}Python 3 not found in system${NC}"
        return 1
    fi
}

[Остальные функции остаются без изменений...]

# Main installation process
echo -e "${GREEN}Starting Movies API installation...${NC}"

# Install system dependencies
if $IS_ROOT; then
    apt-get update && apt-get install -y git curl make build-essential libssl-dev zlib1g-dev \
    libbz2-dev libreadline-dev libsqlite3-dev wget llvm libncurses5-dev libncursesw5-dev \
    xz-utils tk-dev libffi-dev liblzma-dev python3-openssl || error_exit "Failed to install system dependencies"
else
    sudo apt-get update && sudo apt-get install -y git curl make build-essential libssl-dev zlib1g-dev \
    libbz2-dev libreadline-dev libsqlite3-dev wget llvm libncurses5-dev libncursesw5-dev \
    xz-utils tk-dev libffi-dev liblzma-dev python3-openssl || error_exit "Failed to install system dependencies"
fi

# Check Python and decide whether to use pyenv
if check_system_python; then
    echo -e "${GREEN}System Python meets requirements${NC}"
    if confirm "Would you like to use pyenv for Python management anyway? (y/N) " "n"; then
        USE_PYENV=true
        install_pyenv
        install_python_with_pyenv
    else
        install_python_system
    fi
else
    echo -e "${YELLOW}System Python doesn't meet requirements or not found${NC}"
    USE_PYENV=true
    install_pyenv
    install_python_with_pyenv
fi

install_poetry
setup_project
setup_systemd

# Проверяем необходимость перезагрузки shell
check_path

# Final instructions
echo -e "\n${GREEN}=== Installation Completed Successfully! ===${NC}"
echo -e "Service is running as ${YELLOW}movies-api${NC}"
echo -e "Access URL: ${YELLOW}http://$(hostname -I | awk '{print $1}'):${SERVICE_PORT}${NC}"
echo -e "\n${GREEN}Management commands:${NC}"
echo -e "Check status: ${YELLOW}sudo systemctl status movies-api${NC}"
echo -e "Restart service: ${YELLOW}sudo systemctl restart movies-api${NC}"
echo -e "View logs: ${YELLOW}sudo journalctl -u movies-api -f${NC}"
echo -e "\n${GREEN}Important paths:${NC}"
echo -e "Project directory: ${YELLOW}${PROJECT_DIR}${NC}"
echo -e "Environment file: ${YELLOW}${PROJECT_DIR}/.env${NC}"
echo -e "Releases directory: ${YELLOW}${USER_HOME}/releases/${NC}"
echo -e "\n${YELLOW}Don't forget to add your JSON files to the releases directory!${NC}"

# Предлагаем перезагрузить shell если нужно
if $NEED_RELOAD; then
    reload_shell
fi