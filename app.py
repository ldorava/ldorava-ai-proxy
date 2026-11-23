import os
import time
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(
    app,
    resources={
        r"/chat": {
            "origins": ["https://ldorava.github.io", "http://localhost:5000"]
        }
    },
)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
STORE_JSON_URL = os.environ.get(
    "STORE_JSON_URL",
    "https://raw.githubusercontent.com/ldorava/Ldorava/main/data/store.json",
).strip()

CACHE_TTL_SECONDS = 300
_cache = {"entries": [], "fetched_at": 0}


def extract_entries(obj, prefix=""):
  results = []
  if isinstance(obj, list):
    for idx, item in enumerate(obj, start=1):
      results.extend(extract_entries(item, f"{prefix} #{idx}".strip()))
  elif isinstance(obj, dict):
    title = (
        str(
            obj.get("title")
            or obj.get("name")
            or obj.get("product_name")
            or obj.get("label")
            or prefix
            or "عنصر"
        ).strip()
    )

    parts = []
    for k, v in obj.items():
      if k in ("title", "name", "product_name", "label"):
        continue
      if isinstance(v, (str, int, float, bool)):
        parts.append(f"{k}: {v}")
      elif isinstance(v, list):
        sub_texts = []
        for sub in v:
          if isinstance(sub, dict):
            sub_texts.append(
                ", ".join(f"{sk}: {sv}" for sk, sv in sub.items())
            )
          else:
            sub_texts.append(str(sub))
        if sub_texts:
          parts.append(f"{k}: {' | '.join(sub_texts)}")
      elif isinstance(v, dict):
        results.extend(extract_entries(v, f"{title} - {k}"))

    if parts:
      results.append({"title": title, "content": "\n".join(parts)})
  return results


def load_store_data():
  now = time.time()
  if _cache["entries"] and (now - _cache["fetched_at"]) < CACHE_TTL_SECONDS:
    return _cache["entries"]

  try:
    resp = requests.get(STORE_JSON_URL, timeout=15)
    resp.raise_for_status()
    entries = extract_entries(resp.json())
    if entries:
      _cache["entries"] = entries
      _cache["fetched_at"] = now
  except Exception:
    pass

  return _cache["entries"]


def search_store(query, entries, top_k=4):
  words = [w for w in query.lower().split() if len(w) > 1]
  scored = []
  for item in entries:
    title = item.get("title", "")
    content = item.get("content", "")
    haystack = f"{title} {content}".lower()
    score = sum(1 for w in words if w in haystack)
    if score > 0:
      scored.append((score, f"[{title}]\n{content}"))

  scored.sort(key=lambda x: x[0], reverse=True)
  if scored:
    return "\n\n---\n\n".join(x[1] for x in scored[:top_k])

  return "\n\n---\n\n".join(
      f"[{x.get('title', '')}]\n{x.get('content', '')}" for x in entries[:top_k]
  )


@app.route("/chat", methods=["POST"])
def chat():
  if not GEMINI_API_KEY:
    return jsonify({"reply": "إعدادات الخادم غير مكتملة."}), 500

  payload = request.get_json(silent=True) or {}
  msg = str(payload.get("message", "")).strip()[:300]
  history = payload.get("history", [])

  if not msg:
    return jsonify({"reply": "اكتب سؤالك أولاً."}), 400

  entries = load_store_data()
  if not entries:
    return jsonify({"reply": "عذراً، تعذر قراءة بيانات المتجر حالياً."})

  recent_text = " ".join(
      h.get("text", "") for h in history[-2:] if isinstance(h, dict)
  )
  context = search_store(f"{recent_text} {msg}".strip(), entries)

  system_instruction = (
      "أنت مساعد مبيعات لمتجر Ldorava. أجب الزبائن بوضوح ودقة بالاعتماد على"
      " بيانات المتجر أدناه.\n"
      "- التزم بالأسعار والمواصفات والتفاصيل الموجودة في البيانات ولا تخترع"
      " أرقاماً غير موجودة.\n"
      "- إذا لم تجد تفصيلاً محدداً في البيانات، وضّح ذلك للزبون بكل وضوح.\n\n"
      f"بيانات المتجر:\n{context}"
  )

  contents = []
  for h in history[-6:]:
    if (
        isinstance(h, dict)
        and h.get("role") in ("user", "model")
        and h.get("text")
    ):
      contents.append(
          {"role": h["role"], "parts": [{"text": str(h["text"])[:400]}]}
      )
  contents.append({"role": "user", "parts": [{"text": msg}]})

  url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
  body = {
      "system_instruction": {"parts": [{"text": system_instruction}]},
      "contents": contents,
      "generationConfig": {"temperature": 0.15, "maxOutputTokens": 400},
  }

  r = requests.post(url, json=body, timeout=20)
  if r.status_code != 200:
    return jsonify({"reply": "تعذر الاتصال بالمساعد حالياً، حاول بعد قليل."}), 502

  res_json = r.json()
  reply = (
      res_json.get("candidates", [{}])[0]
      .get("content", {})
      .get("parts", [{}])[0]
      .get("text", "")
      .strip()
  )
  return jsonify(
      {"reply": reply or "عذراً، لم أتمكن من العثور على إجابة واضحة."}
  )


if __name__ == "__main__":
  app.run(host="0.0.0.0", port=5000)