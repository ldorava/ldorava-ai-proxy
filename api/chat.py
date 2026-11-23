from http.server import BaseHTTPRequestHandler
import json
import os
import time
import urllib.error
import urllib.request

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
STORE_JSON_URL = os.environ.get(
    "STORE_JSON_URL",
    "https://raw.githubusercontent.com/ldorava/Ldorava/main/data/store.json",
).strip()
ALLOWED_ORIGIN = "https://ldorava.github.io"

MODELS_PRIORITY = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
]

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
    req = urllib.request.Request(
        STORE_JSON_URL, headers={"User-Agent": "LdoravaBot/1.0"}
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
      raw_data = json.loads(resp.read().decode("utf-8"))
      entries = extract_entries(raw_data)
      if entries:
        _cache["entries"] = entries
        _cache["fetched_at"] = now
  except Exception:
    pass

  return _cache["entries"]


def search_store(query, entries, top_k=2):
  words = [w for w in query.lower().split() if len(w) > 1]
  scored = []
  for item in entries:
    title = item.get("title", "")
    content = item.get("content", "")
    haystack = f"{title} {content}".lower()
    score = sum(1 for w in words if w in haystack)
    if score > 0:
      scored.append((score, f"[{title}]\n{content[:500]}"))

  scored.sort(key=lambda x: x[0], reverse=True)
  if scored:
    return "\n\n---\n\n".join(x[1] for x in scored[:top_k])

  return "\n\n---\n\n".join(
      f"[{x.get('title', '')}]\n{x.get('content', '')[:500]}"
      for x in entries[:top_k]
  )


class handler(BaseHTTPRequestHandler):

  def _set_cors(self):
    self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
    self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
    self.send_header("Access-Control-Allow-Headers", "Content-Type")

  def _send_json(self, status_code, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    self.send_response(status_code)
    self._set_cors()
    self.send_header("Content-Type", "application/json; charset=utf-8")
    self.send_header("Content-Length", str(len(body)))
    self.end_headers()
    self.wfile.write(body)

  def do_OPTIONS(self):
    self.send_response(204)
    self._set_cors()
    self.end_headers()

  def do_POST(self):
    if not GEMINI_API_KEY:
      self._send_json(
          500, {"reply": "مفتاح GEMINI_API_KEY غير موجود في إعدادات Vercel."}
      )
      return

    try:
      length = int(self.headers.get("Content-Length", "0"))
      raw_body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
      payload = json.loads(raw_body)
    except Exception:
      self._send_json(400, {"reply": "صيغة الطلب غير صالحة."})
      return

    msg = str(payload.get("message", "")).strip()[:300]
    history = payload.get("history", [])
    if not msg:
      self._send_json(400, {"reply": "اكتب سؤالك أولاً."})
      return

    entries = load_store_data()
    if not entries:
      self._send_json(
          200,
          {
              "reply": (
                  "عذراً، تعذر قراءة بيانات المتجر من الرابط المحدد حالياً."
              )
          },
      )
      return

    recent_text = " ".join(
        h.get("text", "") for h in history[-2:] if isinstance(h, dict)
    )
    context = search_store(f"{recent_text} {msg}".strip(), entries, top_k=2)

    system_instruction = (
        "أنت مساعد مبيعات لمتجر Ldorava. أجب الزبائن بإيجاز ودقة بالاعتماد"
        " على بيانات المتجر أدناه فقط ولا تخترع معلومات.\n\n"
        f"بيانات المتجر:\n{context}"
    )

    contents = []
    for h in history[-4:]:
      if (
          isinstance(h, dict)
          and h.get("role") in ("user", "model")
          and h.get("text")
      ):
        contents.append(
            {"role": h["role"], "parts": [{"text": str(h["text"])[:300]}]}
        )
    contents.append({"role": "user", "parts": [{"text": msg}]})

    res_json = None
    last_err = ""

    for model_name in MODELS_PRIORITY:
      url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
      req_body = json.dumps({
          "system_instruction": {"parts": [{"text": system_instruction}]},
          "contents": contents,
          "generationConfig": {"temperature": 0.15, "maxOutputTokens": 250},
      }).encode("utf-8")

      try:
        req = urllib.request.Request(
            url,
            data=req_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=14) as resp:
          res_json = json.loads(resp.read().decode("utf-8"))
        break
      except urllib.error.HTTPError as e:
        err_detail = e.read().decode("utf-8", errors="ignore")[:180]
        if e.code in (503, 429, 404):
          last_err = "المساعد يمر بضغط مؤقت الآن، جرّب ترسل السؤال مرة ثانية بعد لحظات."
          continue
        last_err = f"خطأ من Gemini ({e.code}): {err_detail}"
        break
      except Exception as e:
        last_err = f"تعذر الاتصال بـ Gemini: {str(e)[:180]}"
        continue

    if res_json is None:
      self._send_json(502, {"reply": last_err})
      return

    reply = (
        res_json.get("candidates", [{}])[0]
        .get("content", {})
        .get("parts", [{}])[0]
        .get("text", "")
        .strip()
    )
    self._send_json(
        200,
        {"reply": reply or "عذراً، لم أتمكن من العثور على إجابة واضحة."},
    )
