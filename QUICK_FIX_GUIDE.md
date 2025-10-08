# ⚡ Швидкий посібник з виправлень

## 🎯 Мета

Цей документ містить **готові до використання** виправлення для критичних проблем безпеки. Виконайте ці кроки **якомога швидше**.

⏱️ **Час виконання:** 2-3 години

---

## 🚨 Критичні виправлення

### 1. Увімкнути SSL верифікацію (15 хвилин)

#### discord_monitor.py

**Знайти (рядки 24-27):**
```python
ssl_context = ssl.create_default_context()
ssl_context.check_hostname = False
ssl_context.verify_mode = ssl.CERT_NONE
```

**Замінити на:**
```python
import certifi

ssl_context = ssl.create_default_context(cafile=certifi.where())
ssl_context.check_hostname = True
ssl_context.verify_mode = ssl.CERT_REQUIRED
```

#### twitter_monitor.py

**Знайти (рядки 86-90):**
```python
ssl_context = ssl.create_default_context()
ssl_context.check_hostname = False
ssl_context.verify_mode = ssl.CERT_NONE
```

**Замінити на:**
```python
import certifi

ssl_context = ssl.create_default_context(cafile=certifi.where())
ssl_context.check_hostname = True
ssl_context.verify_mode = ssl.CERT_REQUIRED
```

#### selenium_twitter_monitor.py

**Знайти (рядки 13-14):**
```python
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
```

**Видалити або закоментувати** цей рядок

**Встановити certifi:**
```bash
pip install certifi
```

---

### 2. Покращити хешування паролів (30 хвилин)

#### Встановити bcrypt
```bash
pip install bcrypt
```

#### access_manager.py

**Знайти (рядок 50-52):**
```python
def _hash_password(self, password: str) -> str:
    """Хешувати пароль"""
    return hashlib.sha256(password.encode()).hexdigest()
```

**Замінити на:**
```python
import bcrypt

def _hash_password(self, password: str) -> str:
    """Хешувати пароль"""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def _verify_password(self, password: str, password_hash: str) -> bool:
    """Перевірити пароль"""
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except Exception:
        return False
```

**Знайти метод authenticate() і оновити:**
```python
def authenticate(self, telegram_id: int, password: str) -> bool:
    """Автентифікувати користувача"""
    user = self.get_user_by_telegram_id(telegram_id)
    if not user:
        return False
    
    # Використовуємо новий метод верифікації
    return self._verify_password(password, user['password_hash'])
```

#### Міграція існуючих паролів

**Створити скрипт migrate_passwords.py:**
```python
#!/usr/bin/env python3
"""Міграція паролів з SHA-256 на bcrypt"""
import json
import bcrypt
import os
from datetime import datetime

def migrate_passwords():
    # Backup
    backup_file = f"access_data_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    os.system(f"cp access_data.json {backup_file}")
    print(f"✅ Backup створено: {backup_file}")
    
    # Завантажити дані
    with open('access_data.json', 'r') as f:
        data = json.load(f)
    
    # ВАЖЛИВО: Вам потрібно знати оригінальні паролі!
    # Якщо не знаєте - попросіть користувачів встановити нові
    print("⚠️  Для міграції потрібні оригінальні паролі!")
    print("Варіант 1: Ввести паролі вручну")
    print("Варіант 2: Скинути паролі (користувачі встановлять нові)")
    
    choice = input("Виберіть варіант (1/2): ")
    
    if choice == "1":
        # Міграція з введенням паролів
        for user_id, user_data in data['users'].items():
            username = user_data.get('username', user_id)
            password = input(f"Введіть пароль для {username}: ")
            
            # Хешуємо новим способом
            new_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
            user_data['password_hash'] = new_hash
            print(f"✅ Пароль для {username} оновлено")
    
    elif choice == "2":
        # Скидання паролів
        default_password = "ChangeMe123!"
        new_hash = bcrypt.hashpw(default_password.encode(), bcrypt.gensalt()).decode()
        
        for user_id, user_data in data['users'].items():
            user_data['password_hash'] = new_hash
            user_data['password_reset_required'] = True
        
        print(f"✅ Всі паролі скинуті на: {default_password}")
        print("⚠️  Користувачі повинні змінити пароль при наступному вході!")
    
    # Зберегти
    with open('access_data.json', 'w') as f:
        json.dump(data, f, indent=2)
    
    print("✅ Міграція завершена!")

if __name__ == '__main__':
    migrate_passwords()
```

**Запустити міграцію:**
```bash
python migrate_passwords.py
```

---

### 3. Перенести токени з коду в .env (10 хвилин)

#### config.py

**Додати в кінець файлу:**
```python
# Twitter API
TWITTER_BEARER_TOKEN = os.getenv('TWITTER_BEARER_TOKEN', 
    'AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA')
```

#### twitter_monitor.py

**Знайти (рядок 76):**
```python
'Authorization': 'Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA',
```

**Замінити на:**
```python
from config import TWITTER_BEARER_TOKEN

# У headers:
'Authorization': f'Bearer {TWITTER_BEARER_TOKEN}',
```

#### .env

**Додати в .env файл:**
```env
# Twitter Bearer Token
TWITTER_BEARER_TOKEN=AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA
```

---

### 4. Видалити дефолтні паролі (5 хвилин)

#### config.py

**Знайти:**
```python
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', '401483')
```

**Замінити на:**
```python
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD')
if not ADMIN_PASSWORD:
    raise ValueError(
        "❌ ADMIN_PASSWORD не встановлено!\n"
        "Додайте його в .env файл:\n"
        "ADMIN_PASSWORD=ваш_безпечний_пароль"
    )
```

#### .env

**Переконатися що є:**
```env
ADMIN_PASSWORD=ваш_дуже_безпечний_пароль_тут
```

---

### 5. Оновити requirements.txt (2 хвилини)

**Додати в requirements.txt:**
```txt
bcrypt>=4.0.0
certifi>=2023.0.0
```

**Встановити:**
```bash
pip install -r requirements.txt
```

---

## ✅ Перевірка виправлень

### Скрипт перевірки (create_test.py)

```python
#!/usr/bin/env python3
"""Перевірка критичних виправлень"""

def test_ssl_verification():
    """Перевірити SSL верифікацію"""
    print("🔍 Перевірка SSL верифікації...")
    
    # Перевірка discord_monitor.py
    with open('discord_monitor.py', 'r') as f:
        content = f.read()
        if 'CERT_NONE' in content:
            print("❌ discord_monitor.py: SSL верифікація вимкнена!")
            return False
        if 'certifi' in content:
            print("✅ discord_monitor.py: SSL верифікація увімкнена")
    
    # Перевірка twitter_monitor.py
    with open('twitter_monitor.py', 'r') as f:
        content = f.read()
        if 'CERT_NONE' in content:
            print("❌ twitter_monitor.py: SSL верифікація вимкнена!")
            return False
        if 'certifi' in content:
            print("✅ twitter_monitor.py: SSL верифікація увімкнена")
    
    return True

def test_password_hashing():
    """Перевірити хешування паролів"""
    print("\n🔍 Перевірка хешування паролів...")
    
    with open('access_manager.py', 'r') as f:
        content = f.read()
        if 'bcrypt' in content:
            print("✅ access_manager.py: Використовується bcrypt")
            return True
        else:
            print("❌ access_manager.py: bcrypt не знайдено!")
            return False

def test_token_in_env():
    """Перевірити токени в .env"""
    print("\n🔍 Перевірка токенів...")
    
    with open('twitter_monitor.py', 'r') as f:
        content = f.read()
        if 'AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs' in content:
            print("❌ twitter_monitor.py: Bearer токен залишився в коді!")
            return False
    
    with open('config.py', 'r') as f:
        content = f.read()
        if 'TWITTER_BEARER_TOKEN' in content:
            print("✅ config.py: Bearer токен в змінній оточення")
            return True
        else:
            print("❌ config.py: TWITTER_BEARER_TOKEN не знайдено!")
            return False

def test_default_passwords():
    """Перевірити дефолтні паролі"""
    print("\n🔍 Перевірка дефолтних паролів...")
    
    with open('config.py', 'r') as f:
        content = f.read()
        if "'401483'" in content or '"401483"' in content:
            print("❌ config.py: Дефолтний пароль залишився!")
            return False
        if 'raise ValueError' in content and 'ADMIN_PASSWORD' in content:
            print("✅ config.py: Дефолтний пароль видалено")
            return True
        else:
            print("⚠️  config.py: Перевірте вручну")
            return True

def main():
    """Головна функція"""
    print("=" * 50)
    print("🔐 Перевірка критичних виправлень безпеки")
    print("=" * 50)
    
    results = []
    
    results.append(test_ssl_verification())
    results.append(test_password_hashing())
    results.append(test_token_in_env())
    results.append(test_default_passwords())
    
    print("\n" + "=" * 50)
    if all(results):
        print("✅ Всі перевірки пройдені успішно!")
        print("🎉 Критичні проблеми безпеки виправлені!")
    else:
        print("❌ Деякі перевірки не пройдені")
        print("📖 Перегляньте QUICK_FIX_GUIDE.md")
    print("=" * 50)
    
    return all(results)

if __name__ == '__main__':
    success = main()
    exit(0 if success else 1)
```

**Запустити перевірку:**
```bash
python create_test.py
```

---

## 📋 Чекліст виконання

Відмітьте після виконання кожного кроку:

### Критичні виправлення
- [ ] SSL верифікація в discord_monitor.py
- [ ] SSL верифікація в twitter_monitor.py
- [ ] Видалено disable_warnings в selenium_twitter_monitor.py
- [ ] Встановлено certifi
- [ ] Замінено SHA-256 на bcrypt в access_manager.py
- [ ] Виконано міграцію паролів
- [ ] Bearer токен перенесено в config.py
- [ ] Bearer токен додано в .env
- [ ] Видалено дефолтний пароль з config.py
- [ ] ADMIN_PASSWORD додано в .env
- [ ] Оновлено requirements.txt
- [ ] Встановлено нові залежності

### Перевірка
- [ ] Запущено create_test.py
- [ ] Всі перевірки пройдені
- [ ] Створено backup перед змінами
- [ ] Протестовано бота після змін

### Документація
- [ ] Оновлено .env.example
- [ ] Додано коментарі в код
- [ ] Записано зміни в CHANGELOG.md

---

## 🚀 Запуск після виправлень

### 1. Перевірити конфігурацію

```bash
# Переконатися що .env файл налаштовано
cat .env

# Має містити:
# BOT_TOKEN=...
# ADMIN_PASSWORD=...
# AUTHORIZATION=...
# TWITTER_AUTH_TOKEN=...
# TWITTER_CSRF_TOKEN=...
# TWITTER_BEARER_TOKEN=...
```

### 2. Перевірити залежності

```bash
pip install -r requirements.txt
python -c "import bcrypt; import certifi; print('✅ Всі залежності встановлені')"
```

### 3. Тестовий запуск

```bash
# Запустити бота
python bot.py

# Перевірити логи
tail -f bot.log

# Або перевірити в окремому вікні
python -c "import bot; print('✅ Бот імпортується без помилок')"
```

### 4. Перевірити функціональність

1. Запустити бота
2. Відправити /start
3. Авторизуватися
4. Перевірити основні команди
5. Додати тестовий проект
6. Перевірити моніторинг

---

## ⚠️ Поширені проблеми

### Проблема 1: SSL Certificate Error

**Помилка:**
```
SSLError: [SSL: CERTIFICATE_VERIFY_FAILED]
```

**Рішення:**
```bash
# Встановити certifi
pip install --upgrade certifi

# Або на macOS
/Applications/Python\ 3.x/Install\ Certificates.command
```

### Проблема 2: Bcrypt не встановлюється

**Помилка:**
```
error: Microsoft Visual C++ 14.0 is required
```

**Рішення (Windows):**
```bash
# Встановити Visual C++ Build Tools
# Або використати pre-compiled wheel
pip install bcrypt --prefer-binary
```

### Проблема 3: Міграція паролів не працює

**Рішення:**
1. Зробити backup access_data.json
2. Попросити всіх користувачів встановити нові паролі
3. Використати варіант 2 міграції (скидання паролів)

### Проблема 4: Токени не працюють

**Перевірити:**
```python
# Додати debug в config.py
print(f"BOT_TOKEN: {'✅ встановлено' if BOT_TOKEN else '❌ відсутній'}")
print(f"TWITTER_BEARER_TOKEN: {'✅ встановлено' if TWITTER_BEARER_TOKEN else '❌ відсутній'}")
```

---

## 📞 Підтримка

Якщо виникли проблеми:

1. Перевірте логи бота
2. Запустіть create_test.py
3. Перегляньте SECURITY_ISSUES.md
4. Створіть issue на GitHub

---

## 🎓 Додатково

### Створити .env.example

```bash
# Створити шаблон для інших розробників
cat > .env.example << 'EOF'
# Telegram Bot
BOT_TOKEN=your_bot_token_here

# Admin
ADMIN_PASSWORD=your_secure_password

# Discord
AUTHORIZATION=your_discord_token

# Twitter
TWITTER_AUTH_TOKEN=your_twitter_auth_token
TWITTER_CSRF_TOKEN=your_twitter_csrf_token
TWITTER_BEARER_TOKEN=your_twitter_bearer_token

# Intervals
MONITORING_INTERVAL=15
TWITTER_MONITORING_INTERVAL=30
SECURITY_TIMEOUT=300
EOF
```

### Оновити .gitignore

```bash
# Переконатися що .env не комітиться
echo ".env" >> .gitignore
echo "access_data_backup_*.json" >> .gitignore
git add .gitignore
git commit -m "chore: update .gitignore"
```

---

**Час виконання:** 2-3 години  
**Складність:** Середня  
**Пріоритет:** CRITICAL  

✅ **Після виконання цих кроків безпека вашого бота буде значно покращена!**
