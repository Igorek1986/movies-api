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
SERVICE_NAME="movies-api@$USER_NAME.service"

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

function check_pyenv_installed {
    if [ -d "$USER_HOME/.pyenv" ]; then
        echo -e "${GREEN}pyenv is already installed${NC}"
        return 0
    else
        return 1
    fi
}

function check_poetry_installed {
    if command -v poetry &> /dev/null; then
        echo -e "${GREEN}Poetry is already installed${NC}"
        return 0
    elif [ -f "$USER_HOME/.local/bin/poetry" ]; then
        echo -e "${GREEN}Poetry is installed in ~/.local/bin${NC}"
        return 0
    else
        return 1
    fi
}

function install_pyenv {
    if check_pyenv_installed; then
        return
    fi

    echo -e "${YELLOW}Installing pyenv...${NC}"
    curl -sSL https://pyenv.run | bash || error_exit "Failed to install pyenv"

    local SHELL_CONFIG=$(get_shell_config)
    
    # Add pyenv to shell configuration
    if ! grep -q "pyenv init" "$SHELL_CONFIG"; then
        cat <<EOF >> "$SHELL_CONFIG"
# Pyenv configuration
export PYENV_ROOT="$USER_HOME/.pyenv"
export PATH="\$PYENV_ROOT/bin:\$PATH"
eval "\$(pyenv init --path)"
eval "\$(pyenv init -)"
eval "\$(pyenv virtualenv-init -)"
EOF
    fi

    # Source the configuration
    export PYENV_ROOT="$USER_HOME/.pyenv"
    export PATH="$PYENV_ROOT/bin:$PATH"
    eval "$(pyenv init --path)"
    eval "$(pyenv init -)"
    eval "$(pyenv virtualenv-init -)"
}

function install_python {
    echo -e "${YELLOW}Installing Python 3.13.5...${NC}"
    pyenv install 3.13.5 --skip-existing || error_exit "Failed to install Python 3.13.5"
    pyenv global 3.13.5
}

function install_poetry {
    if check_poetry_installed; then
        return
    fi

    echo -e "${YELLOW}Installing Poetry...${NC}"
    curl -sSL https://install.python-poetry.org | python3 - || error_exit "Failed to install Poetry"
    
    # Add Poetry to PATH in the appropriate shell config
    local SHELL_CONFIG=$(get_shell_config)
    if ! grep -q ".local/bin" "$SHELL_CONFIG"; then
        echo "export PATH=\"$USER_HOME/.local/bin:\$PATH\"" >> "$SHELL_CONFIG"
    fi
    
    export PATH="$USER_HOME/.local/bin:$PATH"
}

function setup_project {
    echo -e "${YELLOW}Setting up project...${NC}"
    [ ! -d "$PROJECT_DIR" ] && mkdir -p "$PROJECT_DIR"
    cd "$PROJECT_DIR" || error_exit "Could not enter project directory"

    # Clone repository if not already present
    if [ ! -d ".git" ]; then
        echo -e "${YELLOW}Cloning repository...${NC}"
        git clone https://github.com/your-repository/movies-api.git . || error_exit "Failed to clone repository"
    fi

    poetry install --no-root || error_exit "Failed to install dependencies"
}

function setup_systemd {
    echo -e "${YELLOW}Configuring systemd service...${NC}"

    # Create service file
    SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME"
    sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Movies API Service for $USER_NAME
After=network.target redis-server.service

[Service]
User=$USER_NAME
Group=$USER_NAME
WorkingDirectory=$PROJECT_DIR
Environment="PATH=$USER_HOME/.pyenv/shims:$USER_HOME/.pyenv/bin:$USER_HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

ExecStart=$USER_HOME/.local/bin/poetry run uvicorn app.main:app --host 0.0.0.0 --port 38888

Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable "$SERVICE_NAME"
    sudo systemctl start "$SERVICE_NAME"
}

# Main installation process
echo -e "${GREEN}Starting Movies API installation...${NC}"

# Install dependencies
if $IS_ROOT; then
    apt-get update && apt-get install -y git curl make build-essential libssl-dev zlib1g-dev \
    libbz2-dev libreadline-dev libsqlite3-dev wget llvm libncurses5-dev libncursesw5-dev \
    xz-utils tk-dev libffi-dev liblzma-dev python3-openssl || error_exit "Failed to install system dependencies"
else
    sudo apt-get update && sudo apt-get install -y git curl make build-essential libssl-dev zlib1g-dev \
    libbz2-dev libreadline-dev libsqlite3-dev wget llvm libncurses5-dev libncursesw5-dev \
    xz-utils tk-dev libffi-dev liblzma-dev python3-openssl || error_exit "Failed to install system dependencies"
fi

install_pyenv
install_python
install_poetry
setup_project
setup_systemd

echo -e "${GREEN}Installation completed successfully!${NC}"
echo -e "Service is running as ${YELLOW}$SERVICE_NAME${NC}"
echo -e "Check status with: ${YELLOW}sudo systemctl status $SERVICE_NAME${NC}"