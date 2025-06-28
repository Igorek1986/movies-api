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
    # 1. Проверяем наличие pyenv в PATH и работоспособность
    if command -v pyenv >/dev/null 2>&1 && pyenv version >/dev/null 2>&1; then
        echo -e "${GREEN}pyenv is properly installed and functional${NC}"
        echo -e "  Version: $(pyenv --version 2>/dev/null || echo 'unknown')"
        return 0
    fi

    # 2. Если pyenv не в PATH, но есть каталог ~/.pyenv
    if [ -d "$USER_HOME/.pyenv" ]; then
        # 3. Проверяем есть ли установленные Python-версии
        if [ -d "$USER_HOME/.pyenv/versions" ] && [ -n "$(ls -A "$USER_HOME/.pyenv/versions")" ]; then
            echo -e "${YELLOW}Found existing pyenv installation with Python versions${NC}"
            return 1
        else
            # 4. Если Python-версий нет - чистим старую установку
            echo -e "${YELLOW}Removing broken pyenv installation...${NC}"
            rm -rf "$USER_HOME/.pyenv"
            return 1
        fi
    fi

    return 1
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
    create_project_dir
    clone_repository
    setup_env_file
    install_dependencies
}

function create_project_dir {
    [ ! -d "$PROJECT_DIR" ] && mkdir -p "$PROJECT_DIR"
    cd "$PROJECT_DIR" || error_exit "Could not enter project directory"
}

function clone_repository {
    if [ ! -d ".git" ]; then
        echo -e "${YELLOW}Cloning repository...${NC}"
        git clone https://github.com/Igorek1986/movies-api.git . || error_exit "Failed to clone repository"
    fi
}

function setup_env_file {
    # Создаем или используем существующий .env файл
    if [ ! -f ".env" ]; then
        echo -e "${YELLOW}Creating new .env file from template...${NC}"
        create_env_file_from_template
    else
        echo -e "${GREEN}Using existing .env file${NC}"
        # Для существующего файла проверяем обязательные параметры
        ensure_env_parameters
    fi

    # Всегда генерируем новый пароль для безопасности
    configure_cache_password
    
    # Всегда проверяем настройки релизов
    configure_releases_dir
    
    # Всегда предлагаем настройку токена
    setup_tmdb_token
}

function ensure_env_parameters {
    # Добавляем отсутствующие обязательные параметры
    if ! grep -q "^CACHE_CLEAR_PASSWORD=" .env; then
        echo -e "${YELLOW}Adding missing CACHE_CLEAR_PASSWORD...${NC}"
        echo "CACHE_CLEAR_PASSWORD=''" >> .env
    fi
    
    if ! grep -q "^RELEASES_DIR=" .env; then
        echo -e "${YELLOW}Adding missing RELEASES_DIR...${NC}"
        echo "RELEASES_DIR='releases/'" >> .env
    fi
    
    if ! grep -q "^TMDB_TOKEN=" .env; then
        echo -e "${YELLOW}Adding missing TMDB_TOKEN...${NC}"
        echo "TMDB_TOKEN='Bearer TOKEN'" >> .env
    fi
}

function create_env_file_from_template {
    echo -e "${YELLOW}Creating .env configuration file...${NC}"
    
    if [ -f ".env.template" ]; then
        cp .env.template .env
    else
        echo -e "${RED}Warning: .env.template not found, creating empty .env${NC}"
        touch .env
    fi
}

function configure_cache_password {
    # Проверяем текущий пароль
    local current_pass=$(grep -oP "^CACHE_CLEAR_PASSWORD='?\K[^']*" .env 2>/dev/null)
    
    # Если пароль дефолтный (PASSWORD) или отсутствует - генерируем новый автоматически
    if [[ "$current_pass" == "PASSWORD" || -z "$current_pass" ]]; then
        local new_pass=$(openssl rand -base64 16 | tr -dc 'a-zA-Z0-9' | head -c 12)
        if grep -q "^CACHE_CLEAR_PASSWORD=" .env; then
            sed -i "s|^CACHE_CLEAR_PASSWORD=.*|CACHE_CLEAR_PASSWORD='${new_pass}'|" .env
        else
            echo "CACHE_CLEAR_PASSWORD='${new_pass}'" >> .env
        fi
        echo -e "${GREEN}Auto-generated cache password: ${new_pass}${NC}"
    else
        echo -e "${GREEN}Custom cache password already configured${NC}"
    fi
}

function configure_releases_dir {
    local REL_PATH="releases/"
    sed -i "s|RELEASES_DIR=.*|RELEASES_DIR=${REL_PATH}|" .env
}

function setup_tmdb_token {
    echo -e "\n${GREEN}=== TMDB Token Configuration ==="
    echo -e "==============================${NC}"
    
    local current_token=$(grep -oP "TMDB_TOKEN='\K[^']*" .env 2>/dev/null || echo "Bearer TOKEN")
    
    # Проверка валидности токена
    if [[ "$current_token" == "Bearer TOKEN" || ! "$current_token" =~ ^Bearer\ [^[:space:]]+$ ]]; then
        echo -e "${YELLOW}Current token: ${current_token}${NC}"
        echo -e "${RED}Warning: Invalid or default token detected${NC}"
        
        if confirm "Update token now? (y/N) " "n"; then
            prompt_tmdb_token
        else
            echo -e "${YELLOW}You must update the token later in:${NC}"
            echo -e "${PROJECT_DIR}/.env"
        fi
    else
        echo -e "${GREEN}Valid token already configured${NC}"
        if confirm "Update existing token? (y/N) " "n"; then
            prompt_tmdb_token
        fi
    fi
}

function confirm {
    local prompt="$1"
    local default="${2:-n}"
    read -p "$prompt" response
    case "${response:-$default}" in
        [Yy]*) return 0 ;;
        *) return 1 ;;
    esac
}

function prompt_tmdb_token {
    while true; do
        echo -e "\n${YELLOW}Enter new TMDB Bearer Token (format: Bearer YourTokenWithoutSpaces):${NC}"
        echo -n "Leave empty and press Enter to keep the default token: "
        read -e tmdb_token

        if [[ -z "$tmdb_token" ]]; then
            echo -e "${YELLOW}No token entered. Default token will be used.${NC}"
            return 0
        fi

        if [[ "$tmdb_token" =~ ^Bearer\ [^[:space:]]+$ ]]; then
            sed -i "s|^TMDB_TOKEN='.*'|TMDB_TOKEN='${tmdb_token}'|" .env
            echo -e "${GREEN}Token updated successfully!${NC}"
            return 0
        else
            echo -e "${RED}Invalid format! Must start with 'Bearer' and contain no extra spaces.${NC}"
        fi
    done
}


function validate_tmdb_token {
    local token="$1"
    local valid=true
    
    # Проверяем что токен начинается с Bearer и пробела
    if [[ ! "$token" =~ ^Bearer[[:space:]] ]]; then
        echo -e "${RED}Error: Token must start with 'Bearer ' (with space)${NC}"
        valid=false
    fi
    
    # Проверяем что после Bearer есть хотя бы один символ
    if [[ "$token" == "Bearer " || "$token" == "Bearer" ]]; then
        echo -e "${RED}Error: Token value is missing after 'Bearer'${NC}"
        valid=false
    fi
    
    # Проверяем что нет дополнительных пробелов в самом токене
    if [[ "$token" =~ ^Bearer[[:space:]].*[[:space:]] ]]; then
        echo -e "${RED}Error: Token must not contain spaces after the initial 'Bearer '${NC}"
        valid=false
    fi
    
    if ! $valid; then
        echo -e "${YELLOW}Example valid token format: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...${NC}"
        echo -e "${YELLOW}Note: The token part should not contain any spaces${NC}"
    fi
    
    $valid
}

function save_tmdb_token {
    local token="$1"
    sed -i "s|TMDB_TOKEN='Bearer TOKEN'|TMDB_TOKEN='${token}'|" .env
    echo -e "${GREEN}TMDB Token saved successfully!${NC}"
}

function show_tmdb_token_instructions {
    echo -e "${YELLOW}You can add TMDB Token later in:${NC}"
    echo -e "$PROJECT_DIR/.env"
    echo -e "${YELLOW}Modify the line:${NC}"
    echo -e "TMDB_TOKEN='Bearer TOKEN'"
}

function install_dependencies {
    poetry install --no-root || error_exit "Failed to install dependencies"
}

function setup_systemd {
    # Ask for port number
    echo -e "\n${GREEN}=== Service Port Configuration ===${NC}"
    read -p "Enter port number [${DEFAULT_PORT}]: " SERVICE_PORT
    SERVICE_PORT=${SERVICE_PORT:-$DEFAULT_PORT}

    echo -e "${YELLOW}Configuring systemd service on port ${SERVICE_PORT}...${NC}"

    # Create service file
    SERVICE_FILE="/etc/systemd/system/movies-api.service"
    sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Movies API Service
After=network.target redis-server.service

[Service]
User=$USER_NAME
Group=$USER_NAME
WorkingDirectory=$PROJECT_DIR
Environment="PATH=$USER_HOME/.pyenv/shims:$USER_HOME/.pyenv/bin:$USER_HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
EnvironmentFile=$PROJECT_DIR/.env
ExecStart=$USER_HOME/.local/bin/poetry run uvicorn app.main:app --host 0.0.0.0 --port $SERVICE_PORT

Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable movies-api
    sudo systemctl start movies-api
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