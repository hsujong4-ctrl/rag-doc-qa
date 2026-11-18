import os
import re
import csv
import time
import uuid
import tempfile
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import gradio as gr
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from docx import Document
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph
from docx.shared import Pt, RGBColor
from groq import Groq
from pypdf import PdfReader
from sklearn.feature_extraction.text import TfidfVectorizer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

# ── 設定（可用環境變數覆寫）──
GROQ_KEY = os.getenv("GROQ_API_KEY", "").strip()
EMBED_MODEL = os.getenv("EMBED_MODEL", "intfloat/multilingual-e5-small")
# 語意向量需要較多記憶體；Render 免費方案預設關閉，僅使用關鍵字與 LLM 相關策略
USE_EMBEDDINGS = os.getenv("USE_EMBEDDINGS", "0") == "1"
MODELS = ["openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3.8-27b"]
CHUNK, OVERLAP, CHILD, CHILD_OVERLAP, MAX_CTX = 500, 100, 200, 40, 7000

STRATEGIES = {
    "hybrid": ("⚡ 混合搜尋", "語意 + 關鍵字，RRF 融合排名（推薦）"),
    "semantic": ("🔍 語意搜尋", "向量相似度檢索（需開啟 USE_EMBEDDINGS）"),
    "tfidf": ("📊 關鍵字", "字元 n-gram，適合專有名詞、條號、數字"),
    "rerank": ("🎯 重新排序", "LLM 對候選片段批次評分後重排"),
    "multi_query": ("🔄 多查詢", "LLM 改寫 3 個角度問題聯合檢索"),
    "compress": ("✂️ 壓縮", "LLM 抽取每段最相關的句子"),
    "parent_child": ("📂 父子文檔", "小片段定位，回傳完整大片段"),
    "hyde": ("💡 HyDE", "先生成假設答案再檢索"),
}
_SENT = re.compile(r"(?<=[。！？!?；;])|\n+")


def split_text(text, size, overlap):
    sents = [s.strip() for s in _SENT.split(re.sub(r"[ \t　\xa0]+", " ", text)) if s and s.strip()]
    chunks, buf, fresh = [], "", 0
    for p in (s[i:i + size] for s in sents for i in range(0, len(s), size)):
        if fresh and len(buf) + len(p) + 1 > size:
            chunks.append(buf)
            buf, fresh = (buf[-overlap:].lstrip() if overlap else ""), 0
        buf = f"{buf} {p}" if buf else p
        fresh += len(p)
    return chunks + ([buf.strip()] if fresh else [])


# ── 嵌入（選用，延遲載入）──
_embedder, _embed_failed = None, False


def _embed(texts, query=False):
    global _embedder, _embed_failed
    if not USE_EMBEDDINGS or _embed_failed:
        return None
    if _embedder is None:
        try:
            from fastembed import TextEmbedding
            _embedder = TextEmbedding(model_name=EMBED_MODEL)
        except Exception as e:
            print(f"嵌入模型載入失敗，改用關鍵字檢索：{e}")
            _embed_failed = True
            return None
    prefix = ("query: " if query else "passage: ") if "e5" in EMBED_MODEL.lower() else ""
    vecs = np.array(list(_embedder.embed([prefix + t for t in texts])), dtype=np.float32)
    return vecs / (np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9)


class Index:
    """單一層級的索引：TF-IDF（必備）＋ 向量（選用）"""

    def __init__(self, texts):
        self.texts = texts
        self.vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4),
                                   sublinear_tf=True, max_features=50000)
        self.tm = self.vec.fit_transform(texts)
        self.emb = _embed(texts)

    def kw(self, q, k):
        s = (self.tm @ self.vec.transform([q]).T).toarray().ravel()
        return [int(i) for i in np.argsort(-s)[:k] if s[i] > 0]

    def dense(self, q, k):
        if self.emb is None:
            return []
        s = self.emb @ _embed([q], query=True)[0]
        return [int(i) for i in np.argsort(-s)[:k]]


def rrf(lists, k):
    sc = {}
    for lst in lists:
        for r, i in enumerate(dict.fromkeys(lst)):
            sc[i] = sc.get(i, 0) + 1 / (61 + r)
    return sorted(sc, key=sc.get, reverse=True)[:k]


class RAG:
    def __init__(self):
        self.client = None
        self.model = MODELS[0]
        self.name = ""
        self.chunks, self.labels = [], []
        self.parent = self.child = None
        self.child_to_parent = []

    # ── Groq ──
    def set_key(self, key):
        self.client = Groq(api_key=key.strip()) if key and key.strip() else None

    def list_models(self):
        if not self.client:
            return []
        try:
            ids = [m.id for m in self.client.models.list().data if getattr(m, "active", True) is not False]
        except Exception:
            return []
        return sorted(i for i in ids if not any(s in i.lower() for s in ("whisper", "tts", "orpheus", "guard", "playai")))

    def llm(self, prompt, max_tokens=300, temp=0.3, system=None, raise_err=False):
        if not self.client:
            return None
        kw = dict(model=self.model, temperature=temp, max_completion_tokens=max_tokens,
                  messages=([{"role": "system", "content": system}] if system else [])
                  + [{"role": "user", "content": prompt}])
        if "gpt-oss" in self.model:  # 推理模型：推理 token 也計入上限
            kw.update(max_completion_tokens=max(max_tokens * 3, 1024), reasoning_effort="low", include_reasoning=False)
        try:
            try:
                r = self.client.chat.completions.create(**kw)
            except Exception as e:
                if "reasoning" not in str(e).lower():
                    raise
                kw.pop("reasoning_effort", None)
                kw.pop("include_reasoning", None)
                r = self.client.chat.completions.create(**kw)
            return re.sub(r"<think>.*?</think>", "", r.choices[0].message.content or "", flags=re.S).strip()
        except Exception:
            if raise_err:
                raise
            return None

    # ── 文件載入 ──
    def load(self, path):
        t0, path = time.time(), Path(path)
        if path.suffix.lower() not in (".pdf", ".docx"):
            return "✗ 僅支援 PDF / DOCX"
        try:
            segs = self._pdf(path) if path.suffix.lower() == ".pdf" else self._docx(path)
            pairs = [(c, lab) for lab, txt in segs for c in split_text(txt, CHUNK, OVERLAP)]
            if not pairs:
                return "✗ 無可擷取文字（可能是掃描檔，需先 OCR）"
            self.chunks = [p[0] for p in pairs]
            self.labels = [p[1] for p in pairs]
            self.parent = Index(self.chunks)
            kids, self.child_to_parent = [], []
            for p, par in enumerate(self.chunks):
                for t in split_text(par, CHILD, CHILD_OVERLAP):
                    kids.append(t)
                    self.child_to_parent.append(p)
            self.child = Index(kids)
            self.name = path.name
            return f"✓ {self.name}\n{len(self.chunks)} 片段 · {time.time() - t0:.1f}s"
        except Exception as e:
            return f"✗ 載入失敗：{type(e).__name__}: {e}"

    def _pdf(self, path):
        return [(f"第 {i} 頁", t) for i, p in enumerate(PdfReader(str(path)).pages, 1)
                if (t := p.extract_text() or "").strip()]

    def _docx(self, path):
        doc, segs, head, buf, nt = Document(str(path)), [], "前言", [], 0

        def flush():
            if buf:
                segs.append((head, "\n".join(buf)))
                buf.clear()

        for el in doc.element.body.iterchildren():
            if isinstance(el, CT_P):
                para = Paragraph(el, doc)
                txt = para.text.strip()
                if not txt:
                    continue
                style = (para.style.name if para.style is not None else "") or ""
                if style.lower().startswith(("heading", "title")) or style.startswith(("標題", "标题")):
                    flush()
                    head = txt[:40]
                buf.append(txt)
            elif isinstance(el, CT_Tbl):
                nt += 1
                rows = []
                for row in Table(el, doc).rows:
                    cells = []
                    for c in row.cells:
                        t = re.sub(r"\s+", " ", c.text).strip()
                        if t and (not cells or cells[-1] != t):
                            cells.append(t)
                    if cells:
                        rows.append(" | ".join(cells))
                if rows:
                    flush()
                    segs.append((f"表格 {nt}（{head}）", "\n".join(rows)))
        flush()
        return segs

    # ── 檢索 ──
    def retrieve(self, q, strategy, k):
        P = self.parent

        def hybrid(n):
            return rrf([P.dense(q, n * 2), P.kw(q, n * 2)], n)

        if strategy == "semantic":
            idx = P.dense(q, k) or P.kw(q, k)
        elif strategy == "tfidf":
            idx = P.kw(q, k)
        elif strategy == "parent_child":
            kid = rrf([self.child.dense(q, k * 4), self.child.kw(q, k * 4)], k * 4)
            idx = list(dict.fromkeys(self.child_to_parent[i] for i in kid))[:k]
        elif strategy == "multi_query":
            r = self.llm(f"將以下問題改寫成 3 個角度不同的繁體中文問題，每行一題，不加編號：\n{q}", 200, 0.7) or ""
            extra = [l.strip(" -•*·.、)）0123456789") for l in r.splitlines()]
            extra = [e for e in extra if len(e) > 3][:3]
            lists = []
            for x in [q] + extra:
                lists += [P.dense(x, k * 2), P.kw(x, k * 2)]
            idx = rrf(lists, k)
        elif strategy == "hyde":
            h = self.llm(f"請針對以下問題寫一段約 100 字、語氣像文件原文的假設性答案（繁體中文）：\n{q}", 250, 0.7)
            lists = [P.dense(q, k * 2), P.kw(q, k * 2)]
            if h:
                lists += [P.dense(h, k * 2), P.kw(h, k * 2)]
            idx = rrf(lists, k)
        elif strategy == "rerank":
            cands = hybrid(min(max(k * 3, 6), 12))
            idx = cands[:k]
            if self.client and len(cands) > 1:
                lst = "\n\n".join(f"[{n}] {self.chunks[i][:400]}" for n, i in enumerate(cands, 1))
                r = self.llm(f"問題：{q}\n\n候選片段：\n{lst}\n\n評估每段與問題的相關度（0-10），只輸出「編號:分數」每行一個。",
                             15 * len(cands), 0) or ""
                sc = {int(a): float(b) for a, b in re.findall(r"\[?(\d+)\]?\s*[:：]\s*(\d+(?:\.\d+)?)", r)}
                idx = [cands[n] for n in sorted(range(len(cands)), key=lambda n: (-sc.get(n + 1, -1), n))[:k]]
        else:
            idx = hybrid(k)

        hits = [(i, self.chunks[i]) for i in idx]
        if strategy == "compress" and self.client and hits:
            def comp(h):
                r = self.llm(f"從以下文本逐字擷取與問題「{q}」最相關的 1-3 句，不改寫不解釋；完全無關請輸出「無」。\n\n{h[1]}", 250, 0)
                return (h[0], r if r and r != "無" else h[1][:300])
            with ThreadPoolExecutor(min(4, len(hits))) as ex:
                hits = list(ex.map(comp, hits))
        return hits

    def answer(self, q, strategy, k, model):
        if self.parent is None:
            return "請先上傳 PDF 或 DOCX 文件。", ""
        self.model, t0 = model or MODELS[0], time.time()
        hits = self.retrieve(q, strategy, int(k))
        t1 = time.time()
        blocks, used = [], 0
        for n, (i, txt) in enumerate(hits, 1):
            b = f"[片段{n}｜{self.labels[i]}]\n{txt}"
            if used and used + len(b) > MAX_CTX:
                break
            blocks.append(b)
            used += len(b)
        ctx = "\n\n".join(blocks)
        src = f"📄 {self.name} · {STRATEGIES[strategy][0]} · {len(blocks)} 片段 · 檢索 {t1 - t0:.2f}s\n{'─' * 40}\n\n{ctx}"
        if not hits:
            return "找不到相關片段，請換個問法或策略。", src
        if not self.client:
            return "⚠ 尚未設定 Groq API Key（檢索結果請看下方片段）", src
        try:
            ans = self.llm(
                f"請只根據上下文回答。若上下文沒有相關資訊，請說「文件中未提及」，不要編造。\n"
                f"每個重點後以 [片段n] 標註依據，使用繁體中文條列整理。\n\n上下文：\n{ctx}\n\n問題：{q}",
                1024, 0.2, "你是嚴謹的文件問答助手，只依據提供的上下文作答。", True)
        except Exception as e:
            return f"✗ 生成失敗：{type(e).__name__}: {e}", src
        return f"{ans or '（模型未回傳內容）'}\n\n<sub>{self.model} · 檢索 {t1 - t0:.1f}s · 生成 {time.time() - t1:.1f}s</sub>", src


rag = RAG()
rag.set_key(GROQ_KEY)


# ── Gradio 事件處理 ──
def on_key(key, cur):
    rag.set_key(key)
    if not (key or "").strip():
        return "⚠ 未設定 Key", gr.update()
    ms = rag.list_models()
    if not ms:
        return "✗ Key 無效或無法取得模型", gr.update()
    value = cur if cur in ms else (MODELS[0] if MODELS[0] in ms else ms[0])
    return f"✓ Key 有效 · {len(ms)} 個模型", gr.update(choices=ms, value=value)


def on_upload(f):
    return "⚠ 請選擇檔案" if f is None else rag.load(f if isinstance(f, str) else f.name)


def on_ask(q, history, records, strategy, k, model):
    q = (q or "").strip()
    if not q:
        return "", history, gr.update(), records
    ans, src = rag.answer(q, strategy, k, model)
    records = records + [{
        "時間": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "文件": rag.name, "問題": q,
        "回答": re.sub(r"\n*<sub>.*?</sub>", "", ans, flags=re.S).strip(),
        "策略": STRATEGIES[strategy][0], "模型": rag.model,
        "參考片段": src.split("─" * 40, 1)[-1].strip(),
    }]
    history = history + [{"role": "user", "content": q}, {"role": "assistant", "content": ans}]
    return "", history, src, records


# ── 匯出 ──
def _out_path(ext):
    return os.path.join(tempfile.gettempdir(), f"RAG問答紀錄_{datetime.now():%Y%m%d_%H%M%S}.{ext}")


def export_csv(records):
    if not records:
        gr.Warning("目前沒有問答紀錄可匯出")
        return None
    path = _out_path("csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(records[0]))
        w.writeheader()
        w.writerows(records)
    return path


def _md_runs(par, text):
    for i, part in enumerate(re.split(r"\*\*(.+?)\*\*", text)):
        if part:
            par.add_run(part).bold = bool(i % 2)


def export_docx(records, with_src=True):
    if not records:
        gr.Warning("目前沒有問答紀錄可匯出")
        return None
    doc = Document()
    st = doc.styles["Normal"]
    st.font.name, st.font.size = "Microsoft JhengHei", Pt(11)
    st.element.rPr.rFonts.set(
        "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}eastAsia", "Microsoft JhengHei")
    doc.add_heading("RAG 文件問答紀錄", 0)
    meta = doc.add_paragraph(
        f"文件：{records[-1]['文件']}　·　匯出時間：{datetime.now():%Y-%m-%d %H:%M}　·　共 {len(records)} 題")
    meta.runs[0].font.color.rgb = RGBColor(0x6B, 0x72, 0x80)
    for n, r in enumerate(records, 1):
        doc.add_heading(f"Q{n}．{r['問題']}", level=1)
        info = doc.add_paragraph(f"{r['時間']}　·　{r['策略']}　·　{r['模型']}")
        info.runs[0].font.size = Pt(9)
        info.runs[0].font.color.rgb = RGBColor(0x6B, 0x72, 0x80)
        for line in r["回答"].splitlines():
            line = line.rstrip()
            if not line.strip():
                continue
            if m := re.match(r"^\s*#{1,6}\s+(.*)", line):
                doc.add_heading(m.group(1).replace("**", ""), level=2)
            elif m := re.match(r"^(\s*)[-*•]\s+(.*)", line):
                par = doc.add_paragraph(style="List Bullet 2" if len(m.group(1)) >= 2 else "List Bullet")
                _md_runs(par, m.group(2))
            elif m := re.match(r"^\s*\d+[.)]\s+(.*)", line):
                _md_runs(doc.add_paragraph(style="List Number"), m.group(1))
            else:
                _md_runs(doc.add_paragraph(), line)
        if with_src and r["參考片段"]:
            h = doc.add_paragraph()
            run = h.add_run("參考片段")
            run.bold, run.font.size = True, Pt(9)
            for blk in r["參考片段"].split("\n\n"):
                p = doc.add_paragraph(blk.strip())
                p.paragraph_format.left_indent = Pt(12)
                for run in p.runs:
                    run.font.size = Pt(8.5)
                    run.font.color.rgb = RGBColor(0x55, 0x5B, 0x66)
    path = _out_path("docx")
    doc.save(path)
    return path


# ── PWA 設定（注入到 Gradio 頁面 <head>）──
PWA_HEAD = """
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#059669">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="文件問答">
<link rel="apple-touch-icon" href="/static/icons/apple-touch-icon.png">
<script>
(function () {
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(e => console.error(e));
  }
  const isStandalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone;
  const isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent);
  let deferredPrompt = null;

  function addButton(text, onClick) {
    const btn = document.createElement("button");
    btn.textContent = text;
    btn.style.cssText = "position:fixed;right:16px;bottom:16px;z-index:9999;padding:12px 18px;" +
      "border:none;border-radius:999px;background:#059669;color:#fff;font-size:15px;" +
      "box-shadow:0 4px 12px rgba(0,0,0,.25);cursor:pointer;";
    btn.addEventListener("click", onClick);
    document.body.appendChild(btn);
  }

  window.addEventListener("beforeinstallprompt", e => {
    e.preventDefault();
    deferredPrompt = e;
    addButton("📲 安裝應用程式", async () => {
      deferredPrompt.prompt();
      await deferredPrompt.userChoice;
      deferredPrompt = null;
    });
  });

  if (isIOS && !isStandalone) {
    addButton("📲 加入主畫面", () =>
      alert("請點選 Safari 下方的「分享」按鈕，再選擇「加入主畫面」。"));
  }
})();
</script>
"""

CSS = """
.gradio-container { max-width: 1200px !important; margin: auto; }
#hdr { padding: 18px 4px 6px; }
#hdr h1 { font-size: 26px; margin: 0; font-weight: 700; }
#hdr p { color: #6b7280; margin: 4px 0 0; font-size: 14px; }
#send { min-width: 90px; }
@media (max-width: 768px) {
  .gradio-container { padding: 8px !important; }
  #hdr h1 { font-size: 20px; }
  #send { min-width: 60px; }
}
"""


def build_ui():
    with gr.Blocks(title="RAG 文件問答", css=CSS,
                   theme=gr.themes.Soft(primary_hue="emerald", neutral_hue="slate", radius_size="lg"),
                   head=PWA_HEAD) as demo:
        gr.HTML("<div id='hdr'><h1>📚 RAG 文件問答</h1>"
                "<p>上傳 PDF / DOCX，選擇檢索策略，直接提問 · Groq</p></div>")
        with gr.Row(equal_height=False):
            with gr.Column(scale=1, min_width=280):
                with gr.Accordion("🔑 Groq API Key", open=not GROQ_KEY):
                    key = gr.Textbox(value=GROQ_KEY, type="password", show_label=False,
                                     placeholder="gsk_…（Enter 套用）")
                    key_status = gr.Markdown("✓ 已從環境變數載入" if GROQ_KEY else "⚠ 尚未設定")
                model = gr.Dropdown(MODELS, value=MODELS[0], allow_custom_value=True, label="模型")
                file = gr.File(label="上傳文件", file_types=[".pdf", ".docx"], type="filepath", height=120)
                status = gr.Textbox(show_label=False, interactive=False, lines=2, placeholder="尚未載入文件")
                strategy = gr.Radio([(v[0], k) for k, v in STRATEGIES.items()], value="hybrid", label="檢索策略")
                desc = gr.Markdown(f"<small>{STRATEGIES['hybrid'][1]}</small>")
                topk = gr.Slider(1, 10, 4, step=1, label="片段數量 Top-K")
            with gr.Column(scale=2):
                chat = gr.Chatbot(height=520, show_label=False,
                                  placeholder="**上傳文件後開始提問**<br>例如：這份文件的重點是什麼？")
                with gr.Row():
                    q = gr.Textbox(show_label=False, placeholder="輸入問題，Enter 送出", scale=6, container=False)
                    send = gr.Button("送出", variant="primary", scale=1, elem_id="send")
                with gr.Row():
                    gr.Examples([["這份文件的重點是什麼？"], ["文件提到哪些規定或限制？"], ["有哪些風險或注意事項？"]],
                                q, label=None)
                    clear = gr.Button("🗑 清除對話", size="sm", variant="secondary")
                with gr.Accordion("🔎 檢索到的片段", open=False):
                    src = gr.Textbox(show_label=False, lines=12, interactive=False)
                with gr.Row():
                    with_src = gr.Checkbox(True, label="Word 包含參考片段", scale=2)
                    btn_docx = gr.Button("📄 匯出 Word", size="sm", scale=1)
                    btn_csv = gr.Button("📊 匯出 CSV", size="sm", scale=1)
                out_file = gr.File(label="下載檔案", visible=False)
        records = gr.State([])

        key.submit(on_key, [key, model], [key_status, model])
        key.blur(on_key, [key, model], [key_status, model])
        file.upload(on_upload, file, status)
        strategy.change(lambda s: f"<small>{STRATEGIES[s][1]}</small>", strategy, desc)
        for ev in (q.submit, send.click):
            ev(on_ask, [q, chat, records, strategy, topk, model], [q, chat, src, records])
        clear.click(lambda: ([], "", [], gr.update(value=None, visible=False)), None,
                    [chat, src, records, out_file])
        show = lambda p: gr.update(value=p, visible=bool(p))
        btn_docx.click(lambda r, w: show(export_docx(r, w)), [records, with_src], out_file)
        btn_csv.click(lambda r: show(export_csv(r)), records, out_file)
    return demo


DEMO = build_ui()

# ── FastAPI + PWA 路由 ──
api = FastAPI(title="RAG 文件問答")
api.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@api.get("/sw.js")
def service_worker():
    return FileResponse(os.path.join(STATIC_DIR, "sw.js"), media_type="application/javascript",
                        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


@api.get("/manifest.webmanifest")
def manifest():
    return FileResponse(os.path.join(STATIC_DIR, "manifest.webmanifest"),
                        media_type="application/manifest+json")


@api.get("/offline.html")
def offline_page():
    return FileResponse(os.path.join(STATIC_DIR, "offline.html"), media_type="text/html")


@api.get("/healthz")
def healthz():
    return JSONResponse({"status": "ok", "has_key": bool(GROQ_KEY), "embeddings": USE_EMBEDDINGS})


app = gr.mount_gradio_app(api, DEMO, path="/")

if __name__ == "__main__":
    if os.environ.get("PORT"):  # Render 會提供 PORT
        uvicorn.run(app, host="0.0.0.0", port=int(os.environ["PORT"]))
    else:  # 本機或 Colab：產生公開連結
        DEMO.queue().launch(share=True)
