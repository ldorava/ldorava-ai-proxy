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
        score += 5
      elif w in norm_title:
        score += 3
      elif re.search(rf"\b{re.escape(w)}\b", norm_haystack):
        score += 2
      elif len(w) >= 3 and w in norm_haystack:
        score += 1

    if score > 0:
      scored.append((score, f"[{title}]\n{content[:600]}"))

  scored.sort(key=lambda x: x[0], reverse=True)
  if scored:
    return "\n\n---\n\n".join(x[1] for x in scored[:top_k])

  # إذا لم يظهر تطابق مباشر، نرسل ملخصاً مختصراً جداً للمتوفرات دون فرض منتجات عشوائية
  summary = []
  for x in entries[:6]:
    t = x.get("title", "").strip()
    if t:
      summary.append(f"- {t}")
  return "المنتجات المذكورة في البيانات:\n" + "\n".join(summary)
