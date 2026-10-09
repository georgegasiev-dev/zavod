
"""Local AI usage counters, persistent instructions and Telegram voice input."""
import asyncio
import html
import logging
import os
from pathlib import Path
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent


def _db():
    db = sqlite3.connect(ROOT / 'ai_usage.sqlite3', timeout=15)
    db.execute('CREATE TABLE IF NOT EXISTS usage (at TEXT NOT NULL, model TEXT NOT NULL, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, audio_seconds REAL NOT NULL)')
    return db


def record_usage(model, usage=None, audio_seconds=0):
    """Store only counts, never questions, audio, keys or financial data."""
    usage = usage or {}
    try:
        now = datetime.now(ZoneInfo('Europe/Moscow')).isoformat()
        with _db() as db:
            db.execute('INSERT INTO usage VALUES (?,?,?,?,?)', (now, model,
                int(usage.get('prompt_tokens') or 0), int(usage.get('completion_tokens') or 0), float(audio_seconds)))
    except Exception as error:
        log.error('Cannot save AI usage: %s', type(error).__name__)


def usage_report():
    now = datetime.now(ZoneInfo('Europe/Moscow'))
    lines = ['📊 <b>Расход AI-бота</b> (МСК)']
    with _db() as db:
        for label, prefix in [('Сегодня', now.strftime('%Y-%m-%d')), ('Этот месяц', now.strftime('%Y-%m'))]:
            rows = db.execute('SELECT model, count(*), sum(input_tokens), sum(output_tokens), sum(audio_seconds) FROM usage WHERE at LIKE ? GROUP BY model ORDER BY model', (prefix+'%',)).fetchall()
            lines.append('\n<b>'+label+'</b>')
            if not rows:
                lines.append('Запросов пока нет.')
            for model, calls, incoming, outgoing, audio in rows:
                lines.append(html.escape(model))
                if audio or model.startswith('whisper-'):
                    lines.append(f'Распознаваний: {calls}; аудио: {audio / 60:.1f} мин.')
                else:
                    lines.append(f'API-запросов: {calls}; вход: {incoming:,}; выход: {outgoing:,}; всего: {incoming + outgoing:,} токенов.')
    lines.append('\nУчёт с момента установки; только успешные ответы API этого бота. Один вопрос может вызвать несколько запросов. Полный расход аккаунта и лимиты — в Groq Console. Аудио учитывается по длительности сообщения Telegram.')
    return '\n'.join(lines)


def read_rules():
    path = ROOT / 'ai_rules.txt'
    try:
        value = path.read_text(encoding='utf-8').strip()
    except FileNotFoundError:
        return ''
    if len(value) > 12000:
        raise ValueError('ai_rules.txt exceeds 12000 characters')
    return ('\nПравила владельца для интерпретации данных и ответа:\n' + value) if value else ''


async def handle_voice(msg, tg_token, reply):
    import httpx
    voice = msg['voice']
    if voice.get('duration', 0) > 600 or voice.get('file_size', 0) > 20_000_000:
        await reply('⚠️ Отправь голосовое до 10 минут и до 20 МБ.')
        return
    key = os.getenv('GROQ_API_KEY', '').strip()
    if not key:
        await reply('⚠️ GROQ_API_KEY не настроен.')
        return
    await reply('🎙 Распознаю голосовое…')
    model = os.getenv('GROQ_WHISPER_MODEL', 'whisper-large-v3-turbo')
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(f'https://api.telegram.org/bot{tg_token}/getFile', params={'file_id': voice['file_id']})
            response.raise_for_status()
            body = response.json()
            if not body.get('ok'):
                raise ValueError('Telegram getFile failed')
            info = body['result']
            if info.get('file_size', 0) > 20_000_000:
                await reply('⚠️ Голосовое превышает 20 МБ.')
                return
            path = info['file_path']
            if not isinstance(path, str) or '..' in path or path.startswith('/') or not path.startswith('voice/'):
                raise ValueError('Invalid Telegram voice path')
            audio = bytearray()
            async with client.stream('GET', f'https://api.telegram.org/file/bot{tg_token}/{path}') as download:
                download.raise_for_status()
                async for chunk in download.aiter_bytes():
                    audio.extend(chunk)
                    if len(audio) > 20_000_000:
                        await reply('⚠️ Голосовое превышает 20 МБ.')
                        return
            response = await client.post('https://api.groq.com/openai/v1/audio/transcriptions',
                headers={'Authorization': 'Bearer '+key, 'User-Agent': 'NovatorBot/1.0'},
                files={'file': ('voice.ogg', bytes(audio), 'audio/ogg')},
                data={'model': model, 'language': 'ru', 'response_format': 'json', 'temperature': '0',
                      'prompt': 'Новатор, Семёнов, фанера, шпон, смола, Raiffeisen.'})
            response.raise_for_status()
            record_usage(model, audio_seconds=voice.get('duration', 0))
            text = response.json().get('text', '').strip()
        if not text:
            await reply('⚠️ Речь не распознана. Отправь вопрос текстом.')
            return
        if len(text) > 10000:
            await reply('⚠️ Слишком длинная расшифровка. Раздели голосовое на несколько вопросов.')
            return
        for start in range(0, len(text), 1500):
            await reply('🎙 ' + html.escape(text[start:start+1500], quote=False))
        from ai_agent import ask_agent
        await reply('🤔 Думаю…')
        result = await asyncio.get_running_loop().run_in_executor(None, ask_agent, text)
        await reply(result)
    except httpx.HTTPStatusError as error:
        code = error.response.status_code
        log.warning('Voice API returned HTTP %s', code)
        await reply('⚠️ Достигнут лимит API. Попробуй позже.' if code == 429 else f'⚠️ Не удалось обработать голосовое (HTTP {code}).')
    except Exception as error:
        log.warning('Voice processing failed: %s', type(error).__name__)
        await reply('⚠️ Не удалось обработать голосовое. Попробуй ещё раз или напиши текстом.')

