from http.server import BaseHTTPRequestHandler
import json
import os
import re
import time
import urllib.error
import urllib.request

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
STORE_JSON_URL = os.environ.get(
    "STORE_JSON_URL",
    "https://raw.githubusercontent.com/ldorava/Ldorava/main/data/store.json",
).strip()
PDF_URLS = [
    u.strip()
    for u in os.environ.get(
        "PDF_URLS",
        "https://raw.githubusercontent.com/ldorava/Ldorava/main/data/guide.pdf",
    ).split(",")
    if u.strip()
]
ALLOWED_ORIGIN = "https://ldorava.github.io"

MODELS_PRIORITY = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
]

STORE_CACHE_TTL = 60
PDF_CACHE_TTL = 3600  # ساعة واحدة لتجنب إعادة رفع الملف في كل طلب

_cache = {
    "store_entries": [],
    "store_fetched_at": 0,
    "pdf_files": [],
    "pdf_fetched_at": 0,
}


def normalize_ar(text):
  text = str(text or "").lower()
  text = (
      text.replace("\u200c", " ").replace("\u200f", " ").replace("\u200e", " ")
  )
  text = re.sub(r"[\u064b-\u065f\u0670]", "", text)
  text = re.sub(r"[أإآاٱ]", "ا", text)
  text = text.replace("ى", "ي").replace("ي", "ي").replace("ة", "ه")
  text = re.sub(r"[^\w\s\u0600-\u06ff]", " ", text)
  return re.sub(r"\s+", " ", text).strip()


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
      if k in ("title", "name", "product_name", "label", "image"):
        continue
      if v is None or v == "" or v == []:
        continue
      if isinstance(v, (str, int, float, bool)):
        parts.append(f"{k}: {v}")
      elif isinstance(v, list):
        sub_texts = []
        for sub in v:
          if isinstance(sub, dict):
            sub_items = [
                f"{sk}: {sv}"
                for sk, sv in sub.items()
                if sk != "image" and sv not in (None, "", [])
            ]
            if sub_items:
              sub_texts.append(" | ".join(sub_items))
          else:
            sub_texts.append(str(sub))
        if sub_texts:
          parts.append(f"{k}: {' // '.join(sub_texts)}")
      elif isinstance(v, dict):
        results.extend(extract_entries(v, f"{title} - {k}"))

    if parts:
      results.append({"title": title, "content": "\n".join(parts)})
  return results


def load_store_data():
  now = time.time()
  if (
      _cache["store_entries"]
      and (now - _cache["store_fetched_at"]) < STORE_CACHE_TTL
  ):
    return _cache["store_entries"]

  try:
    req = urllib.request.Request(
        STORE_JSON_URL, headers={"User-Agent": "LdoravaBot/1.0"}
    )
    with urllib.request.urlopen(req, timeout=6) as resp:
      raw_data = json.loads(resp.read().decode("utf-8"))
      products = (
          raw_data.get("products") if isinstance(raw_data, dict) else raw_data
      )
      entries = extract_entries(
          products if products is not None else raw_data
      )
      if entries:
        _cache["store_entries"] = entries
        _cache["store_fetched_at"] = now
  except Exception:
    pass

  return _cache["store_entries"]


def upload_pdf_to_gemini(pdf_bytes, display_name="guide.pdf"):
  if not GEMINI_API_KEY or not pdf_bytes:
    return None

  # خطوة 1: بدء جلسة الرفع القابلة للاستئناف (Resumable Upload)
  start_url = f"https://generativelanguage.googleapis.com/upload/v1beta/files?key={GEMINI_API_KEY}"
  start_headers = {
      "X-Goog-Upload-Protocol": "resumable",
      "X-Goog-Upload-Command": "start",
      "X-Goog-Upload-Header-Content-Length": str(len(pdf_bytes)),
      "X-Goog-Upload-Header-Content-Type": "application/pdf",
      "Content-Type": "application/json",
  }
  start_body = json.dumps({"file": {"display_name": display_name}}).encode(
      "utf-8"
  )

  req1 = urllib.request.Request(
      start_url, data=start_body, headers=start_headers, method="POST"
  )
  with urllib.request.urlopen(req1, timeout=12) as resp1:
    upload_url = resp1.headers.get("X-Goog-Upload-URL")

  if not upload_url:
    return None

  # خطوة 2: رفع بايتات ملف الـ PDF وإنهاء الجلسة
  upload_headers = {
      "Content-Length": str(len(pdf_bytes)),
      "X-Goog-Upload-Offset": "0",
      "X-Goog-Upload-Command": "upload, finalize",
  }
  req2 = urllib.request.Request(
      upload_url, data=pdf_bytes, headers=upload_headers, method="POST"
  )
  with urllib.request.urlopen(req2, timeout=20) as resp2:
    info = json.loads(resp2.read().decode("utf-8"))

  file_obj = info.get("file") if isinstance(info, dict) and "file" in info else info
  file_uri = file_obj.get("uri") if isinstance(file_obj, dict) else None
  file_name = file_obj.get("name") if isinstance(file_obj, dict) else None
  state = file_obj.get("state") if isinstance(file_obj, dict) else None

  # انتظار قصير إذا كانت حالة الملف PROCESSING
  if file_name and state == "PROCESSING":
    for _ in range(4):
      time.sleep(1)
      chk_url = f"https://generativelanguage.googleapis.com/v1beta/{file_name}?key={GEMINI_API_KEY}"
      with urllib.request.urlopen(chk_url, timeout=8) as chk_resp:
        chk_data = json.loads(chk_resp.read().decode("utf-8"))
        state = chk_data.get("state")
        file_uri = chk_data.get("uri") or file_uri
        if state == "ACTIVE":
          break

  if file_uri:
    return {"mimeType": "application/pdf", "fileUri": file_uri}
  return None


def load_gemini_pdf_files():
  now = time.time()
  if _cache["pdf_files"] and (now - _cache["pdf_fetched_at"]) < PDF_CACHE_TTL:
    return _cache["pdf_files"]

  uploaded = []
  for idx, url in enumerate(PDF_URLS, start=1):
    try:
      req = urllib.request.Request(
          url, headers={"User-Agent": "LdoravaBot/1.0"}
      )
      with urllib.request.urlopen(req, timeout=10) as resp:
        pdf_bytes = resp.read()
      file_part = upload_pdf_to_gemini(pdf_bytes, display_name=f"guide_{idx}.pdf")
      if file_part:
        uploaded.append(file_part)
    except Exception:
      continue

  if uploaded:
    _cache["pdf_files"] = uploaded
    _cache["pdf_fetched_at"] = now

  return _cache["pdf_files"]


def search_store(query, entries, top_k=3):
  norm_q = normalize_ar(query)
  words = [w for w in norm_q.split() if len(w) >= 2]
  scored = []

  for item in entries:
    title = item.get("title", "")
    content = item.get("content", "")
    norm_title = normalize_ar(title)
    norm_haystack = f"{norm_title} {normalize_ar(content)}"

    score = 0
    for w in words:
      if re.search(rf"\b{re.escape(w)}\b", norm_title):
        score += 8
      elif w in norm_title:
        score += 5
      elif re.search(rf"\b{re.escape(w)}\b", norm_haystack):
        score += 2
      elif len(w) >= 3 and w in norm_haystack:
        score += 1

    if score > 0:
      scored.append((score, f"[{title}]\n{content[:1000]}"))

  scored.sort(key=lambda x: x[0], reverse=True)
  if scored:
    return "\n\n---\n\n".join(x[1] for x in scored[:top_k])

  summary = []
  for x in entries[:8]:
    t = x.get("title", "").strip()
    if t:
      summary.append(f"- {t}")
  return "المنتجات المتوفرة في المتجر:\n" + "\n".join(summary)


def find_matching_product_id(text_pool, entries):
  norm_pool = normalize_ar(text_pool)
  best_id = None
  best_score = 0

  for item in entries:
    title = item.get("title", "")
    content = item.get("content", "")
    norm_title = normalize_ar(title)
    words = [w for w in norm_title.split() if len(w) >= 3]
    score = sum(1 for w in words if w in norm_pool)

    if score > best_score and score >= 1:
      m = re.search(r"\bid:\s*([^\n]+)", content)
      if m:
        best_score = score
        best_id = m.group(1).strip()

  return best_id


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
    pdf_files = load_gemini_pdf_files()

    if not entries and not pdf_files:
      self._send_json(
          200,
          {
              "reply": (
                  "عذراً، تعذر قراءة بيانات المتجر أو ملفات الإرشادات حالياً."
              )
          },
      )
      return

    recent_text = " ".join(
        h.get("text", "") for h in history[-2:] if isinstance(h, dict)
    )
    context = (
        search_store(f"{recent_text} {msg}".strip(), entries, top_k=3)
        if entries
        else "لا توجد بيانات منتجات متاحة حالياً."
    )

    system_instruction = (
        "أنت مساعد مبيعات واستشارات ودود وسريع لمتجر L'DORA Viva. أجب"
        " بإيجاز ووضوح بالاعتماد على ملفات PDF المرفقة وبيانات المتجر"
        " أدناه فقط.\n"
        "- إذا سأل الزبون عن تعليمات أو نصائح (مثل معرفة نوع البشرة أو"
        " الخطوات والإرشادات)، استخرج الإجابة مباشرة من ملفات PDF المرفقة"
        " وقدّمها بوضوح.\n"
        "- اعتمد على بيانات المتجر كمرجع وحيد للأسعار (د.ع) والتوفر"
        " وطريقة الاستخدام (usage) والأنواع (variants) ولا تخترع منتجات أو"
        " أسعار غير موجودة.\n"
        "- لا تقلد أخطاء سابقة من سجل المحادثة ولا تخلط بين المنتجات.\n\n"
        f"بيانات المتجر:\n{context}"
    )

    contents = []
    for h in history[-2:]:
      if (
          isinstance(h, dict)
          and h.get("role") in ("user", "model")
          and h.get("text")
      ):
        txt = str(h["text"]).strip()
        if h.get("role") == "model" and "غير متوفر" in txt:
          continue
        contents.append({"role": h["role"], "parts": [{"text": txt[:220]}]})

    user_parts = [{"text": msg}]
    for pf in pdf_files:
      user_parts.append({"fileData": pf})

    contents.append({"role": "user", "parts": user_parts})

    res_json = None
    last_err = ""

    for model_name in MODELS_PRIORITY:
      url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
      req_body = json.dumps({
          "system_instruction": {"parts": [{"text": system_instruction}]},
          "contents": contents,
          "generationConfig": {"temperature": 0.15, "maxOutputTokens": 260},
      }).encode("utf-8")

      try:
        req = urllib.request.Request(
            url,
            data=req_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
          res_json = json.loads(resp.read().decode("utf-8"))
        break
      except urllib.error.HTTPError as e:
        err_detail = e.read().decode("utf-8", errors="ignore")[:180]
        if e.code in (503, 429, 404):
          last_err = (
              "المساعد يمر بضغط مؤقت الآن، جرّب ترسل السؤال مرة ثانية بعد"
              " لحظات."
          )
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

    add_to_cart_id = None
    confirm_words = (
        "نعم",
        "ايه",
        "اي",
        "تمام",
        "ضيفه",
        "اضفه",
        "ثبت",
        "موافق",
    )
    if entries and any(w in normalize_ar(msg) for w in confirm_words):
      add_to_cart_id = find_matching_product_id(
          f"{recent_text} {reply}", entries
      )

    self._send_json(
        200,
        {
            "reply": reply or "عذراً، لم أتمكن من العثور على إجابة واضحة.",
            "add_to_cart_id": add_to_cart_id,
        },
    )
