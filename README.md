# hotel-voice-agent

Голосовой ИИ-ресепшн для отелей (украинский). Клон ядра Lance (YC W26), Фаза 1.

Гость говорит с агентом голосом по-украински → агент отвечает голосом, отвечает на
вопросы про отель и принимает предварительную бронь с устным подтверждением.

## Архитектура (cascade pipeline)

```
микрофон → Deepgram STT (nova-3, uk) → Claude (Haiku) → ElevenLabs TTS (flash v2.5, uk) → динамик
```

- **Детекция речи** (когда гость начал/замолчал, перебивание) — Silero VAD, вшит в pipeline.
- Транспорт: SmallWebRTC (микрофон/динамик через браузер, без внешних аккаунтов для дева).
- Фреймворк: Pipecat 1.5.

Что где: `server/bot.py` — весь агент. Системный промпт (персона «Оксана») и справочная
заглушка про отель — там же, вверху файла, правь под себя.

## Запуск (Фаза 1)

Виртуальное окружение уже создано в `.venv/` (Python 3.12, зависимости установлены).

1. Ключи API — нужны три (у всех есть бесплатный кредит):
   - Deepgram: https://console.deepgram.com/
   - ElevenLabs: https://elevenlabs.io/ → Profile → API Keys (+ выбрать `ELEVENLABS_VOICE_ID`
     из библиотеки голосов, лучше мультиязычный/украинский)
   - Anthropic: https://console.anthropic.com/

   ```bash
   cd server
   cp .env.example .env
   # впиши ключи в .env
   ```

2. Запуск:

   ```bash
   cd ~/PycharmProjects/hotel-voice-agent
   source .venv/bin/activate
   cd server
   python bot.py
   ```

3. Открой http://localhost:7860/client → **Connect** → разреши микрофон → говори по-украински.

## Что дальше (из дизайн-дока)

- **Фаза 2** — реальная логика брони: function-calling tools (`check_availability`,
  `create_booking`) + фейковый PMS (SQLite). Сейчас агент собирает бронь устно, но НЕ пишет.
- **Фаза 3** — дашборд (FastAPI+SQLite): лог звонков/транскриптов/броней = артефакт «показать».
- **Фаза 4** — телефония (Twilio/Telnyx/Binotel): реальный номер → реальный звонок →
  Loom-запись = демо для продаж.

Дизайн-док: `~/.gstack/projects/andriidrok1-obsidian-vault/andrii-main-design-20260705-115846.md`

## Заметки

- LLM = Claude Haiku (быстрый, для низкой латентности; бюджет разговора ~300мс round-trip).
  Сменить модель — `ANTHROPIC_MODEL` в `.env`.
- Deepgram `numerals=True` включён — правильно распознаёт числа/даты (критично для броней).
- `uv` не используется (на машине его нет) — обычный venv + pip.
