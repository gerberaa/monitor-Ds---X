# 🔧 Керівництво з рефакторингу

## 📋 Огляд

Цей документ описує план рефакторингу кодової бази Monitor DS + X Bot для покращення підтримуваності, масштабованості та якості коду.

---

## 🎯 Цілі рефакторингу

1. ✅ Розбити великий bot.py (7,473 рядки) на менші модулі
2. ✅ Усунути глобальні змінні
3. ✅ Покращити структуру коду
4. ✅ Додати type hints
5. ✅ Покращити обробку помилок
6. ✅ Зменшити дублювання коду

---

## 📁 Нова структура проекту

### Поточна структура
```
monitor-Ds---X/
├── bot.py                          # 7,473 рядки ❌
├── project_manager.py
├── access_manager.py
├── security_manager.py
├── config.py
└── ...
```

### Цільова структура
```
monitor-Ds---X/
├── bot/
│   ├── __init__.py
│   ├── main.py                     # Точка входу (100 рядків)
│   ├── app.py                      # Application setup (150 рядків)
│   │
│   ├── handlers/                   # Обробники команд
│   │   ├── __init__.py
│   │   ├── auth_handler.py         # Авторизація
│   │   ├── project_handler.py      # Проекти
│   │   ├── twitter_handler.py      # Twitter
│   │   ├── discord_handler.py      # Discord
│   │   ├── selenium_handler.py     # Selenium
│   │   ├── admin_handler.py        # Адміністрування
│   │   └── menu_handler.py         # Меню
│   │
│   ├── keyboards/                  # Inline клавіатури
│   │   ├── __init__.py
│   │   ├── main_keyboard.py
│   │   ├── project_keyboard.py
│   │   ├── twitter_keyboard.py
│   │   └── admin_keyboard.py
│   │
│   ├── services/                   # Бізнес-логіка
│   │   ├── __init__.py
│   │   ├── monitoring_service.py   # Синхронізація моніторів
│   │   ├── forwarding_service.py   # Пересилання повідомлень
│   │   └── project_service.py      # Робота з проектами
│   │
│   ├── middleware/                 # Middleware
│   │   ├── __init__.py
│   │   ├── auth_middleware.py
│   │   └── rate_limiter.py
│   │
│   └── utils/                      # Утиліти
│       ├── __init__.py
│       ├── formatters.py           # Форматування повідомлень
│       ├── validators.py           # Валідація
│       └── helpers.py              # Допоміжні функції
│
├── monitors/                       # Монітори
│   ├── __init__.py
│   ├── base_monitor.py             # Базовий клас
│   ├── twitter_monitor.py
│   ├── discord_monitor.py
│   ├── selenium_monitor.py
│   └── twitter_adapter.py
│
├── managers/                       # Менеджери
│   ├── __init__.py
│   ├── project_manager.py
│   ├── access_manager.py
│   └── security_manager.py
│
├── models/                         # Моделі даних
│   ├── __init__.py
│   ├── project.py
│   ├── user.py
│   └── monitoring_state.py
│
├── database/                       # БД (майбутнє)
│   ├── __init__.py
│   ├── connection.py
│   └── migrations/
│
├── tests/                          # Тести
│   ├── unit/
│   ├── integration/
│   └── conftest.py
│
├── config.py
├── requirements.txt
└── main.py                         # Точка входу
```

---

## 🔨 План рефакторингу

### Етап 1: Підготовка (1 день)

#### 1.1. Створити структуру папок
```bash
mkdir -p bot/{handlers,keyboards,services,middleware,utils}
mkdir -p monitors managers models database tests/{unit,integration}
```

#### 1.2. Додати __init__.py файли
```bash
find . -type d -exec touch {}/__init__.py \;
```

#### 1.3. Створити git гілку
```bash
git checkout -b refactor/modular-structure
```

---

### Етап 2: Виділення моделей (2 дні)

#### 2.1. Створити моделі даних (models/project.py)

**Було:**
```python
# bot.py
project_data = {
    'id': project_id,
    'name': name,
    'type': project_type,
    'url': url,
    'forward_to': forward_to,
    'created_at': datetime.now().isoformat()
}
```

**Стало:**
```python
# models/project.py
from pydantic import BaseModel, validator
from datetime import datetime
from typing import Optional, Literal

class Project(BaseModel):
    id: int
    name: str
    type: Literal['twitter', 'discord']
    url: str
    forward_to: int
    created_at: datetime = datetime.now()
    updated_at: Optional[datetime] = None
    admins: list[int] = []
    
    @validator('url')
    def validate_url(cls, v, values):
        project_type = values.get('type')
        if project_type == 'twitter':
            if not v.startswith(('https://twitter.com/', 'https://x.com/')):
                raise ValueError('Invalid Twitter URL')
        elif project_type == 'discord':
            if not v.startswith('https://discord.com/channels/'):
                raise ValueError('Invalid Discord URL')
        return v
    
    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }
```

#### 2.2. Створити моделі користувачів (models/user.py)

```python
# models/user.py
from pydantic import BaseModel
from datetime import datetime
from typing import Optional, Literal

class User(BaseModel):
    telegram_id: int
    username: str
    access_level: Literal['user', 'admin', 'super_admin'] = 'user'
    permissions: list[str] = []
    created_at: datetime
    last_login: Optional[datetime] = None
    is_active: bool = True
```

---

### Етап 3: Виділення сервісів (3 дні)

#### 3.1. Створити monitoring_service.py

**Було:** Функції в bot.py
```python
# bot.py (рядки 82-200)
def clean_forbidden_accounts():
    ...

async def sync_monitors_with_projects():
    ...

async def start_monitoring():
    ...
```

**Стало:**
```python
# bot/services/monitoring_service.py
from typing import Set, Dict
from managers.project_manager import ProjectManager
from monitors.base_monitor import BaseMonitor

class MonitoringService:
    def __init__(
        self,
        project_manager: ProjectManager,
        monitors: Dict[str, BaseMonitor]
    ):
        self.project_manager = project_manager
        self.monitors = monitors
        self.forbidden_accounts = {'twitter', 'x'}
    
    async def clean_forbidden_accounts(self) -> None:
        """Очистити заборонені акаунти"""
        for monitor in self.monitors.values():
            for account in self.forbidden_accounts:
                if account in monitor.monitoring_accounts:
                    monitor.remove_account(account)
    
    async def sync_monitors_with_projects(self) -> None:
        """Синхронізувати монітори з проектами"""
        all_projects = self.project_manager.get_all_projects()
        
        # Групуємо проекти за типом
        twitter_accounts = set()
        discord_channels = set()
        
        for user_projects in all_projects.values():
            for project in user_projects:
                if project['type'] == 'twitter':
                    username = project['url'].split('/')[-1]
                    twitter_accounts.add(username)
                elif project['type'] == 'discord':
                    discord_channels.add(project['url'])
        
        # Синхронізуємо монітори
        await self._sync_twitter_monitor(twitter_accounts)
        await self._sync_discord_monitor(discord_channels)
    
    async def _sync_twitter_monitor(self, accounts: Set[str]) -> None:
        """Синхронізувати Twitter монітор"""
        if 'twitter' not in self.monitors:
            return
        
        monitor = self.monitors['twitter']
        current_accounts = monitor.monitoring_accounts.copy()
        
        # Додаємо нові акаунти
        for account in accounts - current_accounts:
            await monitor.add_account(account)
        
        # Видаляємо старі акаунти
        for account in current_accounts - accounts:
            monitor.remove_account(account)
```

#### 3.2. Створити forwarding_service.py

```python
# bot/services/forwarding_service.py
import tempfile
import os
from typing import Optional, Dict
from telegram import Bot
from PIL import Image

class ForwardingService:
    def __init__(self, bot: Bot):
        self.bot = bot
    
    async def forward_tweet(
        self,
        channel_id: int,
        tweet_data: Dict,
        project_name: str
    ) -> None:
        """Переслати твіт у канал"""
        text = self._format_tweet_message(tweet_data, project_name)
        
        # Відправляємо медіа якщо є
        if tweet_data.get('media'):
            await self._send_with_media(channel_id, text, tweet_data['media'])
        else:
            await self.bot.send_message(channel_id, text)
    
    async def forward_discord_message(
        self,
        channel_id: int,
        message_data: Dict,
        project_name: str
    ) -> None:
        """Переслати Discord повідомлення у канал"""
        text = self._format_discord_message(message_data, project_name)
        
        if message_data.get('attachments'):
            await self._send_with_attachments(
                channel_id,
                text,
                message_data['attachments']
            )
        else:
            await self.bot.send_message(channel_id, text)
    
    def _format_tweet_message(self, tweet_data: Dict, project_name: str) -> str:
        """Форматувати повідомлення твіта"""
        return (
            f"🐦 **{project_name}**\n\n"
            f"{tweet_data['text']}\n\n"
            f"🔗 {tweet_data['url']}"
        )
```

---

### Етап 4: Виділення обробників (4 дні)

#### 4.1. Створити базовий обробник (bot/handlers/base_handler.py)

```python
# bot/handlers/base_handler.py
from abc import ABC, abstractmethod
from telegram import Update
from telegram.ext import ContextTypes
from typing import Optional

class BaseHandler(ABC):
    """Базовий клас для всіх обробників"""
    
    def __init__(self, **dependencies):
        """Ініціалізація з залежностями"""
        for key, value in dependencies.items():
            setattr(self, key, value)
    
    @abstractmethod
    async def handle(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Головний метод обробки"""
        pass
    
    def _get_user_id(self, update: Update) -> Optional[int]:
        """Отримати ID користувача"""
        return update.effective_user.id if update.effective_user else None
```

#### 4.2. Створити project_handler.py

**Було:** Функції в bot.py
```python
# bot.py (рядки 500-1000)
async def show_projects_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ...

async def add_project_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ...

async def delete_project(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ...
```

**Стало:**
```python
# bot/handlers/project_handler.py
from .base_handler import BaseHandler
from managers.project_manager import ProjectManager
from bot.keyboards.project_keyboard import ProjectKeyboard

class ProjectHandler(BaseHandler):
    def __init__(
        self,
        project_manager: ProjectManager,
        keyboard: ProjectKeyboard
    ):
        self.project_manager = project_manager
        self.keyboard = keyboard
    
    async def show_projects_menu(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Показати меню проектів"""
        user_id = self._get_user_id(update)
        projects = self.project_manager.get_user_projects(user_id)
        
        keyboard = self.keyboard.create_projects_list(projects)
        
        await update.message.reply_text(
            "📁 **Ваші проекти:**",
            reply_markup=keyboard
        )
    
    async def add_project_start(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Початок додавання проекту"""
        keyboard = self.keyboard.create_project_type_selector()
        
        await update.message.reply_text(
            "📝 **Виберіть тип проекту:**",
            reply_markup=keyboard
        )
    
    async def delete_project(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        project_id: int
    ) -> None:
        """Видалити проект"""
        user_id = self._get_user_id(update)
        
        success = self.project_manager.delete_project(user_id, project_id)
        
        if success:
            await update.callback_query.answer("✅ Проект видалено")
        else:
            await update.callback_query.answer("❌ Помилка видалення")
```

---

### Етап 5: Виділення клавіатур (2 дні)

#### 5.1. Створити project_keyboard.py

**Було:**
```python
# bot.py
keyboard = InlineKeyboardMarkup([
    [InlineKeyboardButton("📁 Мої проекти", callback_data='projects')],
    [InlineKeyboardButton("➕ Додати проект", callback_data='add_project')],
    ...
])
```

**Стало:**
```python
# bot/keyboards/project_keyboard.py
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from typing import List, Dict

class ProjectKeyboard:
    @staticmethod
    def create_projects_list(projects: List[Dict]) -> InlineKeyboardMarkup:
        """Створити клавіатуру зі списком проектів"""
        buttons = []
        
        for project in projects:
            emoji = "🐦" if project['type'] == 'twitter' else "💬"
            buttons.append([
                InlineKeyboardButton(
                    f"{emoji} {project['name']}",
                    callback_data=f"project_{project['id']}"
                )
            ])
        
        buttons.append([
            InlineKeyboardButton("➕ Додати проект", callback_data='add_project')
        ])
        buttons.append([
            InlineKeyboardButton("🔙 Назад", callback_data='main_menu')
        ])
        
        return InlineKeyboardMarkup(buttons)
    
    @staticmethod
    def create_project_type_selector() -> InlineKeyboardMarkup:
        """Клавіатура вибору типу проекту"""
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🐦 Twitter", callback_data='project_type_twitter')],
            [InlineKeyboardButton("💬 Discord", callback_data='project_type_discord')],
            [InlineKeyboardButton("🔙 Назад", callback_data='projects')]
        ])
```

---

### Етап 6: Створення Application (2 дні)

#### 6.1. Створити bot/app.py

```python
# bot/app.py
from telegram.ext import Application, CommandHandler, CallbackQueryHandler
from managers.project_manager import ProjectManager
from managers.access_manager import AccessManager
from bot.handlers.project_handler import ProjectHandler
from bot.handlers.twitter_handler import TwitterHandler
from bot.services.monitoring_service import MonitoringService
from config import BOT_TOKEN

class BotApplication:
    def __init__(self):
        # Ініціалізація менеджерів
        self.project_manager = ProjectManager()
        self.access_manager = AccessManager()
        
        # Ініціалізація сервісів
        self.monitoring_service = MonitoringService(
            project_manager=self.project_manager,
            monitors={}  # будуть додані пізніше
        )
        
        # Ініціалізація обробників
        self.project_handler = ProjectHandler(
            project_manager=self.project_manager
        )
        self.twitter_handler = TwitterHandler(...)
        
        # Telegram Application
        self.app = Application.builder().token(BOT_TOKEN).build()
        
        self._register_handlers()
    
    def _register_handlers(self):
        """Реєстрація обробників команд"""
        # Команди
        self.app.add_handler(CommandHandler("start", self._start))
        self.app.add_handler(CommandHandler("projects", self.project_handler.show_projects_menu))
        
        # Callback queries
        self.app.add_handler(CallbackQueryHandler(
            self._handle_callback,
            pattern='^.*$'
        ))
    
    async def _handle_callback(self, update, context):
        """Головний обробник callback'ів"""
        query = update.callback_query
        data = query.data
        
        # Роутинг callback'ів
        if data.startswith('project_'):
            await self.project_handler.handle_project_callback(update, context)
        elif data.startswith('twitter_'):
            await self.twitter_handler.handle_twitter_callback(update, context)
        # ...
    
    def run(self):
        """Запустити бота"""
        self.app.run_polling()
```

#### 6.2. Створити main.py

```python
# main.py
import logging
from bot.app import BotApplication

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

def main():
    """Точка входу"""
    app = BotApplication()
    app.run()

if __name__ == '__main__':
    main()
```

---

### Етап 7: Міграція даних (1 день)

#### 7.1. Створити міграції

```python
# database/migrations/001_initial.py
import json
import sqlite3

def migrate_from_json_to_sqlite():
    """Мігрувати дані з JSON в SQLite"""
    # Читаємо старі дані
    with open('data.json', 'r') as f:
        old_data = json.load(f)
    
    # Створюємо БД
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    
    # Створюємо таблиці
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            url TEXT NOT NULL,
            forward_to INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Мігруємо проекти
    for user_id, projects in old_data['projects'].items():
        for project in projects:
            cursor.execute('''
                INSERT INTO projects (id, user_id, name, type, url, forward_to)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                project['id'],
                int(user_id),
                project['name'],
                project['type'],
                project['url'],
                project['forward_to']
            ))
    
    conn.commit()
    conn.close()
```

---

## 📊 Прогрес рефакторингу

### Чекліст

#### Етап 1: Підготовка
- [ ] Створити структуру папок
- [ ] Додати __init__.py файли
- [ ] Створити git гілку

#### Етап 2: Моделі
- [ ] Створити models/project.py
- [ ] Створити models/user.py
- [ ] Додати валідацію

#### Етап 3: Сервіси
- [ ] Створити monitoring_service.py
- [ ] Створити forwarding_service.py
- [ ] Створити project_service.py

#### Етап 4: Обробники
- [ ] Створити base_handler.py
- [ ] Створити project_handler.py
- [ ] Створити twitter_handler.py
- [ ] Створити discord_handler.py
- [ ] Створити auth_handler.py

#### Етап 5: Клавіатури
- [ ] Створити project_keyboard.py
- [ ] Створити main_keyboard.py
- [ ] Створити twitter_keyboard.py

#### Етап 6: Application
- [ ] Створити bot/app.py
- [ ] Створити main.py
- [ ] Налаштувати DI (Dependency Injection)

#### Етап 7: Міграція
- [ ] Створити міграції
- [ ] Тестувати міграцію
- [ ] Backup старих даних

#### Етап 8: Тестування
- [ ] Unit тести для сервісів
- [ ] Integration тести
- [ ] E2E тести

---

## 🎯 Очікувані результати

### Метрики

| Метрика | Було | Стане | Покращення |
|---------|------|-------|------------|
| Розмір bot.py | 7,473 рядки | ~200 рядків | -97% |
| Кількість файлів | 15 | 50+ | +233% |
| Середній розмір файлу | ~830 рядків | ~150 рядків | -82% |
| Глобальні змінні | 10+ | 0 | -100% |
| Code coverage | 0% | 80%+ | +80% |

### Переваги після рефакторингу

1. ✅ **Підтримуваність** - Легше знайти і змінити код
2. ✅ **Тестованість** - Можна тестувати окремі компоненти
3. ✅ **Масштабованість** - Легко додавати нові функції
4. ✅ **Читабельність** - Зрозуміла структура
5. ✅ **Переусність** - Компоненти можна використовувати повторно

---

## ⚠️ Ризики і рекомендації

### Ризики
1. 🔴 Можливі баги при міграції
2. 🟡 Час на рефакторинг (~2 тижні)
3. 🟡 Потрібно навчитися новій структурі

### Рекомендації
1. ✅ Зробити backup перед рефакторингом
2. ✅ Рефакторити по одному модулю
3. ✅ Писати тести для кожного модуля
4. ✅ Code review після кожного етапу
5. ✅ Documenta нову структуру

---

**Дата створення:** Грудень 2024  
**Версія:** 1.0  
**Автор:** Refactoring Guide
