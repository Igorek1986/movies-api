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

####  Установка
```bash
curl https://raw.githubusercontent.com/Igorek1986/movies-api/refs/heads/main/scripts/install-movies-api.sh | bash
```


1. Поместите JSON-файлы в ~/releases/
```bash
cd ~/releases/
```
#### Удаление
```bash
curl https://raw.githubusercontent.com/Igorek1986/movies-api/refs/heads/main/scripts/uninstall-movies-api.sh | bash
```

```bash
RELEASES_DIR=$(pwd | sed "s|$HOME/||") && \
if [ -f ~/movies-api/.env ]; then \
    if grep -q "^RELEASES_DIR=" ~/movies-api/.env; then \
        sed -i "s|^RELEASES_DIR=.*|RELEASES_DIR=$RELEASES_DIR|" ~/movies-api/.env; \
    else \
        echo "RELEASES_DIR=$RELEASES_DIR" >> ~/movies-api/.env; \
    fi; \
else \
    echo "RELEASES_DIR=$RELEASES_DIR" > ~/movies-api/.env; \
fi
```

##### Опцианально
2. Настройка Nginx
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
curl http://localhost:8888/movies_id_2025?page=1&per_page=20&language=ru
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
curl http://localhost:8888/
```  
Ответ:

```json
{"status": "ok", "message": "NUMParser API работает"}
```  

#### Управление кэшем
##### Очистка кэша


```
curl -X POST http://localhost:8888/cache/clear -H "X-Password: ваш_пароль"
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
curl -X GET http://localhost:8888/cache/info
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
GET curl -X GET http://localhost:8888/cache/path
```
Ответ:

```json
{
  "cache_path": "/path/to/tmdb_cache.json",
  "exists": true,
  "size": 2456789
}
```
