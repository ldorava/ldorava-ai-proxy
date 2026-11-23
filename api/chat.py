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
GUIDE_TXT_URL = os.environ.get(
    "GUIDE_TXT_URL",
    "https://raw.githubusercontent.com/ldorava/Ldorava/main/data/guide.txt",
).strip()
ALLOWED_ORIGIN = "https://ldorava.github.io"

MODELS_PRIORITY = [
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
]

CACHE_TTL_SECONDS = 60
_cache = {
    "entries": [],
    "fetched_at": 0,
}


def normalize_ar(text):
  text = str(text or "").lower()
  text = (
      text.replace("\u200c", " ").replace("\u200f", " ").replace("\u200e", " ")
  )
  text = re.sub(r"[\u064b-\u065f\u0670]", "", text)
  text = re.sub(r"[أإآاٱ]", "ا", text)
  text = text.replace("ى", "ي").replace("ي", "ي").replace("ة", "ه")
  text = re.sub(r"[^\w\s\u0600-\u6ff]", " ", text)
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


def parse_guide_text(raw_text):
  if not raw_text:
    return []
  blocks = [
      b.strip() for b in re.split(r"\n\s*\n+", raw_text.strip()) if b.strip()
  ]
  guide_entries = []
  for idx, block in enumerate(blocks, start=1):
    first_line = block.splitlines()[0].strip()[:80]
    title = (
        f"دليل العناية - {first_line}" if first_line else f"دليل العناية #{idx}"
    )
    guide_entries.append({
        "title": title,
        "content": block[:1400],
    })
  return guide_entries


def fetch_url_text(url, timeout=6):
  if not url:
    return ""
  req = urllib.request.Request(url, headers={"User-Agent": "LdoravaBot/1.0"})
  with urllib.request.urlopen(req, timeout=timeout) as resp:
    return resp.read().decode("utf-8", errors="ignore")


def load_all_data():
  now = time.time()
  if _cache["entries"] and (now - _cache["fetched_at"]) < CACHE_TTL_SECONDS:
    return _cache["entries"]

  combined = []

  try:
    raw_store = fetch_url_text(STORE_JSON_URL, timeout=6)
    if raw_store:
      store_json = json.loads(raw_store)
      products = (
          store_json.get("products")
          if isinstance(store_json, dict)
          else store_json
      )
      combined.extend(
          extract_entries(products if products is not None else store_json)
      )
  except Exception:
    pass

  try:
    raw_guide = fetch_url_text(GUIDE_TXT_URL, timeout=6)
    if raw_guide:
      combined.extend(parse_guide_text(raw_guide))
  except Exception:
    pass

  if combined:
    _cache["entries"] = combined
    _cache["fetched_at"] = now

  return _cache["entries"]


def search_store(query, entries, top_k=4):
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
        score += 9
      elif w in norm_title:
        score += 5
      elif re.search(rf"\b{re.escape(w)}\b", norm_haystack):
        score += 2
      elif len(w) >= 3 and w in norm_haystack:
        score += 1

    if score > 0:
      scored.append((score, f"[{title}]\n{content[:1100]}"))

  scored.sort(key=lambda x: x[0], reverse=True)
  if scored:
    return "\n\n---\n\n".join(x[1] for x in scored[:top_k])

  summary = []
  for x in entries[:8]:
    t = x.get("title", "").strip()
    if t:
      summary.append(f"- {t}")
  return "المنتجات والإرشادات المتوفرة:\n" + "\n".join(summary)


def build_context(combined_q, entries):
  norm_q = normalize_ar(combined_q)

  # 1) أسئلة معرفة نوع البشرة أو أنواع البشرة
  if any(
      k in norm_q
      for k in ("نوع بشرت", "انواع البشر", "اعرف بشرت", "تحديد البشر")
  ):
    parts = []
    for topic in (
        "تحديد البشرة الجافة",
        "البشرة الدهنية",
        "البشرة المختلطة",
        "البشرة الحساسة",
    ):
      found = search_store(topic, entries, top_k=1)
      if found and found not in parts:
        parts.append(found)
    if parts:
      return "\n\n---\n\n".join(parts)

  # 2) أسئلة معرفة نوع الشعر أو أنواع الشعر
  if any(
      k in norm_q for k in ("نوع شعر", "انواع الشعر", "اعرف شعر", "تحديد الشعر")
  ):
    parts = []
    for topic in (
        "الشعر الجاف",
        "الشعر الدهني",
        "الشعر المختلط",
        "العناية بالشعر العادي",
    ):
      found = search_store(topic, entries, top_k=1)
      if found and found not in parts:
        parts.append(found)
    if parts:
      return "\n\n---\n\n".join(parts)

  # 3) أسئلة الباقات العلاجية أو التفتيح أو الشعر
  if any(
      k in norm_q for k in ("باقه", "باقات", "مجموعه", "مجموعات", "بكج", "بكجات")
  ):
    base = search_store(combined_q, entries, top_k=3)
    notes = search_store("ملاحظات هامة تحذيرات تخصيص الباقات", entries, top_k=1)
    return f"{base}\n\n---\n\n{notes}" if notes else base

  return search_store(combined_q, entries, top_k=4)


def find_matching_product_id(text_pool, entries):
  norm_pool = normalize_ar(text_pool)
  best_id = None
  best_score = 0

  for item in entries:
    title = item.get("title", "")
    content = item.get("content", "")
    if "\nid:" not in f"\n{content}" and not content.startswith("id:"):
      continue
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

    entries = load_all_data()
    if not entries:
      self._send_json(
          200,
          {
              "reply": (
                  "عذراً، تعذر قراءة بيانات المتجر أو ملف الإرشادات حالياً."
              )
          },
      )
      return

    recent_text = " ".join(
        h.get("text", "") for h in history[-2:] if isinstance(h, dict)
    )
    combined_q = f"{recent_text} {msg}".strip()
    context = build_context(combined_q, entries)

    system_instruction = (
        "أنت مساعد مبيعات واستشارات ودود، دقيق، وسريع لمتجر L'DORA Viva. ادخل"
        " في صلب الإجابة مباشرة دون مقدمات طويلة متكررة، بالاعتماد على"
        " البيانات والإرشادات أدناه فقط.\n"
        "- عند السؤال عن معرفة نوع البشرة أو أنواعها: اذكر خطوات الفحص"
        " بترقيم واضح ثم وضّح علامات وطريقة تمييز الأنواع المتوفرة (الجافة،"
        " الدهنية، المختلطة، الحساسة).\n"
        "- عند السؤال عن معرفة نوع الشعر أو أنواع الشعر: اذكر خطوات الفحص"
        " والعلامات والعناية لكل نوع متوفر في الدليل.\n"
        "- عند السؤال عن الباقات أو العناية أو طريقة الاستخدام: رتّب"
        " المكونات أو الخطوات بنقاط واضحة ومباشرة، واذكر الملاحظات أو"
        " التحذيرات المهمة المرتبطة بها إن وجدت.\n"
        "- إذا رشّحت منتجاً من بيانات المتجر، اذكر اسمه وسعره بدقة بعملة"
        " (د.ع) ولا تخترع منتجات أو أسعار غير موجودة.\n"
        "- اختم بسؤال قصير ومفيد واحد فقط لتضييق الخيار عند الحاجة (مثل نوع"
        " البشرة أو المشكلة أو الميزانية).\n"
        "- لا تقلد أخطاء سابقة من سجل المحادثة ولا تخلط بين المنتجات.\n\n"
        f"البيانات والإرشادات:\n{context}"
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
    contents.append({"role": "user", "parts": [{"text": msg}]})

    res_json = None
    last_err = ""

    for model_name in MODELS_PRIORITY:
      url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
      req_body = json.dumps({
          "system_instruction": {"parts": [{"text": system_instruction}]},
          "contents": contents,
          "generationConfig": {"temperature": 0.15, "maxOutputTokens": 360},
      }).encode("utf-8")

      try:
        req = urllib.request.Request(
            url,
            data=req_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=11) as resp:
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
    if any(w in normalize_ar(msg) for w in confirm_words):
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
