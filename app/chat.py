"""Gemini-backed research assistant — scoped to the real data already
computed elsewhere in the app, not a general-purpose chatbot.

Security note: GEMINI_API_KEY must come from an environment variable.
This repo is public on GitHub — a key hardcoded here would be visible to
anyone who opens the file. Set it via `export GEMINI_API_KEY=...` locally
or Render → your service → Environment. Get a key at
https://aistudio.google.com/apikey.
"""
from __future__ import annotations

import json
import os

import requests

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
_ENDPOINT = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

SYSTEM_INSTRUCTION = (
    "You are the research assistant embedded in SwingLab, a personal investing "
    "research tool for Indian (NSE) equities. Every message includes a JSON "
    "'context' block of real data the app already computed for whatever page "
    "the person is looking at (fundamentals, valuation, technical/strategy "
    "signals). Rules, no exceptions:\n"
    "1. Only cite numbers that appear in the provided context. Never invent, "
    "estimate, or recall a financial figure from your own knowledge — this "
    "app's whole design principle is 'never guess a number.'\n"
    "2. If the answer needs data that isn't in the context, say plainly that "
    "it isn't available here, rather than guessing.\n"
    "3. Never give a direct buy/sell/hold recommendation or personalized "
    "investment advice. Explain what the data shows and let the person draw "
    "their own conclusion — matching this app's own disclaimers.\n"
    "4. Be concise: a few sentences, not an essay, unless asked for more detail."
)


class ChatNotConfigured(RuntimeError):
    pass


def ask(message: str, context: dict | None, history: list[dict] | None = None) -> str:
    if not GEMINI_API_KEY:
        raise ChatNotConfigured("GEMINI_API_KEY is not set on the server.")

    context_text = json.dumps(context, indent=2, default=str) if context else "(no page context available — general question about SwingLab itself)"

    contents = []
    for turn in (history or [])[-8:]:
        role = "user" if turn.get("role") == "user" else "model"
        text = turn.get("text", "")
        if text:
            contents.append({"role": role, "parts": [{"text": text}]})
    contents.append({
        "role": "user",
        "parts": [{"text": f"Context (real data already on the page):\n{context_text}\n\nQuestion: {message}"}],
    })

    payload = {
        "contents": contents,
        "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 500},
    }
    try:
        resp = requests.post(_ENDPOINT, params={"key": GEMINI_API_KEY}, json=payload, timeout=30)
    except requests.RequestException as e:
        raise RuntimeError(f"Could not reach Gemini: {e}")

    if resp.status_code != 200:
        raise RuntimeError(f"Gemini API error {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"Unexpected Gemini response shape: {json.dumps(data)[:300]}")
