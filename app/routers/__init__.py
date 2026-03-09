# FILE: app/routers/__init__.py
# ✅ Keep this file OpenAI-free. Do NOT import openai-based routers here.

from . import chat_ai
from . import lang_pool
from . import teacher_chat
from . import translate
from . import translate_ai
from . import command_parse
from . import admin
from . import f2f_ws
from . import tts
from . import stt
from . import ocr_translate

# OPTIONALS are imported inside main.py with try/except as you already do:
# exam_pro, level_test, voice_openai, ocr, offline
