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

sudo sed -i 's/^supervised no/supervised systemd/' /etc/redis/redis.conf
sudo sed -i 's/^# maxmemory .*/maxmemory 256mb/' /etc/redis/redis.conf
sudo sed -i 's/^# maxmemory-policy .*/maxmemory-policy allkeys-lru/' /etc/redis/redis.conf

sudo systemctl restart redis
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

### Использование API
#### Основные эндпоинты
##### Получение данных по категории


```
curl http://localhost:8000/movies_id_2025?page=1&per_page=20&language=ru
```
Параметры:

* category - название категории (например: movies_id_2025)
* page - номер страницы (по умолчанию: 1)
* per_page - элементов на странице (по умолчанию: 20)
* language - язык (по умолчанию: ru)

Пример ответа:

```json
{
  "page": 1,
  "results": [...],
  "total_pages": 10,
  "total_results": 200
}
```
##### Проверка здоровья API


```
curl http://localhost:8000/
```  
Ответ:

```json
{"status": "ok", "message": "NUMParser API работает"}
```  

#### Управление кэшем
##### Очистка кэша


```
curl -X POST http://localhost:8000/cache/clear -H "X-Password: ваш_пароль"
```

Ответ при успехе:

```
Кэш успешно очищен

```

При ошибке:


```
Неверный пароль для очистки кэша
```


##### Информация о кэше

text
```
curl -X GET http://localhost:8000/cache/info
```
Пример ответа:

```json
{
  "cache_size": 754,
  "cache_size_mb": 2.34,
  "sample_keys": [["movie", 123], ["tv", 456]]
}
```

##### Путь к файлу кэша


```
GET curl -X GET http://localhost:8000/cache/path
```
Ответ:

```json
{
  "cache_path": "/path/to/tmdb_cache.json",
  "exists": true,
  "size": 2456789
}
```
