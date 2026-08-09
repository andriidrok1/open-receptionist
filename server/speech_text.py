"""speech_text.py — підготовка тексту LLM до озвучення (українська).

ElevenLabs не розуміє змісту — він озвучує те, як написано. Тому цей шар
чистить розмітку пауз перед синтезом.

**break-теги** дають точну паузу там, де її не задати пунктуацією (між
блоками цифр телефону, перед ціною). Але ElevenLabs прямо попереджає: від
надлишку тегів мовлення розганяється й з'являються артефакти — тому тут
санітайзер: не більше `MAX_BREAKS` на репліку, кожна не довша за
`MAX_BREAK_SEC`. Модель у промпті теж просять ставити максимум два, але
покладатись на це не можна.

---
ЩО ТУТ БУЛО І ЧОМУ ЗНИКЛО (перевірено на слух 09.08.2026).

Тут стояла розстановка наголосів: словник `ukrainian-word-stress` (2.9 млн
словоформ) ставив комбінований акут U+0301 у кожне слово, плюс доменний шар
для «Рів'є́ра», «Делю́кс», «бро́ні». Логіка була така: модель вгадує наголос за
написанням і промахується на числівниках і датах — саме там, де в нас ціни й
телефони.

Гіпотеза НЕ підтвердилась. У сліпому порівнянні (voice_lab final/domain)
сирий текст звучить краще і за суцільну розмітку, і за точкову.

Причина: проблема з наголосами була властивістю АНГЛІЙСЬКОГО голосу (Sarah),
який читав українську навмання. Нативний український голос (Kateryna Pavlenko,
fine-tuned під flash v2.5) ставить наголоси сам, а незвичні символи в тексті
його лише збивають.

Висновок на майбутнє: **наголоси — це лікування не тієї хвороби. Спершу
нативний голос.** Якщо колись зміниться голос чи модель — гіпотезу варто
перевірити наново: `pip install --no-deps ukrainian-word-stress marisa-trie`,
далі `Stressifier(stress_symbol=StressSymbol.CombiningAcuteAccent)`.
"""

import re

MAX_BREAKS = 2  # більше — і мовлення починає розганятись
MAX_BREAK_SEC = 0.6

_BREAK_RE = re.compile(r'<break\s+time="(\d*\.?\d+)s"\s*/>', re.I)


def sanitize_breaks(text: str) -> str:
    """Лишити максимум MAX_BREAKS тегів, кожен не довший за MAX_BREAK_SEC."""
    kept = 0

    def repl(m: re.Match) -> str:
        nonlocal kept
        if kept >= MAX_BREAKS:
            return " "
        kept += 1
        secs = min(float(m.group(1)), MAX_BREAK_SEC)
        return f'<break time="{secs}s" />'

    return _BREAK_RE.sub(repl, text)


class UkrainianSpeechFilter:
    """Фільтр тексту перед TTS. Реалізує інтерфейс pipecat BaseTextFilter.

    Наслідування зроблено «м'яким» (без імпорту pipecat), щоб модуль лишався
    імпортовним і тестованим окремо від пайплайна.
    """

    def prepare(self, text: str) -> str:
        """Уся обробка одним викликом — зручно тестувати без пайплайна."""
        if not text or not text.strip():
            return text
        return sanitize_breaks(text)

    # --- інтерфейс pipecat BaseTextFilter ---

    async def filter(self, text: str) -> str:
        return self.prepare(text)

    async def update_settings(self, settings):
        pass

    async def handle_interruption(self):
        pass

    async def reset_interruption(self):
        pass


if __name__ == "__main__":
    f = UkrainianSpeechFilter()
    samples = [
        "Так, дивлюся — Покращений на двох є, з чотирнадцятого по сімнадцяте.",
        'Записую.<break time="0.4s" /> Три ночі.<break time="2s" /> Телефон:'
        '<break time="0.3s" /> нуль шістдесят сім<break time="0.3s" /> двісті тридцять чотири.',
    ]
    for s in samples:
        print("\nIN :", s)
        print("OUT:", f.prepare(s))
