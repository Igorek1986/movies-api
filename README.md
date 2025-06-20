Установка Poetry
```Shell
curl -sSL https://install.python-poetry.org | python3 -

# добавить в zshrc 
# export PATH="/root/.local/bin:$PATH"
# plugins(
#	poetry
#	...
#	)

source ~/.zshrc
mkdir $ZSH_CUSTOM/plugins/poetry
poetry completions zsh > $ZSH_CUSTOM/plugins/poetry/_poetry
```

### Вариант 2: Без Docker (через systemd)

1. Установите зависимости:
```bash
sudo apt update
sudo apt install -y redis-server nginx git
```
2. Склонируйте репозиторий:
```bash
git clone https://github.com/Igorek1986/movies-api.git
cd movies-api
```
3. Установка зависимостей
```bash
cd ~/movies-api
eval $(poetry env activate)
poetry install --no-root
```
4. Поместите JSON-файлы в /~/releases/
5. Настройка systemd службы (Замените пользователя и путь)
```bash
sudo cp scripts/movies-api.service /etc/systemd/system/
sudo vim /etc/systemd/system/movies-api.service
sudo systemctl daemon-reload
sudo systemctl start movies-api
sudo systemctl status movies-api
sudo systemctl enable movies-api
```
5. Настройка Nginx
```bash
sudo cp nginx/numparser.conf /etc/nginx/sites-available/
sudo ln -sf /etc/nginx/sites-available/numparser.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl restart nginx
# Добавить сертификат через certbot
```